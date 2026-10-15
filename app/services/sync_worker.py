import os
import threading
import time
from datetime import datetime

from .. import db
from ..models import SyncJob, SyncJobItem
from .media_dedupe import resolve_or_create_asset
from .media_fingerprint import quick_fingerprint, sha256_file
from .media_storage import copy_source_to_photo_storage, get_sync_temp_folder, move_temp_to_photo_storage
from .sync_engine import recompute_job_counters
from .sync_events import log_sync_event

_worker_lock = threading.Lock()
_worker_started = False
_stop_event = threading.Event()


def _process_item(item: SyncJobItem) -> None:
    item.attempts = int(item.attempts or 0) + 1
    item.state = 'hashing'
    item.updated_at = datetime.utcnow()
    db.session.commit()

    if not item.uploaded_blob_path or not os.path.exists(item.uploaded_blob_path):
        item.state = 'failed'
        item.last_error = 'Uploaded blob missing'
        db.session.commit()
        return

    blob_path = os.path.abspath(item.uploaded_blob_path)
    sync_temp_root = os.path.abspath(get_sync_temp_folder())
    is_temp_blob = blob_path.startswith(sync_temp_root + os.sep) or blob_path == sync_temp_root

    try:
        sha = sha256_file(item.uploaded_blob_path)
        qf = quick_fingerprint(item.uploaded_blob_path)
        item.content_sha256 = sha
        item.quick_fingerprint = qf
        item.state = 'dedupe_check'
        item.updated_at = datetime.utcnow()
        db.session.commit()

        mime = item.mime_type
        is_video = (mime or '').lower().startswith('video/')

        item.state = 'storing'
        db.session.commit()

        target_filename = None
        try:
            outcome = resolve_or_create_asset(
                sha256=sha,
                file_size=item.size_bytes,
                mime_type=mime,
                is_video=is_video,
                album_id=None,
                original_filename=item.filename,
                uploader='sync',
                visibility='family',
                storage_filename=None,
                source_device_id=None,
                source_path=item.source_rel_path,
                source_mtime_utc=item.mtime_utc,
                ingest_job_id=item.job_id,
                origin_kind='device_sync',
            )
        except ValueError:
            if is_temp_blob:
                target_filename, _ = move_temp_to_photo_storage(item.uploaded_blob_path, item.filename)
            else:
                target_filename, _ = copy_source_to_photo_storage(item.uploaded_blob_path, item.filename)
            outcome = resolve_or_create_asset(
                sha256=sha,
                file_size=item.size_bytes,
                mime_type=mime,
                is_video=is_video,
                album_id=None,
                original_filename=item.filename,
                uploader='sync',
                visibility='family',
                storage_filename=target_filename,
                source_device_id=None,
                source_path=item.source_rel_path,
                source_mtime_utc=item.mtime_utc,
                ingest_job_id=item.job_id,
                origin_kind='device_sync',
            )

        if outcome['action'] == 'linked_existing':
            if is_temp_blob:
                try:
                    os.remove(item.uploaded_blob_path)
                except Exception:
                    pass
            item.state = 'linked_existing'
        else:
            item.state = 'completed'
        item.photo_asset_id = int(outcome['asset'].id)
        item.updated_at = datetime.utcnow()
        db.session.commit()
    except Exception as exc:
        item.state = 'failed'
        item.last_error = str(exc)
        item.updated_at = datetime.utcnow()
        db.session.commit()


def _loop(app):
    with app.app_context():
        while not _stop_event.is_set():
            try:
                if not process_next_sync_item():
                    time.sleep(1.0)
                    continue
            except Exception:
                db.session.rollback()
                time.sleep(1.0)


def start_sync_worker(app):
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return False
        _worker_started = True
    thread = threading.Thread(target=_loop, args=(app,), daemon=True, name='homehub-sync-worker')
    thread.start()
    return True


def process_next_sync_item() -> bool:
    item = SyncJobItem.query.filter(SyncJobItem.state == 'uploaded_temp').order_by(SyncJobItem.updated_at.asc(), SyncJobItem.id.asc()).first()
    if not item:
        return False

    _process_item(item)
    job = SyncJob.query.get(item.job_id)
    if job:
        recompute_job_counters(job)
        db.session.commit()
        log_sync_event(event_type='sync.item.processed', message='Sync item processed', job_id=job.id, item_id=item.id, source_id=job.source_id, payload={'state': item.state})
    return True
