import json
import os
import subprocess
from datetime import datetime

from flask import current_app, jsonify, redirect, render_template, request, send_file, send_from_directory, session
from PIL import ExifTags, Image
from werkzeug.utils import secure_filename

from ..blueprints import main_bp
from ..models import FamilyMember, PhotoAlbum, PhotoAsset, PhotoDownloadJob, PhotoWatchFolder, db
from ..security import sanitize_text
from ..services.media_dedupe import resolve_or_create_asset
from ..services.media_fingerprint import save_upload_to_temp
from ..services.media_storage import get_sync_temp_folder, move_temp_to_photo_storage, resolve_photo_asset_original_abs_path
from ..services.photo_downloads import build_download_job_zip, create_download_job, get_zip_abs_path, job_to_dict, mark_expired_if_needed
from ..services.notification_service import publish_notification

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
PHOTO_UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads', 'photos')
os.makedirs(PHOTO_UPLOAD_FOLDER, exist_ok=True)

PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.heic', '.heif'}
VIDEO_EXTS = {'.mp4', '.webm', '.mov', '.m4v', '.avi', '.mkv'}

_EXIF_TAG_NAME_TO_ID = {value: key for key, value in ExifTags.TAGS.items()}


def _parse_capture_datetime(value: str | None) -> datetime | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None


def _extract_captured_at_from_image(path: str) -> datetime | None:
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return None
            for key_name in ('DateTimeOriginal', 'DateTimeDigitized', 'DateTime'):
                tag_id = _EXIF_TAG_NAME_TO_ID.get(key_name)
                if not tag_id:
                    continue
                parsed = _parse_capture_datetime(exif.get(tag_id))
                if parsed is not None:
                    return parsed
    except Exception:
        return None
    return None


