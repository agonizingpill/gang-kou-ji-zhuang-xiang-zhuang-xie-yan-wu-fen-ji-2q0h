from collections import Counter
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import ContainerCheck, GateEvent, VesselCall

def active_gate_events(db: Session, vessel_id: int):
    return db.execute(
        select(GateEvent).join(ContainerCheck).where(
            ContainerCheck.vessel_id == vessel_id,
            GateEvent.status == "active",
        )
    ).scalars().all()

def lock_vessel(db: Session, vessel_id: int):
    """Take the per-call write lock used by re-hang confirmation.

    Manifest and gate mutations for a call take this same row lock, so they
    either finish before a confirmation reads its evidence or wait until the
    confirmation commits.  That keeps "evidence unchanged" checks honest.
    """
    return db.execute(
        select(VesselCall).where(VesselCall.id == vessel_id).with_for_update()
    ).scalar_one_or_none()

def features_from_records(vessel: VesselCall, containers, gates) -> dict:
    """Compute the manual cross features from an explicit manifest/gate set.

    Re-hang trials use this to evaluate a hypothetical manifest without writing
    anything, while the normal triage path passes the live records.
    """
    containers = list(containers)
    gates = list(gates)
    dwell = [g.dwell_min for g in gates]
    avg_dwell = round(sum(dwell) / len(dwell), 2) if dwell else 0
    reefer = sum(1 for c in containers if c.iso_type.startswith("R"))
    hazardous = sum(1 for c in containers if c.cargo_class in {"hazmat", "chemical"})
    customs_open = sum(1 for c in containers if c.customs_status != "finished")
    cargo_counts = Counter(c.cargo_class for c in containers)
    return {
        "container_count": len(containers),
        "avg_gate_dwell_min": avg_dwell,
        "max_gate_dwell_min": max(dwell) if dwell else 0,
        "reefer_ratio": round(reefer / max(1, len(containers)), 3),
        "hazmat_ratio": round(hazardous / max(1, len(containers)), 3),
        "customs_open_count": customs_open,
        "gate_event_count": len(gates),
        "dry_count": cargo_counts.get("dry", 0),
        "reefer_count": cargo_counts.get("reefer", 0),
        "hazmat_count": cargo_counts.get("hazmat", 0),
        "port_call_hour": vessel.eta.hour,
        "window_minutes": max(1, int((vessel.etd - vessel.eta).total_seconds() / 60)),
    }

def duplicate_gate_refs(db: Session, vessel_id: int):
    rows = active_gate_events(db, vessel_id)
    counts = Counter((r.container_id, r.event_type, r.event_time.isoformat()) for r in rows)
    return [key for key, count in counts.items() if count > 1]

def build_features(db: Session, vessel: VesselCall):
    return features_from_records(vessel, vessel.containers, active_gate_events(db, vessel.id))

def domain_precheck(db: Session, vessel: VesselCall):
    features = build_features(db, vessel)
    issues = []
    if features["customs_open_count"] > 0:
        issues.append(f"{features['customs_open_count']} 个箱号海关查验未结束")
    duplicates = duplicate_gate_refs(db, vessel.id)
    if duplicates:
        labels = [f"container_id={cid}/event_type={event_type}/time={when}" for cid, event_type, when in duplicates]
        issues.append("重复闸口记录需先冲正：" + ", ".join(labels))
    return features, issues
