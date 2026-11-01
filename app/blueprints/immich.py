import os
from urllib.parse import quote, urlparse

import requests
from flask import Response, jsonify, request, session

from ..blueprints import main_bp


def _settings():
    base_url = (os.environ.get('IMMICH_EXTERNAL_URL') or '').rstrip('/')
    api_key = os.environ.get('IMMICH_API_KEY') or ''
    album_name = os.environ.get('IMMICH_SHARED_ALBUM') or 'Shared Family Album'
    album_id = os.environ.get('IMMICH_SHARED_ALBUM_ID') or ''
    user_id = os.environ.get('IMMICH_USER_ID') or ''
    return base_url, api_key, album_name, album_id, user_id


def _headers(api_key):
    return {'x-api-key': api_key, 'Accept': 'application/json'}


def _connection():
    base_url, api_key, _, _, _ = _settings()
    if not base_url or not api_key:
        raise RuntimeError('Immich slideshow is not configured')
    return base_url, api_key


def _dedupe_assets(assets):
    unique = []
    seen_ids = set()
    for asset in assets or []:
        asset_id = str(asset.get('id') or '')
        if not asset_id or asset_id in seen_ids:
            continue
        seen_ids.add(asset_id)
        unique.append(asset)
    return unique


def _all_album_assets(base_url, api_key, album_id):
    assets = []
    page = 1
    for _ in range(100):
        response = requests.post(
            f'{base_url}/api/search/metadata',
            headers={**_headers(api_key), 'Content-Type': 'application/json'},
            json={'albumIds': [album_id], 'size': 250, 'page': page},
            timeout=15,
        )
        response.raise_for_status()
        result = response.json().get('assets') or {}
        assets.extend(result.get('items') or [])
        next_page = result.get('nextPage')
        if not next_page:
            break
        try:
            page = int(next_page)
        except (TypeError, ValueError):
            break
    return _dedupe_assets(assets)


def _album():
    base_url, api_key, album_name, album_id, user_id = _settings()
    if not base_url or not api_key or not user_id:
        raise RuntimeError('Immich slideshow is not configured')
    if album_id:
        response = requests.get(f'{base_url}/api/albums/{quote(album_id, safe="")}', headers=_headers(api_key), params={'withAssets': 'true'}, timeout=10)
        response.raise_for_status()
        album = response.json()
        # Album detail responses can be capped. Metadata search is paginated,
        # so use it whenever it returns assets to ensure the whole album rotates.
        all_assets = _all_album_assets(base_url, api_key, album_id)
        album['assets'] = all_assets or _dedupe_assets(album.get('assets'))
        return base_url, api_key, album
    response = requests.get(f'{base_url}/api/albums', headers=_headers(api_key), params={'userId': user_id}, timeout=10)
    response.raise_for_status()
    payload = response.json()
    for album in (payload.get('albums', []) if isinstance(payload, dict) else payload):
        if str(album.get('albumName') or album.get('name') or '').strip().lower() == album_name.strip().lower():
            return base_url, api_key, album
    raise RuntimeError(f'Immich album "{album_name}" was not found')


@main_bp.route('/api/immich/shared-family-album')
def api_immich_shared_family_album():
    if not session.get('current_member'):
        return jsonify({'ok': False, 'error': 'Authentication required'}), 401
    try:
        base_url, api_key, album = _album()
        if not album.get('assets'):
            detail = requests.get(f"{base_url}/api/albums/{quote(str(album.get('id')), safe='')}", headers=_headers(api_key), timeout=10)
            detail.raise_for_status()
            album = detail.json()
        assets = _dedupe_assets(album.get('assets'))
        return jsonify({'ok': True, 'name': album.get('albumName') or album.get('name'), 'assets': [
            {
                'id': asset.get('id'),
                'type': str(asset.get('type') or '').upper(),
                'filename': asset.get('originalFileName') or asset.get('originalPath') or 'Family photo',
            }
            for asset in assets
            if asset.get('id') and str(asset.get('type') or '').upper() in {'IMAGE', 'VIDEO'}
        ]})
    except (requests.RequestException, RuntimeError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 502


@main_bp.route('/api/immich/assets/<asset_id>/thumbnail')
def api_immich_thumbnail(asset_id):
    if not session.get('current_member'):
        return jsonify({'ok': False, 'error': 'Authentication required'}), 401
    try:
        base_url, api_key = _connection()
        response = requests.get(f'{base_url}/api/assets/{quote(asset_id, safe="")}/thumbnail', headers=_headers(api_key), params={'size': 'preview'}, timeout=20)
        response.raise_for_status()
        return Response(response.content, content_type=response.headers.get('Content-Type', 'image/jpeg'), headers={'Cache-Control': 'private, max-age=300'})
    except (requests.RequestException, RuntimeError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 502


def _proxy_asset_stream(asset_id, endpoint):
    base_url, api_key = _connection()
    headers = _headers(api_key)
    range_header = request.headers.get('Range')
    if range_header:
        headers['Range'] = range_header
    response = requests.get(
        f'{base_url}/api/assets/{quote(asset_id, safe="")}/{endpoint}',
        headers=headers,
        stream=True,
        timeout=30,
    )
    response.raise_for_status()

    def stream_content():
        try:
            yield from response.iter_content(64 * 1024)
        finally:
            response.close()

    response_headers = {'Cache-Control': 'private, max-age=300'}
    for header_name in ('Accept-Ranges', 'Content-Length', 'Content-Range'):
        if response.headers.get(header_name):
            response_headers[header_name] = response.headers[header_name]
    return Response(
        stream_content(),
        status=response.status_code,
        content_type=response.headers.get('Content-Type', 'application/octet-stream'),
        headers=response_headers,
    )


@main_bp.route('/api/immich/assets/<asset_id>/original')
def api_immich_original(asset_id):
    if not session.get('current_member'):
        return jsonify({'ok': False, 'error': 'Authentication required'}), 401
    try:
        return _proxy_asset_stream(asset_id, 'original')
    except (requests.RequestException, RuntimeError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 502


@main_bp.route('/api/immich/assets/<asset_id>/playback')
def api_immich_playback(asset_id):
    if not session.get('current_member'):
        return jsonify({'ok': False, 'error': 'Authentication required'}), 401
    try:
        return _proxy_asset_stream(asset_id, 'video/playback')
    except (requests.RequestException, RuntimeError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 502