def _extract_captured_at_from_video(path: str) -> datetime | None:
    # Best-effort probe using embedded media metadata when ffprobe exists.
    cmd = [
        'ffprobe',
        '-v',
        'quiet',
        '-print_format',
        'json',
        '-show_entries',
        'format_tags=creation_time:stream_tags=creation_time',
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        if proc.returncode != 0:
            return None
        payload = json.loads(proc.stdout or '{}')
        fmt_tags = (payload.get('format') or {}).get('tags') or {}
        parsed = _parse_capture_datetime(fmt_tags.get('creation_time'))
        if parsed is not None:
            return parsed
        streams = payload.get('streams') or []
        for stream in streams:
            tags = (stream or {}).get('tags') or {}
            parsed = _parse_capture_datetime(tags.get('creation_time'))
            if parsed is not None:
                return parsed
    except Exception:
        return None
    return None


def _extract_captured_at_from_media(path: str, is_video: bool) -> datetime | None:
    if is_video:
        return _extract_captured_at_from_video(path)
    return _extract_captured_at_from_image(path)


def _asset_media_datetime(asset: PhotoAsset) -> datetime | None:
    return asset.captured_at or asset.source_mtime_utc


def _asset_capture_sort_key(asset: PhotoAsset, asc: bool = False) -> tuple[int, float, int]:
    media_dt = _asset_media_datetime(asset)
    if media_dt is None:
        return (1, float('inf'), asset.id)
    stamp = media_dt.timestamp()
    if asc:
        return (0, stamp, asset.id)
    return (0, -stamp, -asset.id)


def _parse_date_filter(value: str | None) -> datetime | None:
    raw = sanitize_text(value or '').strip()
    if not raw:
        return None
    try:
        day = datetime.strptime(raw, '%Y-%m-%d')
        return datetime(day.year, day.month, day.day)
    except Exception:
        return None


def _current_member_name() -> str:
    name = sanitize_text(session.get('current_member') or '').strip()
    if name:
        return name
    return sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator')).strip()


def _all_family_member_names() -> list[str]:
    names = [sanitize_text((m.name or '')).strip() for m in FamilyMember.query.order_by(FamilyMember.name.asc()).all()]
    names = [n for n in names if n]
    admin_name = sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator')).strip()
    if admin_name and admin_name not in names:
        names.insert(0, admin_name)
    return names


def _safe_json_list(raw: str | None) -> list[str]:
    text = str(raw or '').strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    cleaned = [sanitize_text(str(v or '')).strip() for v in value]
    return [v for v in cleaned if v]


def _is_admin_member(name: str) -> bool:
    clean = sanitize_text(name or '').strip()
    if not clean:
        return False
    admin_name = sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator')).strip()
    if clean in {admin_name, 'Administrator', 'admin'}:
        return True
    row = FamilyMember.query.filter_by(name=clean).first()
    return bool(row and (row.role or 'standard') == 'admin')


def _can_view_album(album: PhotoAlbum, member: str) -> bool:
    if _is_admin_member(member):
        return True
    if sanitize_text(album.creator or '').strip() == member:
        return True
    if sanitize_text(album.visibility or 'private').strip().lower() == 'family':
        return True
    return member in _safe_json_list(album.shared_with_json)


def _can_view_asset(asset: PhotoAsset, member: str) -> bool:
    if _is_admin_member(member):
        return True
    if sanitize_text(asset.uploader or '').strip() == member:
        return True
    if sanitize_text(asset.visibility or 'private').strip().lower() == 'family':
        return True
    if member in _safe_json_list(asset.shared_with_json):
        return True
    if asset.album_id:
        album = PhotoAlbum.query.get(asset.album_id)
        if album and _can_view_album(album, member):
            return True
    return False


def _can_edit_asset(asset: PhotoAsset, member: str) -> bool:
    if _is_admin_member(member):
        return True
    return sanitize_text(asset.uploader or '').strip() == sanitize_text(member or '').strip()


def _can_edit_album(album: PhotoAlbum, member: str) -> bool:
    if _is_admin_member(member):
        return True
    return sanitize_text(album.creator or '').strip() == sanitize_text(member or '').strip()


def _asset_to_dict(asset: PhotoAsset) -> dict:
    return {
        'id': asset.id,
        'album_id': asset.album_id,
        'filename': asset.filename,
        'original_filename': asset.original_filename,
        'uploader': asset.uploader,
        'visibility': asset.visibility or 'private',
        'shared_with': _safe_json_list(asset.shared_with_json),
        'mime_type': asset.mime_type,
        'file_size': int(asset.file_size or 0),
        'is_video': bool(asset.is_video),
        'captured_at': asset.captured_at.isoformat() if asset.captured_at else None,
        'uploaded_at': asset.uploaded_at.isoformat() if asset.uploaded_at else None,
        'download_url': f"/api/photos/assets/{asset.id}/download",
        'original_download_url': f"/api/photos/assets/{asset.id}/download",
        'stream_url': f"/api/photos/assets/{asset.id}/stream",
        'preview_url': f"/uploads/photos/preview/{asset.filename}",
        'content_sha256': asset.content_sha256,
        'origin_kind': asset.origin_kind,
        'dedupe_reason': asset.dedupe_reason,
    }


def _album_to_dict(album: PhotoAlbum, member: str | None = None) -> dict:
    cover_url = None
    if album.cover_photo_id:
        cover = PhotoAsset.query.get(album.cover_photo_id)
        if cover and (not member or _can_view_asset(cover, member)):
            cover_url = f"/uploads/photos/preview/{cover.filename}"
    return {
        'id': album.id,
        'name': album.name,
        'description': album.description or '',
        'creator': album.creator,
        'visibility': album.visibility or 'private',
        'shared_with': _safe_json_list(album.shared_with_json),
        'cover_photo_id': album.cover_photo_id,
        'cover_url': cover_url,
        'created_at': album.created_at.isoformat() if album.created_at else None,
        'updated_at': album.updated_at.isoformat() if album.updated_at else None,
    }


@main_bp.route('/photos', methods=['GET'])
def photos_page():
    return redirect('/photos/slideshow')


@main_bp.route('/photos/albums/<int:album_id>', methods=['GET'])
def photos_album_page(album_id: int):
    album = PhotoAlbum.query.get_or_404(album_id)
    member = _current_member_name()
    if not _can_view_album(album, member):
        return redirect('/upload#family-photos')
    return redirect(f'/upload?photo_album={album.id}#family-photos')


@main_bp.route('/photos/slideshow', methods=['GET'])
def photos_slideshow_page():
    member = sanitize_text(request.args.get('member') or _current_member_name()).strip() or _current_member_name()
    album_rows = PhotoAlbum.query.order_by(PhotoAlbum.updated_at.desc(), PhotoAlbum.id.desc()).all()
    albums = []
    for album in album_rows:
        if not _can_view_album(album, member):
            continue
        row = _album_to_dict(album, member)
        row['asset_count'] = PhotoAsset.query.filter_by(album_id=album.id).count()
        albums.append(row)

    selected_album_id = None
    selected_raw = sanitize_text(request.args.get('album_id') or '').strip()
    if selected_raw.isdigit():
        selected_candidate = int(selected_raw)
        if any(int(a.get('id') or 0) == selected_candidate for a in albums):
            selected_album_id = selected_candidate

    return render_template(
        'photos_slideshow.html',
        albums=albums,
        selected_album_id=selected_album_id,
        current_member_name=member,
    )


@main_bp.route('/uploads/photos/<filename>', methods=['GET'])
def photo_download(filename: str):
    return send_from_directory(PHOTO_UPLOAD_FOLDER, filename, as_attachment=True)


@main_bp.route('/uploads/photos/preview/<filename>', methods=['GET'])
def photo_preview(filename: str):
    response = send_from_directory(PHOTO_UPLOAD_FOLDER, filename, as_attachment=False)
    response.headers['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'"
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return response


@main_bp.route('/api/photos/assets/<int:asset_id>/download', methods=['GET'])
def api_photo_asset_download(asset_id: int):
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    asset = PhotoAsset.query.get(asset_id)
    if not asset:
        return jsonify({'ok': False, 'error': 'Asset not found'}), 404
    if not _can_view_asset(asset, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    try:
        abs_path = resolve_photo_asset_original_abs_path(asset)
    except FileNotFoundError:
        return jsonify({'ok': False, 'error': 'Original file missing'}), 404
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid original file path'}), 500

    return send_file(
        abs_path,
        as_attachment=True,
        download_name=asset.original_filename or asset.filename,
        mimetype=asset.mime_type or None,
        conditional=True,
        etag=asset.content_sha256 or True,
        last_modified=asset.uploaded_at,
    )


@main_bp.route('/api/photos/assets/<int:asset_id>/stream', methods=['GET'])
def api_photo_asset_stream(asset_id: int):
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    asset = PhotoAsset.query.get(asset_id)
    if not asset:
        return jsonify({'ok': False, 'error': 'Asset not found'}), 404
    if not _can_view_asset(asset, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    try:
        abs_path = resolve_photo_asset_original_abs_path(asset)
    except FileNotFoundError:
        return jsonify({'ok': False, 'error': 'Original file missing'}), 404
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid original file path'}), 500

    return send_file(
        abs_path,
        as_attachment=False,
        download_name=asset.original_filename or asset.filename,
        mimetype=asset.mime_type or None,
        conditional=True,
        etag=asset.content_sha256 or True,
        last_modified=asset.uploaded_at,
    )


@main_bp.route('/api/photos/download-jobs', methods=['POST'])
def api_photo_download_jobs_create():
    member = _current_member_name()
    data = request.get_json(force=True) or {}
    mode = sanitize_text(data.get('mode') or 'selected_assets').strip().lower()
    if mode not in {'selected_assets', 'album'}:
        return jsonify({'ok': False, 'error': 'Invalid mode'}), 400

    selected_assets: list[PhotoAsset] = []
    album_id = None
    if mode == 'album':
        album_id = data.get('album_id')
        try:
            album_id = int(album_id)
        except Exception:
            return jsonify({'ok': False, 'error': 'album_id is required for album mode'}), 400
        album = PhotoAlbum.query.get(album_id)
        if not album:
            return jsonify({'ok': False, 'error': 'Album not found'}), 404
        if not _can_view_album(album, member):
            return jsonify({'ok': False, 'error': 'Not allowed'}), 403
        rows = PhotoAsset.query.filter_by(album_id=album.id).all()
        selected_assets = [r for r in rows if _can_view_asset(r, member)]
        selected_assets.sort(key=lambda row: _asset_capture_sort_key(row, asc=False))
    else:
        ids_raw = data.get('asset_ids') if isinstance(data.get('asset_ids'), list) else []
        ids = []
        for value in ids_raw:
            try:
                ids.append(int(value))
            except Exception:
                continue
        ids = sorted(set(ids))
        if not ids:
            return jsonify({'ok': False, 'error': 'asset_ids is required for selected_assets mode'}), 400
        rows = PhotoAsset.query.filter(PhotoAsset.id.in_(ids)).order_by(PhotoAsset.id.asc()).all()
        if not rows:
            return jsonify({'ok': False, 'error': 'Assets not found'}), 404
        selected_assets = [r for r in rows if _can_view_asset(r, member)]

    if not selected_assets:
        return jsonify({'ok': False, 'error': 'No accessible assets found'}), 403

    job = create_download_job(
        requested_by_member=member,
        mode=mode,
        album_id=album_id,
        assets=selected_assets,
    )
    build_download_job_zip(job)
    return jsonify({'ok': True, 'job': job_to_dict(job)})


def _can_access_download_job(job: PhotoDownloadJob, member: str) -> bool:
    return _is_admin_member(member) or sanitize_text(job.requested_by_member or '').strip() == sanitize_text(member or '').strip()


@main_bp.route('/api/photos/download-jobs/<int:job_id>', methods=['GET'])
def api_photo_download_job_status(job_id: int):
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    job = PhotoDownloadJob.query.get(job_id)
    if not job:
        return jsonify({'ok': False, 'error': 'Job not found'}), 404
    if not _can_access_download_job(job, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    mark_expired_if_needed(job)
    return jsonify({'ok': True, 'job': job_to_dict(job)})


@main_bp.route('/api/photos/download-jobs/<int:job_id>/file', methods=['GET'])
def api_photo_download_job_file(job_id: int):
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    job = PhotoDownloadJob.query.get(job_id)
    if not job:
        return jsonify({'ok': False, 'error': 'Job not found'}), 404
    if not _can_access_download_job(job, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    mark_expired_if_needed(job)
    if job.state == 'expired':
        return jsonify({'ok': False, 'error': 'Download artifact has expired'}), 410
    if job.state == 'failed':
        return jsonify({'ok': False, 'error': job.error_message or 'Download job failed'}), 409
    if job.state != 'ready':
        return jsonify({'ok': False, 'error': 'Download job is not ready'}), 409

    try:
        abs_zip = get_zip_abs_path(job)
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid download artifact path'}), 500
    if not os.path.exists(abs_zip):
        return jsonify({'ok': False, 'error': 'Download artifact missing'}), 404

    job.last_accessed_at = datetime.utcnow()
    db.session.commit()
    return send_file(
        abs_zip,
        as_attachment=True,
        download_name=job.zip_filename or f'homehub_photos_job_{job.id}.zip',
        mimetype='application/zip',
        conditional=True,
        etag=True,
        last_modified=job.completed_at or job.updated_at,
    )


@main_bp.route('/api/photos/albums', methods=['GET'])
def api_photos_albums():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    rows = PhotoAlbum.query.order_by(PhotoAlbum.updated_at.desc(), PhotoAlbum.id.desc()).all()
    visible = [r for r in rows if _can_view_album(r, member)]
    payload = []
    for album in visible:
        count = PhotoAsset.query.filter_by(album_id=album.id).count()
        item = _album_to_dict(album, member)
        item['asset_count'] = count
        item['detail_url'] = f"/upload?photo_album={album.id}#family-photos"
        payload.append(item)
    return jsonify({'ok': True, 'items': payload})


@main_bp.route('/api/photos/albums/<int:album_id>', methods=['GET'])
def api_photos_album_detail(album_id: int):
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    album = PhotoAlbum.query.get_or_404(album_id)
    if not _can_view_album(album, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    assets = PhotoAsset.query.filter_by(album_id=album.id).all()
    visible_assets = [a for a in assets if _can_view_asset(a, member)]
    visible_assets.sort(key=lambda row: _asset_capture_sort_key(row, asc=False))
    payload = _album_to_dict(album, member)
    payload['asset_count'] = len(visible_assets)
    payload['assets'] = [_asset_to_dict(a) for a in visible_assets]
    return jsonify({'ok': True, 'album': payload})


@main_bp.route('/api/photos/albums/<int:album_id>/cover', methods=['POST'])
def api_photos_set_album_cover(album_id: int):
    member = _current_member_name()
    album = PhotoAlbum.query.get_or_404(album_id)
    if not _can_edit_album(album, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    data = request.get_json(force=True) or {}
    cover_photo_id = data.get('cover_photo_id')
    if cover_photo_id in (None, '', 0, '0'):
        album.cover_photo_id = None
        db.session.commit()
        return jsonify({'ok': True, 'cover_photo_id': None})
    try:
        cover_photo_id = int(cover_photo_id)
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid cover photo id'}), 400
    asset = PhotoAsset.query.get(cover_photo_id)
    if not asset or asset.album_id != album.id or not _can_view_asset(asset, member):
        return jsonify({'ok': False, 'error': 'Cover photo not found in album'}), 404
    album.cover_photo_id = asset.id
    db.session.commit()
    return jsonify({'ok': True, 'cover_photo_id': asset.id})


@main_bp.route('/api/photos/albums', methods=['POST'])
def api_photos_create_album():
    data = request.get_json(force=True) or {}
    creator = sanitize_text(data.get('creator') or '').strip() or _current_member_name()
    name = sanitize_text(data.get('name') or '').strip()
    description = sanitize_text(data.get('description') or '').strip()
    visibility = sanitize_text(data.get('visibility') or 'private').strip().lower()
    if visibility not in {'private', 'family'}:
        visibility = 'private'
    shared_with = data.get('shared_with') if isinstance(data.get('shared_with'), list) else []
    shared_with = [sanitize_text(str(v or '')).strip() for v in shared_with]
    shared_with = [v for v in shared_with if v]

    if not name:
        return jsonify({'ok': False, 'error': 'Album name is required'}), 400

    row = PhotoAlbum(
        name=name,
        description=description,
        creator=creator,
        visibility=visibility,
        shared_with_json=json.dumps(shared_with),
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({'ok': True, 'album_id': row.id})


@main_bp.route('/api/photos/assets', methods=['GET'])
def api_photos_assets():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    scope = sanitize_text(request.args.get('scope') or 'all').strip().lower()
    album_id = request.args.get('album_id', type=int)
    sort_by = sanitize_text(request.args.get('sort_by') or 'captured_at').strip().lower()
    sort_dir = sanitize_text(request.args.get('sort_dir') or 'desc').strip().lower()
    start_date = _parse_date_filter(request.args.get('start_date'))
    end_date = _parse_date_filter(request.args.get('end_date'))

    rows = PhotoAsset.query.order_by(PhotoAsset.id.desc()).all()
    visible = [r for r in rows if _can_view_asset(r, member)]
    if album_id:
        visible = [r for r in visible if r.album_id == album_id]
    if scope == 'private':
        visible = [r for r in visible if sanitize_text(r.visibility or 'private').strip().lower() == 'private']
    if scope == 'family':
        visible = [r for r in visible if sanitize_text(r.visibility or 'private').strip().lower() == 'family']

    if start_date or end_date:
        filtered = []
        for row in visible:
            media_dt = _asset_media_datetime(row)
            if media_dt is None:
                continue
            media_day = datetime(media_dt.year, media_dt.month, media_dt.day)
            if start_date and media_day < start_date:
                continue
            if end_date and media_day > end_date:
                continue
            filtered.append(row)
        visible = filtered

    if sort_by in {'captured_at', 'media_date'}:
        visible.sort(
            key=lambda row: _asset_capture_sort_key(row, asc=(sort_dir == 'asc')),
        )
    else:
        visible.sort(
            key=lambda row: ((row.uploaded_at or datetime.min), row.id),
            reverse=(sort_dir != 'asc'),
        )

    return jsonify({'ok': True, 'items': [_asset_to_dict(r) for r in visible[:2000]]})


@main_bp.route('/api/photos/assets/batch', methods=['POST'])
def api_photos_assets_batch():
    member = _current_member_name()
    data = request.get_json(force=True) or {}
    action = sanitize_text(data.get('action') or '').strip().lower()
    ids_raw = data.get('asset_ids') if isinstance(data.get('asset_ids'), list) else []
    asset_ids = []
    for value in ids_raw:
        try:
            asset_ids.append(int(value))
        except Exception:
            continue
    asset_ids = sorted(set(asset_ids))
    if not asset_ids:
        return jsonify({'ok': False, 'error': 'No assets selected'}), 400

    rows = PhotoAsset.query.filter(PhotoAsset.id.in_(asset_ids)).all()
    editable = [a for a in rows if _can_edit_asset(a, member)]
    if not editable:
        return jsonify({'ok': False, 'error': 'No editable assets selected'}), 403

    changed = []
    if action == 'move':
        target_album_id = data.get('target_album_id')
        try:
            target_album_id = int(target_album_id) if target_album_id not in (None, '', 0, '0') else None
        except Exception:
            return jsonify({'ok': False, 'error': 'Invalid target album id'}), 400
        target_album = PhotoAlbum.query.get(target_album_id) if target_album_id else None
        if target_album and not _can_edit_album(target_album, member):
            return jsonify({'ok': False, 'error': 'Not allowed to move into target album'}), 403
        for asset in editable:
            asset.album_id = target_album.id if target_album else None
            changed.append(asset.id)
    elif action == 'visibility':
        visibility = sanitize_text(data.get('visibility') or '').strip().lower()
        if visibility not in {'private', 'family'}:
            return jsonify({'ok': False, 'error': 'Invalid visibility'}), 400
        for asset in editable:
            asset.visibility = visibility
            changed.append(asset.id)
    elif action == 'delete':
        for asset in editable:
            try:
                abs_file = os.path.join(PHOTO_UPLOAD_FOLDER, asset.filename)
                if os.path.exists(abs_file):
                    os.remove(abs_file)
            except Exception:
                pass
            albums = PhotoAlbum.query.filter_by(cover_photo_id=asset.id).all()
            for album in albums:
                album.cover_photo_id = None
            db.session.delete(asset)
            changed.append(asset.id)
    else:
        return jsonify({'ok': False, 'error': 'Unsupported action'}), 400

    db.session.commit()
    return jsonify({'ok': True, 'action': action, 'changed_ids': changed, 'count': len(changed)})


@main_bp.route('/api/photos/upload', methods=['POST'])
def api_photos_upload():
    files = request.files.getlist('files') or ([request.files['file']] if 'file' in request.files else [])
    uploader = sanitize_text(request.form.get('creator') or '').strip() or _current_member_name()
    visibility = sanitize_text(request.form.get('visibility') or 'private').strip().lower()
    if visibility not in {'private', 'family'}:
        visibility = 'private'
    album_id = request.form.get('album_id', type=int)

    if not files:
        return jsonify({'ok': False, 'error': 'No files uploaded'}), 400

    album = PhotoAlbum.query.get(album_id) if album_id else None
    if album_id and not album:
        return jsonify({'ok': False, 'error': 'Album not found'}), 404
    if album and not (_is_admin_member(uploader) or sanitize_text(album.creator or '').strip() == uploader):
        return jsonify({'ok': False, 'error': 'Not allowed to upload to this album'}), 403

    uploaded = []
    for file in files:
        if not file or not getattr(file, 'filename', ''):
            continue
        original_name = secure_filename(file.filename)
        ext = os.path.splitext(original_name)[1].lower()
        if ext not in PHOTO_EXTS and ext not in VIDEO_EXTS:
            continue
        temp_meta = save_upload_to_temp(file, get_sync_temp_folder(), prefix='manual_')
        temp_path = temp_meta['temp_path']
        sha = temp_meta['sha256']
        mime_type = sanitize_text(temp_meta.get('mime_type') or '').strip() or None
        captured_at = _extract_captured_at_from_media(temp_path, ext in VIDEO_EXTS)

        outcome = None
        try:
            outcome = resolve_or_create_asset(
                sha256=sha,
                file_size=int(temp_meta['size_bytes'] or 0),
                mime_type=mime_type,
                is_video=(ext in VIDEO_EXTS),
                album_id=album.id if album else None,
                original_filename=original_name,
                uploader=uploader,
                visibility=visibility,
                storage_filename=None,
                source_device_id=None,
                source_path=None,
                source_mtime_utc=captured_at,
                ingest_job_id=None,
                origin_kind='manual_upload',
            )
        except ValueError:
            storage_name, _ = move_temp_to_photo_storage(temp_path, original_name)
            outcome = resolve_or_create_asset(
                sha256=sha,
                file_size=int(temp_meta['size_bytes'] or 0),
                mime_type=mime_type,
                is_video=(ext in VIDEO_EXTS),
                album_id=album.id if album else None,
                original_filename=original_name,
                uploader=uploader,
                visibility=visibility,
                storage_filename=storage_name,
                source_device_id=None,
                source_path=None,
                source_mtime_utc=captured_at,
                ingest_job_id=None,
                origin_kind='manual_upload',
            )

        row = outcome['asset']
        if row.captured_at is None and captured_at is not None:
            row.captured_at = captured_at
        if outcome['action'] == 'linked_existing':
            try:
                os.remove(temp_path)
            except Exception:
                pass
            row.dedupe_reason = outcome.get('dedupe_reason') or 'content_sha256_match'
        uploaded.append((row, outcome['action']))

    db.session.commit()

    if uploaded and visibility == 'family':
        targets = [n for n in _all_family_member_names() if n != uploader]
        publish_notification(
            event_type='shared_cloud_add',
            title='New family photos uploaded',
            message=f"{uploader} uploaded {len(uploaded)} photo/video file{'s' if len(uploaded) != 1 else ''}.",
            target_members=targets,
            created_by=uploader,
            payload={'count': len(uploaded)},
            channels=['in_app', 'mobile'],
        )

    return jsonify({'ok': True, 'items': [{**_asset_to_dict(r), 'dedupe_action': action} for r, action in uploaded]})



@main_bp.route('/api/photos/watch-folders', methods=['GET'])
def api_watch_folders_list():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    rows = PhotoWatchFolder.query.filter_by(member_name=member).order_by(PhotoWatchFolder.created_at.desc()).all()
    return jsonify({'ok': True, 'items': [
        {
            'id': row.id,
            'member_name': row.member_name,
            'folder_uri': row.folder_uri,
            'scope': row.scope or 'private',
            'target_album_id': row.target_album_id,
            'is_active': bool(row.is_active),
            'last_synced_at': row.last_synced_at.isoformat() if row.last_synced_at else None,
            'platform': row.platform or 'android',
        }
        for row in rows
    ]})


@main_bp.route('/api/photos/watch-folders', methods=['POST'])
def api_watch_folders_create():
    data = request.get_json(force=True) or {}
    member = sanitize_text(data.get('member') or '').strip() or _current_member_name()
    folder_uri = sanitize_text(data.get('folder_uri') or '').strip()
    scope = sanitize_text(data.get('scope') or 'private').strip().lower()
    if scope not in {'private', 'family'}:
        scope = 'private'
    if not folder_uri:
        return jsonify({'ok': False, 'error': 'Folder URI is required'}), 400

    row = PhotoWatchFolder(
        member_name=member,
        folder_uri=folder_uri,
        scope=scope,
        target_album_id=data.get('target_album_id'),
        is_active=True,
        platform=sanitize_text(data.get('platform') or 'android').strip() or 'android',
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({'ok': True, 'id': row.id})


@main_bp.route('/api/photos/watch-folders/<int:watch_id>', methods=['PATCH'])
def api_watch_folders_update(watch_id: int):
    member = _current_member_name()
    row = PhotoWatchFolder.query.get_or_404(watch_id)
    if not (_is_admin_member(member) or sanitize_text(row.member_name or '').strip() == member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    data = request.get_json(force=True) or {}
    if 'is_active' in data:
        row.is_active = bool(data.get('is_active'))
    if 'scope' in data:
        scope = sanitize_text(data.get('scope') or '').strip().lower()
        if scope in {'private', 'family'}:
            row.scope = scope
    if 'target_album_id' in data:
        raw_album = data.get('target_album_id')
        if raw_album in (None, '', 0, '0'):
            row.target_album_id = None
        else:
            try:
                row.target_album_id = int(raw_album)
            except Exception:
                return jsonify({'ok': False, 'error': 'Invalid target album id'}), 400
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/photos/watch-folders/<int:watch_id>', methods=['DELETE'])
def api_watch_folders_delete(watch_id: int):
    member = _current_member_name()
    row = PhotoWatchFolder.query.get_or_404(watch_id)
    if not (_is_admin_member(member) or sanitize_text(row.member_name or '').strip() == member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True})
