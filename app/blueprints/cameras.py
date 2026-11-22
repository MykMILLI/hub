import json
import logging
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import quote, urlparse, urlunparse

import requests
from flask import Response, current_app, jsonify, render_template, request, send_from_directory, session, stream_with_context

from ..blueprints import main_bp
from ..security import sanitize_text


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
CAMERA_UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads', 'cameras')
CAMERA_SNAPSHOT_DIR = os.path.join(CAMERA_UPLOAD_DIR, 'snapshots')
CAMERA_RECORDING_DIR = os.path.join(CAMERA_UPLOAD_DIR, 'recordings')
LOGGER = logging.getLogger(__name__)

PTZ_ACTION_MAP = {
    'up': 'Up',
    'down': 'Down',
    'left': 'Left',
    'right': 'Right',
    'left_up': 'LeftUp',
    'left_down': 'LeftDown',
    'right_up': 'RightUp',
    'right_down': 'RightDown',
    'stop': 'Stop',
    'zoom_in': 'ZoomInc',
    'zoom_out': 'ZoomDec',
}
PTZ_CAPABILITY_REQUIREMENTS = {
    'up': 'tilt',
    'down': 'tilt',
    'left': 'pan',
    'right': 'pan',
    'left_up': 'diagonal',
    'left_down': 'diagonal',
    'right_up': 'diagonal',
    'right_down': 'diagonal',
    'zoom_in': 'optical_zoom',
    'zoom_out': 'optical_zoom',
}
PTZ_LEASES: dict[str, dict] = {}
PTZ_BLOCKED_CAMERAS: dict[str, str] = {}
PTZ_LOCK = threading.Lock()


def _ensure_camera_dirs() -> None:
    os.makedirs(CAMERA_SNAPSHOT_DIR, exist_ok=True)
    os.makedirs(CAMERA_RECORDING_DIR, exist_ok=True)


