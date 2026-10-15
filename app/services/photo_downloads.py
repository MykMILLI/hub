import json
import os
import zipfile
from datetime import datetime, timedelta

from .. import db
from ..models import PhotoAsset, PhotoDownloadJob, PhotoDownloadJobAsset
from .media_storage import get_photo_download_jobs_folder, resolve_photo_asset_original_abs_path


DEFAULT_JOB_TTL_HOURS = 24


def _utcnow() -> datetime:
    return datetime.utcnow()


def _safe_zip_name(name: str) -> str:
    base = os.path.basename(str(name or '').strip())
    if not base:
        return 'asset.bin'
    return base.replace('\\', '_').replace('/', '_')


def job_to_dict(job: PhotoDownloadJob) -> dict:
    download_url = None
    if job.state == 'ready' and job.expires_at and job.expires_at > _utcnow():
        download_url = f"/api/photos/download-jobs/{job.id}/file"
    return {
        'id': job.id,
        'state': job.state,
        'mode': job.mode,
        'album_id': job.album_id,
        'total_assets': int(job.total_assets or 0),
        'included_assets': int(job.included_assets or 0),
        'skipped_assets': int(job.skipped_assets or 0),
        'total_bytes': int(job.total_bytes or 0),
        'zip_size_bytes': int(job.zip_size_bytes or 0) if job.zip_size_bytes is not None else None,
        'download_url': download_url,
        'expires_at': job.expires_at.isoformat() if job.expires_at else None,
        'error_code': job.error_code,
        'error_message': job.error_message,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'updated_at': job.updated_at.isoformat() if job.updated_at else None,
    }


def mark_expired_if_needed(job: PhotoDownloadJob) -> bool:
    if job.state in {'expired', 'cleaned'}:
        return False
    now = _utcnow()
    if job.expires_at and job.expires_at <= now:
        job.state = 'expired'
        job.updated_at = now
        job.error_code = job.error_code or 'EXPIRED'
        job.error_message = job.error_message or 'Download artifact has expired'
        db.session.commit()
        return True
    return False


def _zip_abs_path(job: PhotoDownloadJob) -> str:
    root = os.path.abspath(get_photo_download_jobs_folder())
    rel = str(job.zip_rel_path or '').strip()
    if not rel:
        raise ValueError('missing_zip_path')
    abs_path = os.path.abspath(os.path.join(root, rel))
    if os.path.commonpath([root, abs_path]) != root:
        raise ValueError('invalid_zip_path')
    return abs_path


def get_zip_abs_path(job: PhotoDownloadJob) -> str:
    return _zip_abs_path(job)


def create_download_job(*, requested_by_member: str, mode: str, album_id: int | None, assets: list[PhotoAsset]) -> PhotoDownloadJob:
    row = PhotoDownloadJob(
        requested_by_member=requested_by_member,
        mode=mode,
        album_id=album_id,
        state='queued',
        total_assets=len(assets),
        included_assets=0,
        skipped_assets=0,
        total_bytes=0,
    )
    db.session.add(row)
    db.session.flush()

    for asset in assets:
        db.session.add(PhotoDownloadJobAsset(
            job_id=row.id,
            photo_asset_id=asset.id,
            original_filename=_safe_zip_name(asset.original_filename or asset.filename),
            storage_filename=str(asset.filename or ''),
            mime_type=asset.mime_type,
            file_size=int(asset.file_size or 0),
            content_sha256=asset.content_sha256,
        ))

    db.session.commit()
    return row


def build_download_job_zip(job: PhotoDownloadJob) -> None:
    job.state = 'preparing'
    job.started_at = job.started_at or _utcnow()
    job.error_code = None
    job.error_message = None
    db.session.commit()

    root = get_photo_download_jobs_folder()
    storage_name = f"photo_dl_{job.id}_{int(_utcnow().timestamp())}.zip"
    rel_path = storage_name
    abs_zip = os.path.abspath(os.path.join(root, rel_path))

    items = PhotoDownloadJobAsset.query.filter_by(job_id=job.id).order_by(PhotoDownloadJobAsset.id.asc()).all()
    seen_names: dict[str, int] = {}
    included_assets = 0
    skipped_assets = 0
    total_bytes = 0
    manifest_items = []

    try:
        with zipfile.ZipFile(abs_zip, mode='w', compression=zipfile.ZIP_STORED) as zf:
            for item in items:
                asset = PhotoAsset.query.get(item.photo_asset_id)
                if not asset:
                    skipped_assets += 1
                    continue
                try:
                    abs_original = resolve_photo_asset_original_abs_path(asset)
                except Exception:
                    skipped_assets += 1
                    continue

                file_name = _safe_zip_name(item.original_filename)
                if file_name in seen_names:
                    seen_names[file_name] += 1
                    stem, ext = os.path.splitext(file_name)
                    file_name = f"{stem}_{seen_names[file_name]}{ext}"
                else:
                    seen_names[file_name] = 0

                zf.write(abs_original, arcname=file_name)
                file_size = int(os.path.getsize(abs_original))
                total_bytes += file_size
                included_assets += 1
                manifest_items.append({
                    'asset_id': asset.id,
                    'zip_name': file_name,
                    'original_filename': item.original_filename,
                    'storage_filename': item.storage_filename,
                    'mime_type': item.mime_type,
                    'size_bytes': file_size,
                    'content_sha256': item.content_sha256,
                })

            zf.writestr('manifest.json', json.dumps({'job_id': job.id, 'items': manifest_items}, indent=2))

        now = _utcnow()
        job.state = 'ready'
        job.included_assets = included_assets
        job.skipped_assets = skipped_assets
        job.total_bytes = total_bytes
        job.zip_storage_name = storage_name
        job.zip_rel_path = rel_path
        job.zip_filename = f"homehub_photos_job_{job.id}.zip"
        job.zip_size_bytes = int(os.path.getsize(abs_zip)) if os.path.exists(abs_zip) else None
        job.completed_at = now
        job.expires_at = now + timedelta(hours=DEFAULT_JOB_TTL_HOURS)
        job.cleanup_after_at = job.expires_at
        job.updated_at = now
        db.session.commit()
    except Exception as exc:
        if os.path.exists(abs_zip):
            try:
                os.remove(abs_zip)
            except Exception:
                pass
        job.state = 'failed'
        job.error_code = 'ZIP_BUILD_ERROR'
        job.error_message = str(exc)
        job.updated_at = _utcnow()
        job.completed_at = _utcnow()
        db.session.commit()
