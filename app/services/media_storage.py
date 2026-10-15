import os
import secrets
import shutil
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
PHOTO_UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads', 'photos')
SYNC_TEMP_FOLDER = os.path.join(BASE_DIR, 'uploads', 'sync_temp')
PHOTO_DOWNLOAD_JOBS_FOLDER = os.path.join(BASE_DIR, 'uploads', 'photo_download_jobs')


def ensure_media_dirs() -> None:
    os.makedirs(PHOTO_UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(SYNC_TEMP_FOLDER, exist_ok=True)
    os.makedirs(PHOTO_DOWNLOAD_JOBS_FOLDER, exist_ok=True)


def get_sync_temp_folder() -> str:
    ensure_media_dirs()
    return SYNC_TEMP_FOLDER


def get_photo_download_jobs_folder() -> str:
    ensure_media_dirs()
    return PHOTO_DOWNLOAD_JOBS_FOLDER


def get_photo_upload_folder() -> str:
    ensure_media_dirs()
    return PHOTO_UPLOAD_FOLDER


def resolve_photo_asset_original_abs_path(asset) -> str:
    ensure_media_dirs()
    filename = str(getattr(asset, 'filename', '') or '').strip()
    if not filename:
        raise ValueError('invalid_storage_filename')
    if os.path.basename(filename) != filename:
        raise ValueError('invalid_storage_filename')
    if '/' in filename or '\\' in filename:
        raise ValueError('invalid_storage_filename')

    root = os.path.abspath(PHOTO_UPLOAD_FOLDER)
    candidate = os.path.abspath(os.path.join(root, filename))
    if os.path.commonpath([root, candidate]) != root:
        raise ValueError('path_escape')
    if not os.path.exists(candidate) or not os.path.isfile(candidate):
        raise FileNotFoundError('missing_original')
    return candidate


def canonical_storage_name(original_filename: str) -> str:
    base = os.path.basename(original_filename or 'upload.bin').replace('\\', '_').replace('/', '_')
    return f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{secrets.token_hex(4)}_{base}"


def move_temp_to_photo_storage(temp_path: str, original_filename: str) -> tuple[str, str]:
    ensure_media_dirs()
    filename = canonical_storage_name(original_filename)
    final_path = os.path.join(PHOTO_UPLOAD_FOLDER, filename)
    os.replace(temp_path, final_path)
    return filename, final_path


def copy_source_to_photo_storage(source_path: str, original_filename: str) -> tuple[str, str]:
    ensure_media_dirs()
    filename = canonical_storage_name(original_filename)
    final_path = os.path.join(PHOTO_UPLOAD_FOLDER, filename)
    shutil.copy2(source_path, final_path)
    return filename, final_path
