"""End-to-end tests for the re-hang trial workbench.

Run: python3 -m pytest backend/tests/test_rehang.py -q
"""

import os
import sys
import tempfile
import warnings
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DB_PATH = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SEED_DEMO"] = "false"
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402

from src.database import Base, SessionLocal, engine  # noqa: E402
from src.main import app  # noqa: E402
from src.models import ContainerCheck, GateEvent, VesselCall  # noqa: E402
from scripts.seed_demo import seed  # noqa: E402


def _reset_ledger():
    # Each test starts from the pristine demo ledger so trials never inherit
    # boxes moved by an earlier test.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    seed()


@pytest.fixture()
def client():
    _reset_ledger()
    with TestClient(app) as test_client:
        yield test_client


def login(client, username, password):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def supervisor(client):
    return login(client, "supervisor", "supervisor123")


@pytest.fixture()
def operator(client):
    return login(client, "operator", "operator123")


@pytest.fixture()
def viewer(client):
    return login(client, "viewer", "viewer123")


@pytest.fixture()
def fresh_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def vessel_ids(db):
    rows = db.query(VesselCall).all()
    return {v.call_no: v.id for v in rows}


def container_ids(db, call_no):
    vessel = db.query(VesselCall).filter_by(call_no=call_no).one()
    return [c.id for c in sorted(vessel.containers, key=lambda c: c.id)]


def test_trial_requires_supervisor(client, operator, fresh_db):
    vs = vessel_ids(fresh_db)
    response = client.post("/api/rehang/trials", headers=operator, json={
        "source_vessel_id": vs["CALL-HAI-20261121"],
        "target_vessel_id": vs["CALL-LINK-20261121"],
        "container_ids": container_ids(fresh_db, "CALL-HAI-20261121")[:1],
    })
    assert response.status_code == 403


def test_trial_rejects_boxes_from_other_call(client, supervisor, fresh_db):
    vs = vessel_ids(fresh_db)
    response = client.post("/api/rehang/trials", headers=supervisor, json={
        "source_vessel_id": vs["CALL-HAI-20261121"],
        "target_vessel_id": vs["CALL-LINK-20261121"],
        "container_ids": container_ids(fresh_db, "CALL-TRI-20261121")[:1],
    })
    assert response.status_code == 409


def test_eligible_trial_scores_both_calls_and_confirms_atomically(client, supervisor, fresh_db):
    vs = vessel_ids(fresh_db)
    batch = container_ids(fresh_db, "CALL-HAI-20261121")[:2]
    created = client.post("/api/rehang/trials", headers=supervisor, json={
        "source_vessel_id": vs["CALL-HAI-20261121"],
        "target_vessel_id": vs["CALL-LINK-20261121"],
        "container_ids": batch,
    })
    assert created.status_code == 201, created.text
    trial = created.json()
    assert trial["evaluation"]["eligible"] is True
    source = trial["evaluation"]["source"]
    target = trial["evaluation"]["target"]
    assert source["after"]["severe_probability"] < source["before"]["severe_probability"]
    assert target["after"]["severe_probability"] <= target["before"]["severe_probability"]
    assert trial["stale"] is False

    confirmed = client.post(f"/api/rehang/trials/{trial['id']}/confirm", headers=supervisor, json={
        "idempotency_key": "confirm-hai-link-0001",
        "note": "夜间窗口可接驳",
    })
    assert confirmed.status_code == 200, confirmed.text
    result = confirmed.json()
    assert result["moved_count"] == 2
    assert result["replayed"] is False

    db = SessionLocal()
    try:
        moved = db.query(ContainerCheck).filter(ContainerCheck.id.in_(batch)).all()
        assert all(c.vessel_id == vs["CALL-LINK-20261121"] for c in moved)
    finally:
        db.close()

    # Retrying with the same key replays the original result and moves nothing.
    replay = client.post(f"/api/rehang/trials/{trial['id']}/confirm", headers=supervisor, json={
        "idempotency_key": "confirm-hai-link-0001",
        "note": "夜间窗口可接驳",
    })
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True

    # A retry with a fresh key on a confirmed trial also replays.
    replay2 = client.post(f"/api/rehang/trials/{trial['id']}/confirm", headers=supervisor, json={
        "idempotency_key": "confirm-hai-link-0002",
    })
    assert replay2.status_code == 200
    assert replay2.json()["replayed"] is True

    listing = client.get("/api/rehang/trials", headers=supervisor)
    assert listing.status_code == 200
    assert any(row["id"] == trial["id"] and row["status"] == "confirmed" for row in listing.json())


