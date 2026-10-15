import os
from datetime import datetime

from ..models import MediaAlias, MediaContentIndex, PhotoAsset, db


def resolve_or_create_asset(*, sha256: str, file_size: int, mime_type: str | None, is_video: bool, album_id: int | None,
                            original_filename: str, uploader: str, visibility: str,
                            storage_filename: str | None, source_device_id: int | None = None,
                            source_path: str | None = None, source_mtime_utc=None, ingest_job_id: int | None = None,
                            origin_kind: str = 'manual_upload') -> dict:
    idx = MediaContentIndex.query.filter_by(content_sha256=sha256).first()
    if idx:
        canonical = PhotoAsset.query.get(idx.canonical_photo_asset_id)
        if canonical:
            alias_needed = bool(
                canonical.original_filename != original_filename
                or (source_path and canonical.source_path != source_path)
                or (source_device_id and canonical.source_device_id != source_device_id)
            )
            if alias_needed:
                db.session.add(MediaAlias(
                    photo_asset_id=canonical.id,
                    alias_filename=original_filename,
                    alias_source_path=source_path,
                    alias_device_id=source_device_id,
                    alias_kind='renamed_duplicate' if canonical.original_filename != original_filename else 'same_content_duplicate',
                ))
            return {
                'action': 'linked_existing',
                'asset': canonical,
                'dedupe_reason': 'content_sha256_match',
                'created_new': False,
            }

    if not storage_filename:
        raise ValueError('storage_filename required when creating new canonical asset')

    row = PhotoAsset(
        album_id=album_id,
        filename=storage_filename,
        original_filename=original_filename,
        uploader=uploader,
        visibility=visibility,
        shared_with_json='[]',
        mime_type=mime_type,
        file_size=int(file_size or 0),
        is_video=bool(is_video),
        uploaded_at=datetime.utcnow(),
        content_sha256=sha256,
        source_device_id=source_device_id,
        source_path=source_path,
        source_mtime_utc=source_mtime_utc,
        ingest_job_id=ingest_job_id,
        origin_kind=origin_kind,
        dedupe_reason=None,
    )
    db.session.add(row)
    db.session.flush()

    db.session.add(MediaContentIndex(
        content_sha256=sha256,
        canonical_photo_asset_id=row.id,
        file_size=int(file_size or 0),
        mime_type=mime_type,
    ))

    return {
        'action': 'stored_new',
        'asset': row,
        'dedupe_reason': None,
        'created_new': True,
    }