def _current_member_name() -> str:
    return sanitize_text(session.get('current_member') or current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')).strip() or 'Administrator'


def _camera_admins() -> set[str]:
    cfg = current_app.config.get('HOMEHUB_CONFIG') or {}
    camera_cfg = cfg.get('cameras') or {}
    configured = camera_cfg.get('admins') if isinstance(camera_cfg, dict) else None
    names = set()
    if isinstance(configured, list):
        for item in configured:
            clean = sanitize_text(str(item or '')).strip()
            if clean:
                names.add(clean.lower())
    if not names:
        names.update({'rachel', 'mykal'})
        admin_name = sanitize_text(cfg.get('admin_name', '')).strip().lower()
        if admin_name:
            names.add(admin_name)
    return names


def _can_manage_cameras(member_name: str) -> bool:
    return sanitize_text(member_name or '').strip().lower() in _camera_admins()


def _camera_capabilities(raw_camera: dict) -> dict:
    ptz_enabled = bool(raw_camera.get('ptz_enabled', True))
    configured = raw_camera.get('capabilities')
    caps = configured if isinstance(configured, dict) else {}

    def _cap_bool(name: str, fallback: bool) -> bool:
        value = caps.get(name, fallback)
        if isinstance(value, bool):
            return value
        if value is None:
            return fallback
        text = sanitize_text(str(value)).strip().lower()
        if text in {'1', 'true', 'yes', 'on'}:
            return True
        if text in {'0', 'false', 'no', 'off'}:
            return False
        return fallback

    def _cap_int(name: str, fallback: int, minimum: int, maximum: int) -> int:
        value = caps.get(name, fallback)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = fallback
        return max(minimum, min(maximum, parsed))

    pan = _cap_bool('pan', ptz_enabled)
    tilt = _cap_bool('tilt', ptz_enabled)
    diagonal = _cap_bool('diagonal', pan and tilt)
    optical_zoom = _cap_bool('optical_zoom', False)
    digital_zoom = _cap_bool('digital_zoom', False)
    presets = _cap_bool('presets', False)
    speed_control = _cap_bool('speed_control', ptz_enabled)
    speed_min = _cap_int('speed_min', 8, 1, 64) if speed_control else 32
    speed_max = _cap_int('speed_max', 48, 1, 64) if speed_control else 32
    if speed_min > speed_max:
        speed_min, speed_max = speed_max, speed_min
    speed_default = _cap_int('speed_default', 32, speed_min, speed_max)

    return {
        'pan': pan,
        'tilt': tilt,
        'diagonal': diagonal and pan and tilt,
        'optical_zoom': optical_zoom,
        'digital_zoom': digital_zoom,
        'presets': presets,
        'speed_control': speed_control,
        'speed_min': speed_min,
        'speed_max': speed_max,
        'speed_default': speed_default,
    }


def _camera_catalog() -> list[dict]:
    cfg = current_app.config.get('HOMEHUB_CONFIG') or {}
    camera_cfg = cfg.get('cameras') or {}
    items = camera_cfg.get('items') if isinstance(camera_cfg, dict) else []
    if not isinstance(items, list):
        return []

    result = []
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            continue
        camera_id = sanitize_text(raw.get('id') or '').strip().lower()
        if not camera_id:
            camera_id = f'camera-{idx + 1}'
        host = sanitize_text(raw.get('host') or '').strip()
        name = sanitize_text(raw.get('name') or camera_id).strip() or camera_id
        channel = raw.get('channel', 0)
        try:
            channel = int(channel)
        except (TypeError, ValueError):
            channel = 0
        capabilities = _camera_capabilities(raw)
        result.append({
            'id': camera_id,
            'name': name,
            'host': host,
            'scheme': sanitize_text(raw.get('scheme') or 'http').strip().lower() or 'http',
            'username': sanitize_text(raw.get('username') or '').strip(),
            'password': str(raw.get('password') or '').strip(),
            'rtsp_url': sanitize_text(raw.get('rtsp_url') or '').strip(),
            'rtsp_sub_url': sanitize_text(raw.get('rtsp_sub_url') or '').strip(),
            'rtsp_path': sanitize_text(raw.get('rtsp_path') or '/h264Preview_01_main').strip() or '/h264Preview_01_main',
            'rtsp_sub_path': sanitize_text(raw.get('rtsp_sub_path') or '/h264Preview_01_sub').strip() or '/h264Preview_01_sub',
            'channel': channel,
            'ptz_enabled': bool(raw.get('ptz_enabled', True)),
            'control_protocol': sanitize_text(raw.get('control_protocol') or 'reolink_cgi').strip().lower() or 'reolink_cgi',
            'ptz_lease_seconds': raw.get('ptz_lease_seconds'),
            'capabilities': capabilities,
        })
    return result


def _public_camera_view(camera: dict, member_name: str) -> dict:
    return {
        'id': camera['id'],
        'name': camera['name'],
        'host': camera['host'],
        'channel': int(camera['channel']),
        'ptz_enabled': bool(camera['ptz_enabled']),
        'capabilities': camera.get('capabilities') or {},
        'control_protocol': camera.get('control_protocol') or 'reolink_cgi',
        'can_manage': _can_manage_cameras(member_name),
        'frame_url': f"/api/cameras/{camera['id']}/frame",
        'stream_url': f"/api/cameras/{camera['id']}/stream",
        'audio_url': f"/api/cameras/{camera['id']}/audio",
    }


def _find_camera(camera_id: str) -> dict | None:
    clean_id = sanitize_text(camera_id or '').strip().lower()
    if not clean_id:
        return None
    for camera in _camera_catalog():
        if camera['id'] == clean_id:
            return camera
    return None


def _inject_rtsp_credentials(url: str, username: str, password: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        return url
    host = parsed.hostname or ''
    port = f":{parsed.port}" if parsed.port else ''
    if not host:
        return url
    if parsed.username or (not username):
        return url
    auth = quote(username, safe='')
    if password:
        auth = f"{auth}:{quote(password, safe='')}"
    netloc = f"{auth}@{host}{port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _camera_rtsp_url(camera: dict, stream: str = 'main') -> str:
    stream_kind = sanitize_text(stream or 'main').strip().lower()
    explicit = camera.get('rtsp_sub_url') if stream_kind == 'sub' else camera.get('rtsp_url')
    if explicit:
        return _inject_rtsp_credentials(str(explicit), camera.get('username', ''), camera.get('password', ''))
    host = camera.get('host') or ''
    if not host:
        return ''
    path = camera.get('rtsp_sub_path') if stream_kind == 'sub' else camera.get('rtsp_path')
    path = str(path or '/h264Preview_01_main')
    if not path.startswith('/'):
        path = f"/{path}"
    username = camera.get('username') or ''
    password = camera.get('password') or ''
    auth = ''
    if username:
        auth = quote(username, safe='')
        if password:
            auth = f"{auth}:{quote(password, safe='')}"
        auth = f"{auth}@"
    return f"rtsp://{auth}{host}:554{path}"


def _ffmpeg_capture_frame_bytes(rtsp_url: str) -> bytes:
    if not rtsp_url:
        raise RuntimeError('Camera RTSP URL is not configured')
    cmd = [
        'ffmpeg',
        '-nostdin',
        '-rtsp_transport',
        'tcp',
        '-i',
        rtsp_url,
        '-frames:v',
        '1',
        '-q:v',
        '2',
        '-f',
        'image2pipe',
        'pipe:1',
    ]
    completed = subprocess.run(cmd, capture_output=True, timeout=25, check=False)
    if completed.returncode != 0 or not completed.stdout:
        stderr = (completed.stderr or b'').decode('utf-8', errors='ignore').strip()
        raise RuntimeError(stderr or 'ffmpeg frame capture failed')
    return completed.stdout


def _ffmpeg_mjpeg_frames(rtsp_url: str, *, fps: int = 8, jpeg_quality: int = 5):
    if not rtsp_url:
        raise RuntimeError('Camera RTSP URL is not configured')
    frame_rate = max(1, min(20, int(fps or 8)))
    mjpeg_quality = max(2, min(15, int(jpeg_quality or 5)))
    while True:
        cmd = [
            'ffmpeg',
            '-nostdin',
            '-loglevel',
            'error',
            '-rtsp_transport',
            'tcp',
            '-fflags',
            'nobuffer',
            '-flags',
            'low_delay',
            '-i',
            rtsp_url,
            '-an',
            '-vf',
            f'fps={frame_rate}',
            '-q:v',
            str(mjpeg_quality),
            '-f',
            'mjpeg',
            'pipe:1',
        ]
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        buffer = b''
        emitted = False
        error_message = ''
        try:
            while True:
                chunk = process.stdout.read(4096) if process.stdout else b''
                if not chunk:
                    break
                buffer += chunk
                while True:
                    start = buffer.find(b'\xff\xd8')
                    if start < 0:
                        if len(buffer) > 512000:
                            buffer = buffer[-4096:]
                        break
                    end = buffer.find(b'\xff\xd9', start + 2)
                    if end < 0:
                        if start > 0:
                            buffer = buffer[start:]
                        break
                    frame = buffer[start:end + 2]
                    buffer = buffer[end + 2:]
                    emitted = True
                    yield frame
        finally:
            if process.poll() is None:
                process.kill()
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                stderr = (process.stderr.read() or b'').decode('utf-8', errors='ignore').strip()
                if stderr:
                    error_message = stderr
                process.stderr.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        if not emitted:
            current_app.logger.warning('Camera stream produced no frames: %s', error_message or rtsp_url)
        time.sleep(1)


def _multipart_mjpeg_stream(rtsp_url: str, *, fps: int = 8, jpeg_quality: int = 5):
    boundary = b'--frame\r\n'
    try:
        for frame in _ffmpeg_mjpeg_frames(rtsp_url, fps=fps, jpeg_quality=jpeg_quality):
            header = f'Content-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n'.encode('ascii')
            yield boundary + header + frame + b'\r\n'
    except GeneratorExit:
        return
    except Exception as err:
        current_app.logger.warning('Camera stream ended with error: %s', err)
        return


def _ffmpeg_mp3_chunks(rtsp_url: str, *, bitrate_kbps: int = 96):
    if not rtsp_url:
        raise RuntimeError('Camera RTSP URL is not configured')
    audio_bitrate = max(48, min(192, int(bitrate_kbps or 96)))
    while True:
        cmd = [
            'ffmpeg',
            '-nostdin',
            '-loglevel',
            'error',
            '-rtsp_transport',
            'tcp',
            '-fflags',
            'nobuffer',
            '-i',
            rtsp_url,
            '-vn',
            '-ac',
            '1',
            '-ar',
            '22050',
            '-c:a',
            'libmp3lame',
            '-b:a',
            f'{audio_bitrate}k',
            '-f',
            'mp3',
            'pipe:1',
        ]
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        emitted = False
        error_message = ''
        try:
            while True:
                chunk = process.stdout.read(4096) if process.stdout else b''
                if not chunk:
                    break
                emitted = True
                yield chunk
        finally:
            if process.poll() is None:
                process.kill()
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                stderr = (process.stderr.read() or b'').decode('utf-8', errors='ignore').strip()
                if stderr:
                    error_message = stderr
                process.stderr.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        if not emitted:
            current_app.logger.warning('Camera audio stream produced no chunks: %s', error_message or rtsp_url)
        time.sleep(1)


def _audio_mp3_stream(rtsp_url: str, *, bitrate_kbps: int = 96):
    try:
        for chunk in _ffmpeg_mp3_chunks(rtsp_url, bitrate_kbps=bitrate_kbps):
            yield chunk
    except GeneratorExit:
        return
    except Exception as err:
        current_app.logger.warning('Camera audio stream ended with error: %s', err)
        return


def _log_camera_diagnostic(camera_id: str, operation: str, started_at: float, *, ok: bool, detail: str = '', reason: str = '') -> None:
    duration_ms = max(0, int((time.monotonic() - started_at) * 1000))
    payload = {
        'camera_id': camera_id,
        'operation': operation,
        'duration_ms': duration_ms,
        'ok': ok,
    }
    if reason:
        payload['reason'] = reason
    if detail:
        payload['detail'] = detail[:280]
    LOGGER.info('camera_diagnostic %s', json.dumps(payload, separators=(',', ':')))


def _camera_supports_action(camera: dict, action: str) -> tuple[bool, str]:
    if action == 'stop':
        return True, ''
    if action not in PTZ_ACTION_MAP:
        return False, 'Unsupported PTZ action'
    if not camera.get('ptz_enabled'):
        return False, 'PTZ is disabled for this camera'
    caps = camera.get('capabilities') or {}
    requirement = PTZ_CAPABILITY_REQUIREMENTS.get(action)
    if requirement == 'pan' and not caps.get('pan'):
        return False, 'Pan is not supported by this camera'
    if requirement == 'tilt' and not caps.get('tilt'):
        return False, 'Tilt is not supported by this camera'
    if requirement == 'diagonal' and not caps.get('diagonal'):
        return False, 'Diagonal movement is not supported by this camera'
    if requirement == 'optical_zoom' and not caps.get('optical_zoom'):
        return False, 'Optical zoom is not supported by this camera'
    return True, ''


def _camera_ptz_lease_seconds(camera: dict) -> float:
    defaults = (current_app.config.get('HOMEHUB_CONFIG') or {}).get('cameras') or {}
    configured = camera.get('ptz_lease_seconds', defaults.get('ptz_lease_seconds', 1.4))
    try:
        value = float(configured)
    except (TypeError, ValueError):
        value = 1.4
    return max(0.5, min(6.0, value))


def _ptz_send(camera: dict, action: str, speed: int) -> None:
    op = PTZ_ACTION_MAP.get(action)
    if not op:
        raise RuntimeError('Unsupported PTZ action')
    token = _reolink_login_token(camera)
    _reolink_request(
        camera,
        cmd_name='PtzCtrl',
        token=token,
        param={
            'channel': int(camera.get('channel') or 0),
            'op': op,
            'speed': speed,
        },
    )


def _ptz_mark_blocked(camera_id: str, message: str) -> None:
    with PTZ_LOCK:
        PTZ_BLOCKED_CAMERAS[camera_id] = message


def _ptz_clear_block(camera_id: str) -> None:
    with PTZ_LOCK:
        PTZ_BLOCKED_CAMERAS.pop(camera_id, None)


def _ptz_block_reason(camera_id: str) -> str | None:
    with PTZ_LOCK:
        return PTZ_BLOCKED_CAMERAS.get(camera_id)


def _ptz_client_id(raw_value: object) -> str:
    text = sanitize_text(str(raw_value or '')).strip()
    if not text:
        return ''
    return text[:64]


def _ptz_set_lease(camera: dict, action: str, speed: int, lease_seconds: float, owner_member: str, owner_client_id: str) -> dict:
    lease_id = str(uuid.uuid4())
    expires_at = time.monotonic() + lease_seconds
    with PTZ_LOCK:
        current = PTZ_LEASES.get(camera['id'])
        if current and current.get('timer'):
            current['timer'].cancel()
        PTZ_LEASES[camera['id']] = {
            'lease_id': lease_id,
            'camera': dict(camera),
            'action': action,
            'speed': speed,
            'expires_at': expires_at,
            'owner_member': owner_member,
            'owner_client_id': owner_client_id,
            'timer': None,
        }
    _ptz_schedule_expiration(camera['id'], lease_id, lease_seconds)
    return {'lease_id': lease_id, 'expires_at': expires_at}


def _ptz_schedule_expiration(camera_id: str, lease_id: str, delay_seconds: float) -> None:
    timer = threading.Timer(delay_seconds, _ptz_expire_lease, args=(camera_id, lease_id))
    timer.daemon = True
    with PTZ_LOCK:
        lease = PTZ_LEASES.get(camera_id)
        if not lease or lease.get('lease_id') != lease_id:
            return
        existing = lease.get('timer')
        if existing:
            existing.cancel()
        lease['timer'] = timer
    timer.start()


def _ptz_expire_lease(camera_id: str, lease_id: str) -> None:
    started_at = time.monotonic()
    camera = None
    speed = 32
    with PTZ_LOCK:
        lease = PTZ_LEASES.get(camera_id)
        if not lease or lease.get('lease_id') != lease_id:
            return
        camera = dict(lease.get('camera') or {})
        speed = int(lease.get('speed') or 32)
    try:
        _ptz_send(camera, 'stop', speed)
        _log_camera_diagnostic(camera_id, 'ptz_lease_expired_stop', started_at, ok=True, reason='lease_expired')
        _ptz_clear_block(camera_id)
    except Exception as err:
        message = f'Movement timeout stop failed: {err}'
        _ptz_mark_blocked(camera_id, message)
        _log_camera_diagnostic(camera_id, 'ptz_lease_expired_stop', started_at, ok=False, reason='lease_expired', detail=str(err))
    finally:
        with PTZ_LOCK:
            lease = PTZ_LEASES.get(camera_id)
            if lease and lease.get('lease_id') == lease_id:
                timer = lease.get('timer')
                if timer:
                    timer.cancel()
                PTZ_LEASES.pop(camera_id, None)


def _ptz_stop(
    camera: dict,
    speed: int,
    *,
    lease_id: str | None = None,
    force: bool = False,
    reason: str = 'stop',
    owner_member: str = '',
    owner_client_id: str = '',
) -> tuple[bool, str, bool]:
    camera_id = camera['id']
    active_lease_id = None
    lease_owner_member = ''
    lease_owner_client_id = ''
    with PTZ_LOCK:
        lease = PTZ_LEASES.get(camera_id)
        if lease:
            active_lease_id = str(lease.get('lease_id') or '')
            lease_owner_member = sanitize_text(lease.get('owner_member') or '').strip()
            lease_owner_client_id = sanitize_text(lease.get('owner_client_id') or '').strip()
    if lease_id and active_lease_id and lease_id != active_lease_id and not force:
        return True, '', True
    if active_lease_id and not force:
        if lease_owner_member and owner_member and lease_owner_member != owner_member:
            return False, f'Movement lease is owned by {lease_owner_member}.', False
        if lease_owner_client_id and owner_client_id and lease_owner_client_id != owner_client_id:
            return False, 'Movement lease belongs to a different control session.', False
    started_at = time.monotonic()
    try:
        _ptz_send(camera, 'stop', speed)
        _ptz_clear_block(camera_id)
        _log_camera_diagnostic(camera_id, 'ptz_stop', started_at, ok=True, reason=reason)
    except Exception as err:
        message = f'Stop command failed: {err}'
        _ptz_mark_blocked(camera_id, message)
        _log_camera_diagnostic(camera_id, 'ptz_stop', started_at, ok=False, reason=reason, detail=str(err))
        return False, message, False
    with PTZ_LOCK:
        lease = PTZ_LEASES.get(camera_id)
        if lease:
            if not lease_id or force or lease.get('lease_id') == lease_id:
                timer = lease.get('timer')
                if timer:
                    timer.cancel()
                PTZ_LEASES.pop(camera_id, None)
    return True, '', False


def _ptz_renew(camera_id: str, lease_id: str, lease_seconds: float, owner_member: str, owner_client_id: str) -> tuple[bool, str]:
    with PTZ_LOCK:
        lease = PTZ_LEASES.get(camera_id)
        if not lease:
            return False, 'Movement lease is no longer active'
        if lease.get('lease_id') != lease_id:
            return False, 'Movement lease has been replaced by a newer command'
        lease_owner_member = sanitize_text(lease.get('owner_member') or '').strip()
        lease_owner_client_id = sanitize_text(lease.get('owner_client_id') or '').strip()
        if lease_owner_member and owner_member and lease_owner_member != owner_member:
            return False, f'Movement lease is owned by {lease_owner_member}.'
        if lease_owner_client_id and owner_client_id and lease_owner_client_id != owner_client_id:
            return False, 'Movement lease belongs to a different control session.'
        lease['expires_at'] = time.monotonic() + lease_seconds
    _ptz_schedule_expiration(camera_id, lease_id, lease_seconds)
    return True, ''


def _reolink_request(camera: dict, *, cmd_name: str, param: dict, token: str | None = None) -> list:
    host = camera.get('host') or ''
    if not host:
        raise RuntimeError('Camera host is not configured')
    scheme = camera.get('scheme') or 'http'
    token_query = f"&token={token}" if token else ''
    url = f"{scheme}://{host}/cgi-bin/api.cgi?cmd={cmd_name}{token_query}"
    payload = [{
        'cmd': cmd_name,
        'action': 0,
        'param': param,
    }]
    response = requests.post(url, json=payload, timeout=10)
    if response.status_code != 200:
        raise RuntimeError(f"Camera API returned HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError('Unexpected camera API response')
    first = data[0] if isinstance(data[0], dict) else {}
    if first.get('code') not in (0, None):
        detail = ''
        try:
            detail = json.dumps(first)
        except Exception:
            detail = str(first)
        raise RuntimeError(f"Camera API error: {detail}")
    value = first.get('value')
    if isinstance(value, dict):
        rsp_code = value.get('rspCode')
        if rsp_code not in (None, 0, 200, '0', '200'):
            detail = ''
            try:
                detail = json.dumps(value)
            except Exception:
                detail = str(value)
            raise RuntimeError(f"Camera rejected command: {detail}")
    return data


def _reolink_login_token(camera: dict) -> str:
    username = camera.get('username') or ''
    password = camera.get('password') or ''
    if not username or not password:
        raise RuntimeError('Camera username/password are required for PTZ control')
    data = _reolink_request(
        camera,
        cmd_name='Login',
        param={'User': {'userName': username, 'password': password}},
    )
    token = (((data[0] or {}).get('value') or {}).get('Token') or {}).get('name')
    clean_token = sanitize_text(token or '').strip()
    if not clean_token:
        raise RuntimeError('Camera login token missing from response')
    return clean_token


@main_bp.route('/cameras', methods=['GET'])
def cameras_screen():
    config = current_app.config['HOMEHUB_CONFIG']
    member = _current_member_name()
    cameras = [_public_camera_view(camera, member) for camera in _camera_catalog()]
    return render_template(
        'cameras.html',
        config=config,
        cameras=cameras,
        current_member_name=member,
        can_manage_cameras=_can_manage_cameras(member),
    )


@main_bp.route('/api/cameras', methods=['GET'])
def api_cameras_list():
    member = _current_member_name()
    cameras = [_public_camera_view(camera, member) for camera in _camera_catalog()]
    return jsonify({'ok': True, 'cameras': cameras})


@main_bp.route('/api/cameras/<camera_id>/frame', methods=['GET'])
def api_cameras_frame(camera_id: str):
    camera = _find_camera(camera_id)
    if not camera:
        return jsonify({'ok': False, 'error': 'Camera not found'}), 404
    stream = sanitize_text(request.args.get('stream') or 'sub').strip().lower()
    if stream not in {'main', 'sub'}:
        return jsonify({'ok': False, 'error': 'stream must be main or sub'}), 400
    try:
        frame = _ffmpeg_capture_frame_bytes(_camera_rtsp_url(camera, stream=stream))
    except Exception as err:
        return jsonify({'ok': False, 'error': f'Unable to capture frame: {err}'}), 502
    return current_app.response_class(frame, mimetype='image/jpeg')


@main_bp.route('/api/cameras/<camera_id>/stream', methods=['GET'])
def api_cameras_stream(camera_id: str):
    camera = _find_camera(camera_id)
    if not camera:
        return jsonify({'ok': False, 'error': 'Camera not found'}), 404
    stream = sanitize_text(request.args.get('stream') or 'sub').strip().lower()
    if stream not in {'main', 'sub'}:
        return jsonify({'ok': False, 'error': 'stream must be main or sub'}), 400
    try:
        fps = int(request.args.get('fps', 8))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'fps must be a number'}), 400
    fps = max(1, min(20, fps))
    try:
        quality = int(request.args.get('quality', 5))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'quality must be a number'}), 400
    quality = max(2, min(15, quality))
    response = Response(
        stream_with_context(
            _multipart_mjpeg_stream(
                _camera_rtsp_url(camera, stream=stream),
                fps=fps,
                jpeg_quality=quality,
            )
        ),
        mimetype='multipart/x-mixed-replace; boundary=frame',
    )
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


