import json
from datetime import datetime

from ..models import SyncEventLog, SyncJob, SyncJobItem, SyncSource, db


def log_sync_event(*, event_type: str, message: str, level: str = 'info', job_id: int | None = None,
                   item_id: int | None = None, source_id: int | None = None, payload: dict | None = None) -> SyncEventLog:
    row = SyncEventLog(
        event_type=event_type,
        message=message,
        level=level,
        job_id=job_id,
        item_id=item_id,
        source_id=source_id,
        payload_json=json.dumps(payload or {}),
    )
    db.session.add(row)
    return row


def latest_sync_markers() -> dict:
    latest_job = db.session.query(SyncJob.id).order_by(SyncJob.updated_at.desc(), SyncJob.id.desc()).first()
    latest_item = db.session.query(SyncJobItem.id).order_by(SyncJobItem.updated_at.desc(), SyncJobItem.id.desc()).first()
    latest_source = db.session.query(SyncSource.id).order_by(SyncSource.updated_at.desc(), SyncSource.id.desc()).first()
    return {
        'job_latest_id': int(latest_job[0]) if latest_job else 0,
        'item_latest_id': int(latest_item[0]) if latest_item else 0,
        'source_latest_id': int(latest_source[0]) if latest_source else 0,
    }
