from datetime import datetime

from ..models import SyncJob, SyncJobItem, SyncSource, db
from .sync_events import log_sync_event

JOB_TERMINAL = {'completed', 'completed_with_errors', 'failed', 'cancelled'}


def create_sync_job(source: SyncSource, trigger_type: str = 'manual') -> SyncJob:
    row = SyncJob(
        source_id=source.id,
        trigger_type=trigger_type or 'manual',
        state='queued',
        started_at=None,
        finished_at=None,
    )
    db.session.add(row)
    db.session.flush()
    log_sync_event(event_type='sync.job.created', message='Sync job created', source_id=source.id, job_id=row.id)
    return row


def set_job_state(job: SyncJob, state: str, error_summary: str | None = None) -> None:
    if job.state in JOB_TERMINAL and state not in JOB_TERMINAL:
        return
    now = datetime.utcnow()
    if state in {'scanning', 'ingesting'} and not job.started_at:
        job.started_at = now
    if state in JOB_TERMINAL:
        job.finished_at = now
    job.state = state
    if error_summary is not None:
        job.error_summary = error_summary
    job.updated_at = now
    log_sync_event(event_type='sync.job.state', message=f'Job state -> {state}', source_id=job.source_id, job_id=job.id, payload={'state': state})


def add_manifest_items(job: SyncJob, items: list[dict]) -> tuple[int, int]:
    accepted = 0
    rejected = 0
    allowed_initial_states = {'awaiting_upload', 'uploaded_temp'}
    for item in items:
        source_rel_path = str(item.get('source_rel_path') or '').strip()
        filename = str(item.get('filename') or '').strip()
        size_bytes = int(item.get('size_bytes') or 0)
        mtime_raw = item.get('mtime_utc')
        mtime = None
        if mtime_raw:
            try:
                mtime = datetime.fromisoformat(str(mtime_raw).replace('Z', '+00:00')).replace(tzinfo=None)
            except Exception:
                mtime = None
        if not source_rel_path or not filename or size_bytes <= 0:
            rejected += 1
            continue
        exists = SyncJobItem.query.filter_by(
            job_id=job.id,
            source_rel_path=source_rel_path,
            size_bytes=size_bytes,
            mtime_utc=mtime,
        ).first()
        if exists:
            rejected += 1
            continue
        initial_state = str(item.get('initial_state') or 'awaiting_upload').strip().lower()
        if initial_state not in allowed_initial_states:
            initial_state = 'awaiting_upload'
        row = SyncJobItem(
            job_id=job.id,
            client_item_id=str(item.get('client_item_id') or ''),
            source_rel_path=source_rel_path,
            filename=filename,
            size_bytes=size_bytes,
            mime_type=str(item.get('mime_type') or '') or None,
            mtime_utc=mtime,
            quick_fingerprint=str(item.get('quick_fingerprint') or '') or None,
            state=initial_state,
            uploaded_blob_path=str(item.get('uploaded_blob_path') or '') or None,
        )
        db.session.add(row)
        accepted += 1

    job.total_items = int(job.total_items or 0) + accepted
    job.updated_at = datetime.utcnow()
    log_sync_event(
        event_type='sync.job.manifest',
        message='Manifest items added',
        source_id=job.source_id,
        job_id=job.id,
        payload={'accepted': accepted, 'rejected': rejected},
    )
    return accepted, rejected


def recompute_job_counters(job: SyncJob) -> None:
    rows = SyncJobItem.query.filter_by(job_id=job.id).all()
    processed = 0
    ingested = 0
    deduped = 0
    failed = 0
    for row in rows:
        if row.state in {'completed', 'linked_existing', 'failed', 'skipped'}:
            processed += 1
        if row.state == 'completed':
            ingested += 1
        if row.state == 'linked_existing':
            deduped += 1
        if row.state == 'failed':
            failed += 1
    job.processed_items = processed
    job.ingested_items = ingested
    job.deduped_items = deduped
    job.failed_items = failed
    job.updated_at = datetime.utcnow()

    if rows and processed >= len(rows):
        job.state = 'completed_with_errors' if failed else 'completed'
        if not job.finished_at:
            job.finished_at = datetime.utcnow()
    elif any(r.state in {'uploaded_temp', 'hashing', 'dedupe_check', 'storing'} for r in rows):
        job.state = 'ingesting'
    elif any(r.state == 'awaiting_upload' for r in rows):
        job.state = 'scanning'