@main_bp.route('/api/cameras/<camera_id>/audio', methods=['GET'])
def api_cameras_audio(camera_id: str):
    camera = _find_camera(camera_id)
    if not camera:
        return jsonify({'ok': False, 'error': 'Camera not found'}), 404
    stream = sanitize_text(request.args.get('stream') or 'main').strip().lower()
    if stream not in {'main', 'sub'}:
        return jsonify({'ok': False, 'error': 'stream must be main or sub'}), 400
    try:
        bitrate = int(request.args.get('bitrate', 96))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'bitrate must be a number'}), 400
    bitrate = max(48, min(192, bitrate))
    response = Response(
        stream_with_context(_audio_mp3_stream(_camera_rtsp_url(camera, stream=stream), bitrate_kbps=bitrate)),
        mimetype='audio/mpeg',
    )
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


@main_bp.route('/api/cameras/<camera_id>/snapshot', methods=['POST'])
def api_cameras_snapshot(camera_id: str):
    member = _current_member_name()
    if not _can_manage_cameras(member):
        return jsonify({'ok': False, 'error': 'Only configured camera admins can capture snapshots.'}), 403
    camera = _find_camera(camera_id)
    if not camera:
        return jsonify({'ok': False, 'error': 'Camera not found'}), 404
    data = request.get_json(silent=True) or {}
    stream = sanitize_text(data.get('stream') or 'main').strip().lower()
    if stream not in {'main', 'sub'}:
        return jsonify({'ok': False, 'error': 'stream must be main or sub'}), 400
    _ensure_camera_dirs()
    try:
        frame = _ffmpeg_capture_frame_bytes(_camera_rtsp_url(camera, stream=stream))
    except Exception as err:
        return jsonify({'ok': False, 'error': f'Unable to capture snapshot: {err}'}), 502
    filename = f"{camera['id']}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.jpg"
    filepath = os.path.join(CAMERA_SNAPSHOT_DIR, filename)
    with open(filepath, 'wb') as file_obj:
        file_obj.write(frame)
    return jsonify({
        'ok': True,
        'filename': filename,
        'url': f'/uploads/cameras/snapshots/{filename}',
    })


