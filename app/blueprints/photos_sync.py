from datetime import datetime
import os

from flask import jsonify, request, session

from .. import db
from ..blueprints import main_bp
from ..models import SyncDevice, SyncEventLog, SyncJob, SyncJobItem, SyncSource
from ..security import sanitize_text
from ..services.media_fingerprint import save_upload_to_temp
from ..services.media_storage import get_sync_temp_folder
from ..services.source_scanners import scan_server_path
from ..services.sync_engine import add_manifest_items, create_sync_job, recompute_job_counters, set_job_state
from ..services.sync_events import log_sync_event


ALLOWED_SOURCE_TYPES = {'browser_import', 'android_library', 'ios_library', 'server_path', 'legacy_watch_folder'}
ALLOWED_PLATFORMS = {'web', 'android', 'ios', 'server'}
ALLOWED_SCOPES = {'private', 'family'}


def _current_member_name() -> str:
    raw = sanitize_text(session.get('current_member') or '').strip()
    if raw:
        return raw
    return sanitize_text(request.args.get('member') or '').strip() or 'Administrator'


def _is_admin(member: str) -> bool:
    return member in {'Administrator', 'admin'}


def _source_to_dict(row: SyncSource) -> dict:
    return {
        'id': row.id,
        'device_id': row.device_id,
        'source_type': row.source_type,
        'source_uri': row.source_uri,
        'scope': row.scope,
        'target_album_id': row.target_album_id,
        'is_active': bool(row.is_active),
        'requires_client_scan': bool(row.requires_client_scan),
        'source_cursor': row.source_cursor,
        'last_scan_at': row.last_scan_at.isoformat() if row.last_scan_at else None,
        'last_success_at': row.last_success_at.isoformat() if row.last_success_at else None,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


def _job_to_dict(row: SyncJob) -> dict:
    total = int(row.total_items or 0)
    processed = int(row.processed_items or 0)
    progress = int((processed / total) * 100) if total > 0 else 0
    return {
        'id': row.id,
        'source_id': row.source_id,
        'trigger_type': row.trigger_type,
        'state': row.state,
        'total_items': total,
        'processed_items': processed,
        'ingested_items': int(row.ingested_items or 0),
        'deduped_items': int(row.deduped_items or 0),
        'failed_items': int(row.failed_items or 0),
        'progress_percent': progress,
        'started_at': row.started_at.isoformat() if row.started_at else None,
        'finished_at': row.finished_at.isoformat() if row.finished_at else None,
        'error_summary': row.error_summary,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


def _item_to_dict(row: SyncJobItem) -> dict:
    return {
        'id': row.id,
        'job_id': row.job_id,
        'client_item_id': row.client_item_id,
        'source_rel_path': row.source_rel_path,
        'filename': row.filename,
        'size_bytes': int(row.size_bytes or 0),
        'mime_type': row.mime_type,
        'mtime_utc': row.mtime_utc.isoformat() if row.mtime_utc else None,
        'quick_fingerprint': row.quick_fingerprint,
        'content_sha256': row.content_sha256,
        'state': row.state,
        'attempts': int(row.attempts or 0),
        'last_error': row.last_error,
        'photo_asset_id': row.photo_asset_id,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


@main_bp.route('/api/photos/sync/sources/register', methods=['POST'])
def api_sync_register_source():
    data = request.get_json(force=True) or {}
    member = sanitize_text(data.get('member') or '').strip() or _current_member_name()
    platform = sanitize_text(data.get('platform') or '').strip().lower()
    source_type = sanitize_text(data.get('source_type') or '').strip().lower()
    source_uri = sanitize_text(data.get('source_uri') or '').strip()
    scope = sanitize_text(data.get('scope') or 'private').strip().lower()
    if platform not in ALLOWED_PLATFORMS:
        return jsonify({'ok': False, 'error': 'Invalid platform'}), 400
    if source_type not in ALLOWED_SOURCE_TYPES:
        return jsonify({'ok': False, 'error': 'Invalid source_type'}), 400
    if scope not in ALLOWED_SCOPES:
        return jsonify({'ok': False, 'error': 'Invalid scope'}), 400
    if not source_uri:
        return jsonify({'ok': False, 'error': 'source_uri is required'}), 400
    if source_type == 'server_path' and (not os.path.isabs(source_uri) or not os.path.exists(source_uri)):
        return jsonify({'ok': False, 'error': 'server_path must exist and be absolute'}), 400

    label = sanitize_text(data.get('device_label') or '').strip() or f"{platform}-{member}"
    app_instance_id = sanitize_text(data.get('app_instance_id') or '').strip() or None
    if platform in {'web', 'android', 'ios'} and not app_instance_id:
        return jsonify({'ok': False, 'error': 'app_instance_id is required for web/android/ios sources'}), 400

    device = None
    if app_instance_id:
        device = SyncDevice.query.filter_by(app_instance_id=app_instance_id).first()
        if device and sanitize_text(device.member_name or '').strip() != member:
            return jsonify({'ok': False, 'error': 'app_instance_id is already bound to a different member'}), 403
    if not device:
        device = SyncDevice.query.filter_by(member_name=member, platform=platform, device_label=label).first()
    if not device:
        device = SyncDevice(
            member_name=member,
            platform=platform,
            device_label=label,
            trust_level='trusted',
            send_enabled=True,
            receive_only=False,
            app_instance_id=app_instance_id,
            last_seen_at=datetime.utcnow(),
        )
        db.session.add(device)
        db.session.flush()
    else:
        device.last_seen_at = datetime.utcnow()
        device.app_instance_id = app_instance_id or device.app_instance_id

    existing = SyncSource.query.filter_by(device_id=device.id, source_type=source_type, source_uri=source_uri).first()
    if existing:
        existing.scope = scope
        existing.target_album_id = data.get('target_album_id')
        existing.is_active = True
        existing.requires_client_scan = source_type != 'server_path'
        existing.updated_at = datetime.utcnow()
        source = existing
    else:
        source = SyncSource(
            device_id=device.id,
            source_type=source_type,
            source_uri=source_uri,
            scope=scope,
            target_album_id=data.get('target_album_id'),
            is_active=True,
            requires_client_scan=(source_type != 'server_path'),
        )
        db.session.add(source)
        db.session.flush()

    log_sync_event(event_type='sync.source.registered', message='Sync source registered', source_id=source.id, payload={'platform': platform, 'source_type': source_type})
    db.session.commit()
    return jsonify({'ok': True, 'device': {'id': device.id, 'member_name': device.member_name, 'platform': device.platform, 'device_label': device.device_label}, 'source': _source_to_dict(source)})


@main_bp.route('/api/photos/sync/sources', methods=['GET'])
def api_sync_list_sources():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    q = db.session.query(SyncSource, SyncDevice).join(SyncDevice, SyncSource.device_id == SyncDevice.id)
    if not _is_admin(member):
        q = q.filter(SyncDevice.member_name == member)
    rows = q.order_by(SyncSource.updated_at.desc(), SyncSource.id.desc()).all()
    return jsonify({'ok': True, 'items': [{**_source_to_dict(src), 'member_name': dev.member_name, 'platform': dev.platform, 'device_label': dev.device_label} for src, dev in rows]})


@main_bp.route('/api/photos/sync/sources/<int:source_id>', methods=['PATCH'])
def api_sync_update_source(source_id: int):
    member = _current_member_name()
    source = SyncSource.query.get_or_404(source_id)
    dev = SyncDevice.query.get(source.device_id)
    if not _is_admin(member) and (not dev or dev.member_name != member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    data = request.get_json(force=True) or {}
    if 'is_active' in data:
        source.is_active = bool(data.get('is_active'))
    if 'scope' in data:
        scope = sanitize_text(data.get('scope') or '').strip().lower()
        if scope in ALLOWED_SCOPES:
            source.scope = scope
    if 'target_album_id' in data:
        raw = data.get('target_album_id')
        source.target_album_id = None if raw in (None, '', 0, '0') else int(raw)
    if 'source_cursor' in data:
        source.source_cursor = str(data.get('source_cursor') or '') or None
    source.updated_at = datetime.utcnow()
    if dev:
        dev.last_seen_at = datetime.utcnow()
    log_sync_event(event_type='sync.source.updated', message='Sync source updated', source_id=source.id)
    db.session.commit()
    return jsonify({'ok': True, 'source': _source_to_dict(source)})


@main_bp.route('/api/photos/sync/sources/<int:source_id>/enqueue-scan', methods=['POST'])
def api_sync_enqueue_scan(source_id: int):
    member = _current_member_name()
    source = SyncSource.query.get_or_404(source_id)
    dev = SyncDevice.query.get(source.device_id)
    if not _is_admin(member) and (not dev or dev.member_name != member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    if not source.is_active:
        return jsonify({'ok': False, 'error': 'Source is inactive'}), 409

    data = request.get_json(silent=True) or {}
    trigger = sanitize_text(data.get('trigger_type') or 'manual').strip().lower() or 'manual'
    job = create_sync_job(source, trigger_type=trigger)
    set_job_state(job, 'scanning')

    if source.source_type == 'server_path':
        manifest = scan_server_path(source.source_uri)
        accepted, rejected = add_manifest_items(job, manifest)
        set_job_state(job, 'ingesting' if accepted > 0 else 'completed')
        log_sync_event(event_type='sync.job.server_scan', message='Server path scan completed', source_id=source.id, job_id=job.id, payload={'accepted': accepted, 'rejected': rejected})

    source.last_scan_at = datetime.utcnow()
    if dev:
        dev.last_seen_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'job_id': job.id, 'state': job.state})


@main_bp.route('/api/photos/sync/jobs/<int:job_id>/items', methods=['POST'])
def api_sync_add_job_items(job_id: int):
    member = _current_member_name()
    job = SyncJob.query.get_or_404(job_id)
    source = SyncSource.query.get(job.source_id)
    dev = SyncDevice.query.get(source.device_id) if source else None
    if not _is_admin(member) and (not dev or dev.member_name != member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    data = request.get_json(force=True) or {}
    items = data.get('items') if isinstance(data.get('items'), list) else []
    accepted, rejected = add_manifest_items(job, items)
    set_job_state(job, 'scanning')
    if dev:
        dev.last_seen_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'accepted_count': accepted, 'rejected_count': rejected})


@main_bp.route('/api/photos/sync/jobs/<int:job_id>/ingest-item', methods=['POST'])
def api_sync_ingest_item(job_id: int):
    member = _current_member_name()
    job = SyncJob.query.get_or_404(job_id)
    source = SyncSource.query.get(job.source_id)
    dev = SyncDevice.query.get(source.device_id) if source else None
    if not _is_admin(member) and (not dev or dev.member_name != member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    item_id = request.form.get('item_id', type=int)
    if not item_id:
        return jsonify({'ok': False, 'error': 'item_id required'}), 400
    item = SyncJobItem.query.filter_by(id=item_id, job_id=job.id).first_or_404()
    upload = request.files.get('file')
    if not upload:
        return jsonify({'ok': False, 'error': 'file required'}), 400

    temp = save_upload_to_temp(upload, get_sync_temp_folder(), prefix=f'job{job.id}_item{item.id}_')
    item.uploaded_blob_path = temp['temp_path']
    item.size_bytes = int(temp['size_bytes'] or item.size_bytes or 0)
    item.mime_type = temp['mime_type'] or item.mime_type
    item.quick_fingerprint = temp['quick_fingerprint']
    item.state = 'uploaded_temp'
    item.updated_at = datetime.utcnow()
    if dev:
        dev.last_seen_at = datetime.utcnow()
    set_job_state(job, 'ingesting')
    db.session.commit()
    return jsonify({'ok': True, 'item': _item_to_dict(item)})


@main_bp.route('/api/photos/sync/jobs/<int:job_id>', methods=['GET'])
def api_sync_job_detail(job_id: int):
    member = _current_member_name()
    job = SyncJob.query.get_or_404(job_id)
    source = SyncSource.query.get(job.source_id)
    dev = SyncDevice.query.get(source.device_id) if source else None
    if not _is_admin(member) and (not dev or dev.member_name != member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    recompute_job_counters(job)
    db.session.commit()

    items = SyncJobItem.query.filter_by(job_id=job.id).order_by(SyncJobItem.updated_at.desc(), SyncJobItem.id.desc()).limit(200).all()
    events = SyncEventLog.query.filter_by(job_id=job.id).order_by(SyncEventLog.created_at.desc(), SyncEventLog.id.desc()).limit(80).all()
    return jsonify({'ok': True, 'job': _job_to_dict(job), 'items': [_item_to_dict(i) for i in items], 'events': [{'id': e.id, 'level': e.level, 'event_type': e.event_type, 'message': e.message, 'payload_json': e.payload_json, 'created_at': e.created_at.isoformat() if e.created_at else None} for e in events]})


@main_bp.route('/api/photos/sync/jobs', methods=['GET'])
def api_sync_jobs_list():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    source_id = request.args.get('source_id', type=int)
    state = sanitize_text(request.args.get('state') or '').strip().lower()

    q = db.session.query(SyncJob, SyncSource, SyncDevice).join(SyncSource, SyncJob.source_id == SyncSource.id).join(SyncDevice, SyncSource.device_id == SyncDevice.id)
    if not _is_admin(member):
        q = q.filter(SyncDevice.member_name == member)
    if source_id:
        q = q.filter(SyncJob.source_id == source_id)
    if state:
        q = q.filter(SyncJob.state == state)
    rows = q.order_by(SyncJob.created_at.desc(), SyncJob.id.desc()).limit(100).all()

    payload = []
    for job, source, device in rows:
        recompute_job_counters(job)
        payload.append({**_job_to_dict(job), 'source_type': source.source_type, 'member_name': device.member_name, 'platform': device.platform, 'device_label': device.device_label})
    db.session.commit()
    return jsonify({'ok': True, 'items': payload})


@main_bp.route('/api/photos/sync/status/summary', methods=['GET'])
def api_sync_status_summary():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    q_sources = db.session.query(SyncSource, SyncDevice).join(SyncDevice, SyncSource.device_id == SyncDevice.id)
    q_jobs = db.session.query(SyncJob, SyncSource, SyncDevice).join(SyncSource, SyncJob.source_id == SyncSource.id).join(SyncDevice, SyncSource.device_id == SyncDevice.id)
    if not _is_admin(member):
        q_sources = q_sources.filter(SyncDevice.member_name == member)
        q_jobs = q_jobs.filter(SyncDevice.member_name == member)

    src_rows = q_sources.order_by(SyncSource.updated_at.desc()).all()
    job_rows = q_jobs.order_by(SyncJob.updated_at.desc()).limit(25).all()

    items_sources = [{**_source_to_dict(src), 'member_name': dev.member_name, 'platform': dev.platform, 'device_label': dev.device_label} for src, dev in src_rows]
    items_jobs = [{**_job_to_dict(job), 'source_type': src.source_type, 'member_name': dev.member_name, 'platform': dev.platform, 'device_label': dev.device_label} for job, src, dev in job_rows]
    return jsonify({'ok': True, 'sources': items_sources, 'jobs': items_jobs})


@main_bp.route('/api/photos/sync/items/<int:item_id>/retry', methods=['POST'])
def api_sync_retry_item(item_id: int):
    member = _current_member_name()
    item = SyncJobItem.query.get_or_404(item_id)
    job = SyncJob.query.get(item.job_id)
    source = SyncSource.query.get(job.source_id) if job else None
    dev = SyncDevice.query.get(source.device_id) if source else None
    if not _is_admin(member) and (not dev or dev.member_name != member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    if item.state != 'failed':
        return jsonify({'ok': False, 'error': 'Only failed items can be retried'}), 409

    item.state = 'uploaded_temp' if item.uploaded_blob_path else 'awaiting_upload'
    item.last_error = None
    item.updated_at = datetime.utcnow()
    if dev:
        dev.last_seen_at = datetime.utcnow()
    if job:
        set_job_state(job, 'ingesting' if item.uploaded_blob_path else 'scanning')
        recompute_job_counters(job)
    log_sync_event(event_type='sync.item.retry', message='Sync item retried', source_id=source.id if source else None, job_id=job.id if job else None, item_id=item.id)
    db.session.commit()
    return jsonify({'ok': True, 'item': _item_to_dict(item)})