def test_manifest_change_invalidates_open_trial(client, supervisor, fresh_db, operator):
    vs = vessel_ids(fresh_db)
    batch = container_ids(fresh_db, "CALL-NORTH-20261121")[:1]
    trial = client.post("/api/rehang/trials", headers=supervisor, json={
        "source_vessel_id": vs["CALL-NORTH-20261121"],
        "target_vessel_id": vs["CALL-LINK-20261121"],
        "container_ids": batch,
    }).json()
    assert trial["evaluation"]["eligible"] is True

    # An operator adds a box to the source call while the trial is open.
    added = client.post("/api/containers", headers=operator, json={
        "vessel_id": vs["CALL-NORTH-20261121"],
        "container_no": "MSCU499999",
        "iso_type": "22G1",
        "cargo_class": "dry",
        "weight_t": 18.0,
        "customs_status": "finished",
        "seal_status": "intact",
    })
    assert added.status_code == 200, added.text

    stale = client.get(f"/api/rehang/trials/{trial['id']}", headers=supervisor).json()
    assert stale["stale"] is True

    blocked = client.post(f"/api/rehang/trials/{trial['id']}/confirm", headers=supervisor, json={
        "idempotency_key": "confirm-north-stale-0001",
    })
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "EVIDENCE_CHANGED"

    still_home = SessionLocal()
    try:
        assert still_home.get(ContainerCheck, batch[0]).vessel_id == vs["CALL-NORTH-20261121"]
    finally:
        still_home.close()

    # The stale trial cannot be confirmed; a fresh calculation is required.
    recalculated = client.post(f"/api/rehang/trials/{trial['id']}/recalculate", headers=supervisor)
    assert recalculated.status_code == 201, recalculated.text
    fresh = recalculated.json()
    assert fresh["id"] != trial["id"]
    assert fresh["stale"] is False
    assert client.get(f"/api/rehang/trials/{trial['id']}", headers=supervisor).json()["status"] == "superseded"


def test_gate_record_change_invalidates_open_trial(client, supervisor, fresh_db, operator):
    vs = vessel_ids(fresh_db)
    batch = container_ids(fresh_db, "CALL-NORTH-20261121")[:1]
    trial = client.post("/api/rehang/trials", headers=supervisor, json={
        "source_vessel_id": vs["CALL-NORTH-20261121"],
        "target_vessel_id": vs["CALL-LINK-20261121"],
        "container_ids": batch,
    }).json()

    gate = client.post("/api/gates", headers=operator, json={
        "container_id": batch[0],
        "event_ref": "GATE-N-LATE-01",
        "gate_code": "G03",
        "event_type": "in",
        "event_time": datetime(2026, 11, 21, 12, 5, tzinfo=timezone.utc).isoformat(),
        "dwell_min": 15,
    })
    assert gate.status_code == 200, gate.text

    assert client.get(f"/api/rehang/trials/{trial['id']}", headers=supervisor).json()["stale"] is True
    blocked = client.post(f"/api/rehang/trials/{trial['id']}/confirm", headers=supervisor, json={
        "idempotency_key": "confirm-north-gate-0001",
    })
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "EVIDENCE_CHANGED"


def test_ineligible_batch_cannot_be_confirmed(client, supervisor, fresh_db):
    vs = vessel_ids(fresh_db)
    # Moving a hazmat box out of PEARL raises the target call's hazmat ratio;
    # we search for an ineligible batch to assert the rule is enforced.
    src = fresh_db.query(VesselCall).filter_by(call_no="CALL-PEARL-20261121").one()
    ids = [c.id for c in sorted(src.containers, key=lambda c: c.id)]
    picked = None
    for cid in ids:
        trial = client.post("/api/rehang/trials", headers=supervisor, json={
            "source_vessel_id": vs["CALL-PEARL-20261121"],
            "target_vessel_id": vs["CALL-HAI-20261121"],
            "container_ids": [cid],
        })
        if trial.status_code == 201 and trial.json()["evaluation"]["eligible"] is False:
            picked = trial.json()
            break
    assert picked is not None
    blocked = client.post(f"/api/rehang/trials/{picked['id']}/confirm", headers=supervisor, json={
        "idempotency_key": f"confirm-ineligible-{picked['id']:04d}",
    })
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "RULE_NOT_MET"


def test_two_supervisors_overlapping_batches_second_is_rejected(client, supervisor, fresh_db):
    """Two trials created before either is confirmed: confirming one changes the
    evidence, so the other must not move its remaining boxes or half a batch."""
    vs = vessel_ids(fresh_db)
    boxes = container_ids(fresh_db, "CALL-HAI-20261121")
    first = client.post("/api/rehang/trials", headers=supervisor, json={
        "source_vessel_id": vs["CALL-HAI-20261121"],
        "target_vessel_id": vs["CALL-LINK-20261121"],
        "container_ids": boxes[:2],
    }).json()
    second = client.post("/api/rehang/trials", headers=supervisor, json={
        "source_vessel_id": vs["CALL-HAI-20261121"],
        "target_vessel_id": vs["CALL-LINK-20261121"],
        "container_ids": [boxes[1], boxes[2]],
    }).json()
    assert first["evaluation"]["eligible"] is True
    assert second["evaluation"]["eligible"] is True

    ok = client.post(f"/api/rehang/trials/{first['id']}/confirm", headers=supervisor, json={
        "idempotency_key": "overlap-first-0001",
    })
    assert ok.status_code == 200

    rejected = client.post(f"/api/rehang/trials/{second['id']}/confirm", headers=supervisor, json={
        "idempotency_key": "overlap-second-0001",
    })
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "EVIDENCE_CHANGED"

    db = SessionLocal()
    try:
        # The untouched box in the second batch stays on the original call.
        assert db.get(ContainerCheck, boxes[2]).vessel_id == vs["CALL-HAI-20261121"]
        moved = db.query(ContainerCheck).filter(ContainerCheck.id.in_(boxes[:2])).all()
        assert all(c.vessel_id == vs["CALL-LINK-20261121"] for c in moved)
    finally:
        db.close()
    state = client.get(f"/api/rehang/trials/{second['id']}", headers=supervisor).json()
    assert state["status"] == "invalidated"
    assert state["stale"] is False


