import json
import os
import subprocess
import time
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
        })
    return result


def _public_camera_view(camera: dict, member_name: str) -> dict:
    return {
        'id': camera['id'],
        'name': camera['name'],
        'host': camera['host'],
        'channel': int(camera['channel']),
        'ptz_enabled': bool(camera['ptz_enabled']),
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

    data = request.get_json(silent=True) or {}
    action = sanitize_text(data.get('action') or '').strip()
    action_map = {
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
    op = action_map.get(action)
    if not op:
        return jsonify({'ok': False, 'error': 'Unsupported PTZ action'}), 400
    try:
        speed = int(data.get('speed', 32))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'speed must be a number'}), 400
    speed = max(1, min(64, speed))
    try:
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
    except Exception as err:
        return jsonify({'ok': False, 'error': f'Unable to apply PTZ command: {err}'}), 502
    return jsonify({'ok': True, 'action': action, 'camera_id': camera['id']})


@main_bp.route('/uploads/cameras/snapshots/<path:filename>', methods=['GET'])
def serve_camera_snapshot(filename: str):
    return send_from_directory(CAMERA_SNAPSHOT_DIR, filename, as_attachment=False)


@main_bp.route('/uploads/cameras/recordings/<path:filename>', methods=['GET'])
def serve_camera_recording(filename: str):
    return send_from_directory(CAMERA_RECORDING_DIR, filename, as_attachment=False)
