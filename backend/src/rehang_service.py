"""Re-hang (改挂) trial calculation and atomic confirmation.

Business rules implemented here:

* A supervisor picks a batch of boxes on the source call and a receiving call.
  Both calls are scored with the *same* shared model (``src.model``) before and
  after the hypothetical re-hang.
* A trial is only eligible when the source call's severe-delay probability goes
  down **and** the receiving call's does not go up.
* Manifests and gate records keep changing while a trial is open.  Every trial
  stores a digest of the evidence used (both calls, their containers, and their
  active gate events).  Confirmation re-reads the evidence: if the digest moved,
  the trial is invalidated and must be recalculated.
* A batch is all-or-nothing.  Confirmation locks the containers, verifies each
  one is still on the source call, and only then moves the whole batch.
* Confirmation is idempotent.  The same idempotency key replays the original
  result; a trial that is already confirmed also replays its stored result, so
  an interrupted retry never moves a box twice.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .model import MODEL_VERSION, SEVERE_LEVELS, predict
from .models import (
    AuditLog,
    ContainerCheck,
    GateEvent,
    RehangConfirmation,
    RehangTrial,
    VesselCall,
)
from .services import features_from_records

ELIGIBLE_STATUSES = {"calculated"}
EPSILON = 1e-9


def _now():
    return datetime.now(timezone.utc)


def _log(db: Session, user, action: str, trial: RehangTrial, detail: str = ""):
    db.add(AuditLog(
        actor=user.username,
        role=user.role,
        action=action,
        entity="rehang_trial",
        entity_id=trial.id,
        detail=detail,
    ))


def _containers(db: Session, vessel_id: int):
    return list(db.execute(
        select(ContainerCheck)
        .where(ContainerCheck.vessel_id == vessel_id)
        .order_by(ContainerCheck.id)
        .execution_options(populate_existing=True)
    ).scalars().all())


def _active_gates(db: Session, container_ids):
    if not container_ids:
        return []
    return list(db.execute(
        select(GateEvent)
        .where(
            GateEvent.container_id.in_(container_ids),
            GateEvent.status == "active",
        )
        .order_by(GateEvent.id)
        .execution_options(populate_existing=True)
    ).scalars().all())


def _evidence_digest(source: VesselCall, target: VesselCall, containers, gates) -> str:
    payload = {
        "model_version": MODEL_VERSION,
        "vessels": [
            [v.id, v.call_no, v.vessel_name, v.terminal, v.berth,
             v.eta.isoformat(), v.etd.isoformat()]
            for v in sorted((source, target), key=lambda item: item.id)
        ],
        # Every container on either call is part of the manifest evidence, so
        # edits outside the selected batch also invalidate an open trial.
        "containers": [
            [c.id, c.vessel_id, c.container_no, c.iso_type, c.cargo_class,
             c.weight_t, c.customs_status, c.seal_status, c.check_state]
            for c in sorted(containers, key=lambda item: item.id)
        ],
        "gate_events": [
            [g.id, g.container_id, g.event_ref, g.gate_code, g.event_type,
             g.event_time.isoformat(), g.dwell_min, g.status]
            for g in sorted(gates, key=lambda item: item.id)
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _side(vessel: VesselCall, before_features: dict, after_features: dict) -> dict:
    before = predict(before_features)
    after = predict(after_features)
    return {
        "vessel_id": vessel.id,
        "call_no": vessel.call_no,
        "vessel_name": vessel.vessel_name,
        "before": {
            "severe_probability": before["severe_probability"],
            "label": before["label"],
            "probabilities": before["probabilities"],
            "features": before["features"],
        },
        "after": {
            "severe_probability": after["severe_probability"],
            "label": after["label"],
            "probabilities": after["probabilities"],
            "features": after["features"],
        },
        "delta": round(after["severe_probability"] - before["severe_probability"], 6),
    }


def evaluate(db: Session, source: VesselCall, target: VesselCall, container_ids) -> dict:
    """Score the two calls before and after moving ``container_ids``.

    This is a pure calculation: nothing is written to the database.
    """
    source_containers = _containers(db, source.id)
    target_containers = _containers(db, target.id)
    all_ids = [c.id for c in source_containers] + [c.id for c in target_containers]
    gates = _active_gates(db, all_ids)

    selected_ids = set(container_ids)
    moved = [c for c in source_containers if c.id in selected_ids]
    remaining = [c for c in source_containers if c.id not in selected_ids]
    source_ids = {c.id for c in source_containers}
    target_ids = {c.id for c in target_containers}
    remaining_ids = {c.id for c in remaining}

    source_before = features_from_records(
        source, source_containers, [g for g in gates if g.container_id in source_ids]
    )
    source_after = features_from_records(
        source, remaining, [g for g in gates if g.container_id in remaining_ids]
    )
    target_before = features_from_records(
        target, target_containers, [g for g in gates if g.container_id in target_ids]
    )
    target_after = features_from_records(
        target,
        target_containers + moved,
        [g for g in gates if g.container_id in target_ids | selected_ids],
    )

    source_side = _side(source, source_before, source_after)
    target_side = _side(target, target_before, target_after)
    source_decreased = source_side["delta"] < -EPSILON
    target_not_increased = target_side["delta"] <= EPSILON

    return {
        "model_version": MODEL_VERSION,
        "severe_levels": list(SEVERE_LEVELS),
        "rule": "原船次严重延误概率下降，且接收船次严重延误概率不上升，才可确认改挂。",
        "source": source_side,
        "target": target_side,
        "checks": {
            "source_decreased": source_decreased,
            "target_not_increased": target_not_increased,
        },
        "eligible": bool(source_decreased and target_not_increased),
        "containers": [
            {
                "id": c.id,
                "container_no": c.container_no,
                "iso_type": c.iso_type,
                "cargo_class": c.cargo_class,
                "customs_status": c.customs_status,
                "seal_status": c.seal_status,
            }
            for c in moved
        ],
        "container_ids": [c.id for c in moved],
        "evidence_digest": _evidence_digest(source, target, source_containers + target_containers, gates),
    }


def _require_vessel(db: Session, vessel_id: int) -> VesselCall:
    vessel = db.get(VesselCall, vessel_id)
    if not vessel:
        raise HTTPException(404, "船次不存在")
    return vessel


def create_trial(db: Session, user, source_vessel_id: int, target_vessel_id: int, container_ids) -> RehangTrial:
    if user.role != "supervisor":
        raise HTTPException(403, "只有主管可以发起改挂试算")
    if source_vessel_id == target_vessel_id:
        raise HTTPException(422, "原船次与接收船次不能相同")
    if not container_ids:
        raise HTTPException(422, "请至少选择一个箱号")
    if len(set(container_ids)) != len(container_ids):
        raise HTTPException(422, "箱号不能重复")

    source = _require_vessel(db, source_vessel_id)
    target = _require_vessel(db, target_vessel_id)
    rows = list(db.execute(
        select(ContainerCheck).where(ContainerCheck.id.in_(container_ids))
    ).scalars().all())
    if len(rows) != len(container_ids):
        raise HTTPException(404, "部分箱号不存在")
    wrong = [c.container_no for c in rows if c.vessel_id != source.id]
    if wrong:
        raise HTTPException(409, f"箱号不在原船次：{', '.join(sorted(wrong))}")

    evaluation = evaluate(db, source, target, container_ids)
    trial = RehangTrial(
        trial_no=f"REH-{_now():%Y%m%d}-{uuid4().hex[:12]}",
        source_vessel_id=source.id,
        target_vessel_id=target.id,
        container_ids=[c.id for c in sorted(rows, key=lambda item: item.id)],
        status="calculated",
        evaluation=evaluation,
        evidence_digest=evaluation["evidence_digest"],
        created_by=user.username,
    )
    db.add(trial)
    db.flush()
    _log(db, user, "rehang_trial_calculated", trial,
         json.dumps({"eligible": evaluation["eligible"],
                     "source": evaluation["source"]["delta"],
                     "target": evaluation["target"]["delta"]}, ensure_ascii=False))
    db.commit()
    db.refresh(trial)
    return trial


def trial_public(trial: RehangTrial, db: Session, digest_cache: dict | None = None) -> dict:
    """Serialize a trial and flag whether its evidence is still current."""
    source = db.get(VesselCall, trial.source_vessel_id)
    target = db.get(VesselCall, trial.target_vessel_id)
    digest = None
    stale = False
    if source and target:
        cache_key = tuple(sorted((source.id, target.id)))
        if digest_cache is not None and cache_key in digest_cache:
            digest = digest_cache[cache_key]
        else:
            source_rows = _containers(db, source.id)
            target_rows = _containers(db, target.id)
            gates = _active_gates(db, [c.id for c in source_rows + target_rows])
            digest = _evidence_digest(source, target, source_rows + target_rows, gates)
            if digest_cache is not None:
                digest_cache[cache_key] = digest
        stale = trial.status == "calculated" and digest != trial.evidence_digest
    return {
        "id": trial.id,
        "trial_no": trial.trial_no,
        "status": trial.status,
        "source_vessel_id": trial.source_vessel_id,
        "source_call_no": source.call_no if source else "",
        "target_vessel_id": trial.target_vessel_id,
        "target_call_no": target.call_no if target else "",
        "container_ids": trial.container_ids,
        "evaluation": trial.evaluation,
        "evidence_digest": trial.evidence_digest,
        "current_evidence_digest": digest,
        "stale": stale,
        "created_by": trial.created_by,
        "created_at": trial.created_at.isoformat() if trial.created_at else None,
        "confirmed_by": trial.confirmed_by,
        "confirmed_at": trial.confirmed_at.isoformat() if trial.confirmed_at else None,
        "result": trial.result,
    }


def get_trial(db: Session, trial_id: int) -> RehangTrial:
    trial = db.get(RehangTrial, trial_id)
    if not trial:
        raise HTTPException(404, "试算记录不存在")
    return trial


def list_trials(db: Session, source_vessel_id=None, target_vessel_id=None, limit=100):
    stmt = select(RehangTrial).order_by(RehangTrial.id.desc()).limit(limit)
    if source_vessel_id:
        stmt = stmt.where(RehangTrial.source_vessel_id == source_vessel_id)
    if target_vessel_id:
        stmt = stmt.where(RehangTrial.target_vessel_id == target_vessel_id)
    digest_cache: dict = {}
    return [trial_public(trial, db, digest_cache) for trial in db.execute(stmt).scalars().all()]


def recalculate(db: Session, user, trial_id: int) -> RehangTrial:
    if user.role != "supervisor":
        raise HTTPException(403, "只有主管可以重新试算")
    old = get_trial(db, trial_id)
    if old.status == "confirmed":
        raise HTTPException(409, "已确认改挂的试算不能重算")
    fresh = create_trial(db, user, old.source_vessel_id, old.target_vessel_id, list(old.container_ids))
    old = db.get(RehangTrial, old.id)
    previous_status = old.status
    old.status = "superseded"
    old.superseded_by = fresh.id
    _log(db, user, "rehang_trial_superseded", old,
         f"superseded_by={fresh.id}; previous={previous_status}")
    db.commit()
    return fresh


def _stored_result(trial: RehangTrial) -> dict:
    result = dict(trial.result or {})
    result["replayed"] = True
    return result


def _invalidate(db: Session, user, trial: RehangTrial, reason: str):
    trial.status = "invalidated"
    _log(db, user, "rehang_trial_invalidated", trial, reason)
    db.commit()


def confirm(db: Session, user, trial_id: int, idempotency_key: str, note: str = "") -> dict:
    if user.role != "supervisor":
        raise HTTPException(403, "只有主管可以确认改挂")
    key = (idempotency_key or "").strip()
    if len(key) < 8 or len(key) > 80:
        raise HTTPException(422, "请提供 8-80 位的幂等键，以便中断后安全重试")

    actor = user.display_name or user.username

    # End the read transaction opened by the auth lookup so the evidence
    # re-read below sees the latest committed manifest on every backend
    # (Postgres READ COMMITTED, and snapshot-based SQLite alike).
    db.rollback()

    # Fast path: a retry after a lost response returns the original result.
    prior = db.execute(
        select(RehangConfirmation).where(RehangConfirmation.idempotency_key == key)
    ).scalar_one_or_none()
    if prior:
        if prior.trial_id != trial_id:
            raise HTTPException(409, "幂等键已用于其他试算，请使用新的幂等键")
        return {**prior.result, "replayed": True}

    # Serialize concurrent confirmations of the same trial, then lock both
    # vessel calls.  Locking the calls (always in id order to avoid deadlocks)
    # makes two supervisors confirming overlapping batches take turns: the
    # second one re-reads evidence after the first commits and sees a changed
    # digest instead of acting on a manifest that no longer exists.
    trial = db.execute(
        select(RehangTrial).where(RehangTrial.id == trial_id).with_for_update()
    ).scalar_one_or_none()
    if not trial:
        raise HTTPException(404, "试算记录不存在")
    if trial.status == "confirmed":
        return _stored_result(trial)
    if trial.status in {"superseded", "invalidated"}:
        raise HTTPException(409, {
            "code": "TRIAL_INVALID",
            "message": "该试算已失效或被新试算替代，请重新试算后再确认",
        })
    if trial.status not in ELIGIBLE_STATUSES:
        raise HTTPException(409, {"code": "TRIAL_NOT_CONFIRMABLE", "message": f"试算状态 {trial.status} 不能确认"})

    locked = db.execute(
        select(VesselCall)
        .where(VesselCall.id.in_(sorted({trial.source_vessel_id, trial.target_vessel_id})))
        .order_by(VesselCall.id)
        .with_for_update()
    ).scalars().all()
    if len(locked) != len({trial.source_vessel_id, trial.target_vessel_id}):
        raise HTTPException(409, "船次已不存在，整批保留原船")
    source = db.get(VesselCall, trial.source_vessel_id)
    target = db.get(VesselCall, trial.target_vessel_id)
    if not source or not target:
        raise HTTPException(409, "船次已不存在，整批保留原船")

    # Re-read the evidence now that the calls are locked.  A changed digest
    # means a manifest or active gate record changed after the trial was
    # calculated: the old trial is void.
    live = evaluate(db, source, target, trial.container_ids)
    if live["evidence_digest"] != trial.evidence_digest:
        _invalidate(db, user, trial, "证据在试算后发生变化")
        raise HTTPException(409, {
            "code": "EVIDENCE_CHANGED",
            "message": "箱单或有效闸口记录已变化，旧试算失效，请重新试算后再确认",
        })
    if not live["eligible"]:
        raise HTTPException(409, {"code": "RULE_NOT_MET", "message": live["rule"]})

    # Lock the whole batch and verify every box line by line.  If another
    # confirmation moved any box already, nothing in this batch moves.
    rows = list(db.execute(
        select(ContainerCheck)
        .where(ContainerCheck.id.in_(trial.container_ids))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().all())
    found = {c.id: c for c in rows}
    missing = [cid for cid in trial.container_ids if cid not in found]
    if missing:
        _invalidate(db, user, trial, f"部分箱号已不存在：{missing}")
        raise HTTPException(409, {"code": "BATCH_CONFLICT", "message": f"部分箱号已不存在：{missing}，整批保留原船"})
    displaced = [c.container_no for c in rows if c.vessel_id != source.id]
    if displaced:
        _invalidate(db, user, trial, f"部分箱号已不在原船次：{', '.join(sorted(displaced))}")
        raise HTTPException(409, {
            "code": "BATCH_CONFLICT",
            "message": f"部分箱号已不在原船次：{', '.join(sorted(displaced))}，整批保留原船",
        })

    ordered = sorted(trial.container_ids)
    moved = [found[cid] for cid in ordered]
    # Move with a guarded UPDATE: rows that are still on the source call and
    # still expect the locked version are updated in one statement.  If any
    # row was changed underneath us the count falls short and the whole
    # transaction rolls back, leaving the entire batch on the original call.
    updated = db.execute(
        update(ContainerCheck)
        .where(
            ContainerCheck.id.in_(ordered),
            ContainerCheck.vessel_id == source.id,
        )
        .values(vessel_id=target.id)
        .execution_options(synchronize_session=False)
    ).rowcount
    if updated != len(ordered):
        # Roll back the partial move before recording the invalidation, so no
        # box in a rejected batch changes calls.
        trial_id, reason = trial.id, "整批箱号已被并发改挂，放弃本批"
        db.rollback()
        stale_trial = db.get(RehangTrial, trial_id)
        if stale_trial and stale_trial.status != "confirmed":
            _invalidate(db, user, stale_trial, reason)
        raise HTTPException(409, {
            "code": "BATCH_CONFLICT",
            "message": "部分箱号已被并发改挂，整批保留原船，请重新试算",
        })

    result = {
        "trial_id": trial.id,
        "trial_no": trial.trial_no,
        "status": "confirmed",
        "source_vessel_id": source.id,
        "source_call_no": source.call_no,
        "target_vessel_id": target.id,
        "target_call_no": target.call_no,
        "container_ids": ordered,
        "container_nos": [c.container_no for c in moved],
        "moved_count": len(moved),
        "before": {
            "source_severe_probability": trial.evaluation["source"]["before"]["severe_probability"],
            "target_severe_probability": trial.evaluation["target"]["before"]["severe_probability"],
        },
        "after": {
            "source_severe_probability": trial.evaluation["source"]["after"]["severe_probability"],
            "target_severe_probability": trial.evaluation["target"]["after"]["severe_probability"],
        },
        "model_version": trial.evaluation["model_version"],
        "confirmed_by": actor,
        "confirmed_at": _now().isoformat(),
        "note": note.strip()[:500],
        "replayed": False,
    }

    trial.status = "confirmed"
    trial.confirmed_by = actor
    trial.confirmed_at = _now()
    trial.result = result
    db.add(RehangConfirmation(
        trial_id=trial.id,
        idempotency_key=key,
        actor=user.username,
        result=result,
    ))
    _log(db, user, "rehang_confirmed", trial,
         json.dumps({"containers": len(moved), "key": key}, ensure_ascii=False))
    try:
        db.commit()
    except IntegrityError:
        # A concurrent request used the same key first; replay its result.
        db.rollback()
        prior = db.execute(
            select(RehangConfirmation).where(RehangConfirmation.idempotency_key == key)
        ).scalar_one_or_none()
        if prior and prior.trial_id == trial_id:
            return {**prior.result, "replayed": True}
        if prior:
            raise HTTPException(409, "幂等键已用于其他试算，请使用新的幂等键")
        raise HTTPException(409, "确认并发冲突，请刷新后重试")
    return result