def test_idempotency_key_cannot_be_reused_for_another_trial(client, supervisor, fresh_db):
    vs = vessel_ids(fresh_db)
    boxes = container_ids(fresh_db, "CALL-HAI-20261121")
    one = client.post("/api/rehang/trials", headers=supervisor, json={
        "source_vessel_id": vs["CALL-HAI-20261121"],
        "target_vessel_id": vs["CALL-LINK-20261121"],
        "container_ids": [boxes[0]],
    }).json()
    two = client.post("/api/rehang/trials", headers=supervisor, json={
        "source_vessel_id": vs["CALL-HAI-20261121"],
        "target_vessel_id": vs["CALL-LINK-20261121"],
        "container_ids": [boxes[1]],
    }).json()
    assert client.post(f"/api/rehang/trials/{one['id']}/confirm", headers=supervisor, json={
        "idempotency_key": "same-key-two-trials",
    }).status_code == 200
    conflict = client.post(f"/api/rehang/trials/{two['id']}/confirm", headers=supervisor, json={
        "idempotency_key": "same-key-two-trials",
    })
    assert conflict.status_code == 409


def test_confirm_requires_an_idempotency_key(client, supervisor, fresh_db):
    vs = vessel_ids(fresh_db)
    trial = client.post("/api/rehang/trials", headers=supervisor, json={
        "source_vessel_id": vs["CALL-HAI-20261121"],
        "target_vessel_id": vs["CALL-LINK-20261121"],
        "container_ids": container_ids(fresh_db, "CALL-HAI-20261121")[:1],
    }).json()
    missing = client.post(f"/api/rehang/trials/{trial['id']}/confirm", headers=supervisor, json={})
    assert missing.status_code == 422


def test_rejected_batch_never_partially_moves(client, supervisor, fresh_db):
    """If a box was moved by another confirmation after trial creation, the whole
    batch must stay on the original call — including boxes the guarded update
    could still have touched."""
    vs = vessel_ids(fresh_db)
    boxes = container_ids(fresh_db, "CALL-HAI-20261121")
    trial = client.post("/api/rehang/trials", headers=supervisor, json={
        "source_vessel_id": vs["CALL-HAI-20261121"],
        "target_vessel_id": vs["CALL-LINK-20261121"],
        "container_ids": boxes[:2],
    }).json()
    assert trial["evaluation"]["eligible"] is True

    # Simulate a competing confirmation moving only the middle box, leaving the
    # digest unchanged from this trial's perspective only for the other calls.
    db = SessionLocal()
    try:
        container = db.get(ContainerCheck, boxes[1])
        container.vessel_id = vs["CALL-TRI-20261121"]
        db.commit()
    finally:
        db.close()

    rejected = client.post(f"/api/rehang/trials/{trial['id']}/confirm", headers=supervisor, json={
        "idempotency_key": "reject-partial-0001",
    })
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] in {"EVIDENCE_CHANGED", "BATCH_CONFLICT"}

    db = SessionLocal()
    try:
        for box_id in boxes[:2]:
            assert db.get(ContainerCheck, box_id).vessel_id != vs["CALL-LINK-20261121"]
        # The box the guarded update could have touched is back home.
        assert db.get(ContainerCheck, boxes[0]).vessel_id == vs["CALL-HAI-20261121"]
    finally:
        db.close()


def test_viewer_can_read_but_not_act(client, viewer, supervisor, fresh_db):
    listing = client.get("/api/rehang/trials", headers=viewer)
    assert listing.status_code == 200
    assert listing.json() == []
    vs = vessel_ids(fresh_db)
    created = client.post("/api/rehang/trials", headers=supervisor, json={
        "source_vessel_id": vs["CALL-HAI-20261121"],
        "target_vessel_id": vs["CALL-LINK-20261121"],
        "container_ids": container_ids(fresh_db, "CALL-HAI-20261121")[:1],
    })
    assert created.status_code == 201
    assert client.get("/api/rehang/trials", headers=viewer).json()
    denied = client.post("/api/rehang/trials", headers=viewer, json={
        "source_vessel_id": vs["CALL-HAI-20261121"],
        "target_vessel_id": vs["CALL-LINK-20261121"],
        "container_ids": container_ids(fresh_db, "CALL-HAI-20261121")[:1],
    })
    assert denied.status_code == 403
    assert client.post(
        f"/api/rehang/trials/{created.json()['id']}/confirm",
        headers=viewer,
        json={"idempotency_key": "viewer-should-not-confirm"},
    ).status_code == 403