@main_bp.route('/api/cameras/<camera_id>/record', methods=['POST'])
def api_cameras_record(camera_id: str):
    member = _current_member_name()
    if not _can_manage_cameras(member):
        return jsonify({'ok': False, 'error': 'Only configured camera admins can record video clips.'}), 403
    camera = _find_camera(camera_id)
    if not camera:
        return jsonify({'ok': False, 'error': 'Camera not found'}), 404
    data = request.get_json(silent=True) or {}
    stream = sanitize_text(data.get('stream') or 'main').strip().lower()
    if stream not in {'main', 'sub'}:
        return jsonify({'ok': False, 'error': 'stream must be main or sub'}), 400
    try:
        duration_seconds = int(data.get('duration_seconds', 20))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'duration_seconds must be a number'}), 400
    duration_seconds = max(5, min(300, duration_seconds))
    _ensure_camera_dirs()
    filename = f"{camera['id']}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.mp4"
    filepath = os.path.join(CAMERA_RECORDING_DIR, filename)
    cmd = [
        'ffmpeg',
        '-nostdin',
        '-rtsp_transport',
        'tcp',
        '-i',
        _camera_rtsp_url(camera, stream=stream),
        '-t',
        str(duration_seconds),
        '-c',
        'copy',
        '-movflags',
        '+faststart',
        filepath,
    ]
    completed = subprocess.run(cmd, capture_output=True, timeout=duration_seconds + 30, check=False)
    if completed.returncode != 0 or not os.path.exists(filepath):
        stderr = (completed.stderr or b'').decode('utf-8', errors='ignore').strip()
        return jsonify({'ok': False, 'error': f'Unable to record clip: {stderr or "ffmpeg failed"}'}), 502
    return jsonify({
        'ok': True,
        'filename': filename,
        'url': f'/uploads/cameras/recordings/{filename}',
        'duration_seconds': duration_seconds,
    })


