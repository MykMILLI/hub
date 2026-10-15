import hashlib
import os
from werkzeug.datastructures import FileStorage


def _sha256_stream(handle, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = handle.read(chunk_size)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: str) -> str:
    with open(path, 'rb') as fh:
        return _sha256_stream(fh)


def quick_fingerprint(path: str) -> str:
    size = os.path.getsize(path)
    with open(path, 'rb') as fh:
        head = fh.read(64 * 1024)
        if size > 64 * 1024:
            fh.seek(max(size - 64 * 1024, 0))
            tail = fh.read(64 * 1024)
        else:
            tail = b''
    token = hashlib.sha1(head + b'|' + tail).hexdigest()
    return f"{size}:{token}"


def save_upload_to_temp(file: FileStorage, temp_dir: str, prefix: str = 'ingest_') -> dict:
    os.makedirs(temp_dir, exist_ok=True)
    name = os.path.basename(file.filename or 'upload.bin')
    safe = name.replace('\\', '_').replace('/', '_')
    temp_name = f"{prefix}{hashlib.sha1((safe + str(os.getpid())).encode()).hexdigest()[:10]}_{safe}"
    temp_path = os.path.join(temp_dir, temp_name)
    file.save(temp_path)
    size = os.path.getsize(temp_path)
    return {
        'temp_path': temp_path,
        'size_bytes': int(size),
        'sha256': sha256_file(temp_path),
        'quick_fingerprint': quick_fingerprint(temp_path),
        'mime_type': getattr(file, 'mimetype', None) or None,
    }
