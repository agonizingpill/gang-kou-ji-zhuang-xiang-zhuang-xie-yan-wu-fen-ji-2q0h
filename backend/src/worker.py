import os, time
from datetime import datetime, timezone
from pathlib import Path
from redis import Redis
from .database import Base, SessionLocal, engine
from .model import predict
from .models import AnalysisJob, DelayCase

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
queue = Redis.from_url(REDIS_URL, decode_responses=True)

def run_job(job_id):
    db = SessionLocal()
    job = db.get(AnalysisJob, int(job_id))
    if not job:
        db.close(); return
    case = db.get(DelayCase, job.case_id)
    try:
        job.status = "running"; db.commit()
        result = predict(dict(job.input_summary or {}))
        label = result["label"]
        job.result = result; job.status = "succeeded"; job.finished_at = datetime.now(timezone.utc)
        case.model_output = result; case.final_level = label; case.status = "classified"; case.exception_reason = None
        db.commit()
    except Exception as exc:
        job.status = "failed"; job.error = str(exc); job.finished_at = datetime.now(timezone.utc); db.commit()
    finally:
        db.close()

def main():
    Base.metadata.create_all(engine)
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        Path(SHARED_DIR / "worker-heartbeat").touch()
        item = queue.blpop("container_delay_triage_jobs", timeout=3)
        if item:
            run_job(item[1])
        time.sleep(1)

if __name__ == "__main__":
    main()
