"""Supervisor re-hang (改挂) trial workbench endpoints."""

from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from . import rehang_service as service
from .security import get_db, require_roles

router = APIRouter(prefix="/api/rehang", tags=["改挂工作台"])


class TrialIn(BaseModel):
    source_vessel_id: int
    target_vessel_id: int
    container_ids: list[int] = Field(min_length=1, max_length=200)


class ConfirmIn(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=80)
    note: str = Field(default="", max_length=500)


@router.get("/trials")
def list_trials(
    source_vessel_id: int | None = Query(default=None),
    target_vessel_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    user=Depends(require_roles("operator", "supervisor", "viewer")),
):
    return service.list_trials(db, source_vessel_id, target_vessel_id, limit)


@router.post("/trials", status_code=201)
def create_trial(
    payload: TrialIn,
    db: Session = Depends(get_db),
    user=Depends(require_roles("supervisor")),
):
    trial = service.create_trial(
        db, user, payload.source_vessel_id, payload.target_vessel_id, payload.container_ids
    )
    return service.trial_public(trial, db)


@router.get("/trials/{trial_id}")
def get_trial(
    trial_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_roles("operator", "supervisor", "viewer")),
):
    return service.trial_public(service.get_trial(db, trial_id), db)


@router.post("/trials/{trial_id}/recalculate", status_code=201)
def recalculate(
    trial_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_roles("supervisor")),
):
    trial = service.recalculate(db, user, trial_id)
    return service.trial_public(trial, db)


@router.post("/trials/{trial_id}/confirm")
def confirm(
    trial_id: int,
    payload: ConfirmIn,
    db: Session = Depends(get_db),
    user=Depends(require_roles("supervisor")),
):
    return service.confirm(db, user, trial_id, payload.idempotency_key, payload.note)