@main_bp.route('/api/cameras/<camera_id>/ptz', methods=['POST'])
def api_cameras_ptz(camera_id: str):
    member = _current_member_name()
    if not _can_manage_cameras(member):
        return jsonify({'ok': False, 'error': 'Only configured camera admins can control PTZ.'}), 403
    camera = _find_camera(camera_id)
    if not camera:
        return jsonify({'ok': False, 'error': 'Camera not found'}), 404
    if not camera.get('ptz_enabled'):
        return jsonify({'ok': False, 'error': 'PTZ is disabled for this camera'}), 400
    if camera.get('control_protocol') != 'reolink_cgi':
        return jsonify({'ok': False, 'error': 'PTZ control protocol is not configured for this camera'}), 400

    data = request.get_json(silent=True) or {}
    action = sanitize_text(data.get('action') or '').strip().lower()
    if action not in PTZ_ACTION_MAP:
        return jsonify({'ok': False, 'error': 'Unsupported PTZ action'}), 400
    supported, support_reason = _camera_supports_action(camera, action)
    if not supported:
        return jsonify({'ok': False, 'error': support_reason, 'unsupported': True}), 400
    try:
        speed = int(data.get('speed', 32))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'speed must be a number'}), 400
    speed = max(int((camera.get('capabilities') or {}).get('speed_min') or 1), min(int((camera.get('capabilities') or {}).get('speed_max') or 64), speed))

    phase = sanitize_text(data.get('phase') or ('stop' if action == 'stop' else 'start')).strip().lower()
    if phase not in {'start', 'renew', 'stop'}:
        return jsonify({'ok': False, 'error': 'phase must be start, renew, or stop'}), 400
    lease_id = sanitize_text(data.get('lease_id') or '').strip()
    client_id = _ptz_client_id(data.get('client_id') or request.headers.get('X-Homehub-Client-Id'))
    force_takeover = bool(data.get('force_takeover'))
    lease_seconds = _camera_ptz_lease_seconds(camera)
    blocked_reason = _ptz_block_reason(camera['id'])
    watchdog_scope = 'process_local'

    if phase == 'renew':
        if not lease_id:
            return jsonify({'ok': False, 'error': 'lease_id is required when renewing movement'}), 400
        ok, message = _ptz_renew(camera['id'], lease_id, lease_seconds, member, client_id)
        if not ok:
            return jsonify({'ok': False, 'error': message, 'stale': True}), 409
        return jsonify({
            'ok': True,
            'camera_id': camera['id'],
            'action': action,
            'phase': 'renew',
            'lease_id': lease_id,
            'lease_seconds': lease_seconds,
            'watchdog_scope': watchdog_scope,
        })

    if phase == 'stop' or action == 'stop':
        force = bool(data.get('force_stop'))
        ok, message, stale = _ptz_stop(
            camera,
            speed,
            lease_id=lease_id or None,
            force=force,
            reason='client_stop',
            owner_member=member,
            owner_client_id=client_id,
        )
        if stale:
            return jsonify({
                'ok': True,
                'camera_id': camera['id'],
                'action': 'stop',
                'phase': 'stop',
                'stale': True,
                'watchdog_scope': watchdog_scope,
            })
        if not ok:
            status = 409 if 'different control session' in message.lower() or 'owned by' in message.lower() else 502
            return jsonify({'ok': False, 'error': message, 'stop_failed': status == 502, 'ownership_mismatch': status == 409}), status
        return jsonify({'ok': True, 'camera_id': camera['id'], 'action': 'stop', 'phase': 'stop', 'watchdog_scope': watchdog_scope})

    if blocked_reason:
        return jsonify({
            'ok': False,
            'error': f'Previous stop command failed. Resolve stop before moving again. {blocked_reason}',
            'stop_required': True,
        }), 409

    with PTZ_LOCK:
        active_lease = PTZ_LEASES.get(camera['id'])
        active_lease_id = (active_lease or {}).get('lease_id')
        active_lease_owner = sanitize_text((active_lease or {}).get('owner_member') or '').strip()
        active_lease_client = sanitize_text((active_lease or {}).get('owner_client_id') or '').strip()
    if active_lease_id and not force_takeover:
        if active_lease_owner and active_lease_owner != member:
            return jsonify({
                'ok': False,
                'error': f'Camera movement is currently controlled by {active_lease_owner}.',
                'takeover_required': True,
                'owner': active_lease_owner,
            }), 409
        if active_lease_client and client_id and active_lease_client != client_id:
            return jsonify({
                'ok': False,
                'error': 'Camera movement is currently controlled by another browser tab.',
                'takeover_required': True,
            }), 409
    if active_lease_id:
        ok, message, stale = _ptz_stop(
            camera,
            speed,
            lease_id=str(active_lease_id),
            force=True,
            reason='direction_change',
            owner_member=member,
            owner_client_id=client_id,
        )
        if not ok:
            return jsonify({'ok': False, 'error': message, 'stop_failed': True}), 502
        if stale:
            return jsonify({'ok': False, 'error': 'Unable to resolve previous movement lease', 'stale': True}), 409

    started_at = time.monotonic()
    try:
        _ptz_send(camera, action, speed)
        _log_camera_diagnostic(camera['id'], 'ptz_start', started_at, ok=True, reason=action)
        _ptz_clear_block(camera['id'])
    except Exception as err:
        _log_camera_diagnostic(camera['id'], 'ptz_start', started_at, ok=False, reason=action, detail=str(err))
        return jsonify({'ok': False, 'error': f'Unable to apply PTZ command: {err}'}), 502

    lease = _ptz_set_lease(camera, action, speed, lease_seconds, member, client_id)
    return jsonify({
        'ok': True,
        'camera_id': camera['id'],
        'action': action,
        'phase': 'start',
        'lease_id': lease['lease_id'],
        'lease_seconds': lease_seconds,
        'watchdog_scope': watchdog_scope,
    })


@main_bp.route('/uploads/cameras/snapshots/<path:filename>', methods=['GET'])
def serve_camera_snapshot(filename: str):
    return send_from_directory(CAMERA_SNAPSHOT_DIR, filename, as_attachment=False)


@main_bp.route('/uploads/cameras/recordings/<path:filename>', methods=['GET'])
def serve_camera_recording(filename: str):
    return send_from_directory(CAMERA_RECORDING_DIR, filename, as_attachment=False)
