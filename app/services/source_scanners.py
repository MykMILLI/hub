import os
from datetime import datetime


def scan_server_path(source_uri: str) -> list[dict]:
    rows: list[dict] = []
    if not source_uri or not os.path.isabs(source_uri) or not os.path.exists(source_uri):
        return rows
    for root, _, files in os.walk(source_uri):
        for name in files:
            abs_path = os.path.join(root, name)
            try:
                stat = os.stat(abs_path)
            except Exception:
                continue
            rel = os.path.relpath(abs_path, source_uri).replace('\\', '/')
            rows.append({
                'client_item_id': rel,
                'source_rel_path': rel,
                'filename': name,
                'size_bytes': int(stat.st_size),
                'mtime_utc': datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
                'initial_state': 'uploaded_temp',
                'uploaded_blob_path': abs_path,
                'abs_path': abs_path,
            })
    return rows
