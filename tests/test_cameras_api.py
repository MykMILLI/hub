from types import SimpleNamespace

import pytest

from app import create_app, db
from app.blueprints import cameras as camera_module


@pytest.fixture()
def app():
    app = create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'SECRET_KEY': 'test',
        'HOMEHUB_CONFIG': {
            'admin_name': 'Rachel',
            'family_members': ['Rachel', 'Mykal', 'Avery', 'Harley'],
            'feature_toggles': {'cameras': True},
            'menu_visibility': {'cameras': True},
            'cameras': {
                'admins': ['Rachel', 'Mykal'],
                'items': [{
                    'id': 'front',
                    'name': 'Front Cam',
                    'host': '10.0.0.10',
                    'username': 'camuser',
                    'password': 'secret',
                    'ptz_enabled': True,
                    'capabilities': {
                        'pan': True,
                        'tilt': True,
                        'diagonal': False,
                        'optical_zoom': False,
                        'speed_control': True,
                        'speed_min': 4,
                        'speed_max': 40,
                        'speed_default': 24,
                    },
                    'rtsp_url': 'rtsp://10.0.0.10:554/h264Preview_01_main',
                    'rtsp_sub_url': 'rtsp://10.0.0.10:554/h264Preview_01_sub',
                }],
            },
        },
    })
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture()
def client(app):
    camera_module.PTZ_LEASES.clear()
    camera_module.PTZ_BLOCKED_CAMERAS.clear()
    with app.test_client() as client:
        yield client


def test_cameras_list_masks_credentials(client):
    response = client.get('/api/cameras')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ok'] is True
    assert payload['cameras']
    camera = payload['cameras'][0]
    assert camera['id'] == 'front'
    assert 'username' not in camera
    assert 'password' not in camera


def test_cameras_screen_renders(client):
    response = client.get('/cameras')
    assert response.status_code == 200
    assert b'Camera Center' in response.data
    assert b'Control' in response.data
    assert b'Digital zoom' in response.data
    assert b'Enlarges the view only' in response.data


def test_camera_frame_returns_jpeg(client, monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=b'jpeg-bytes', stderr=b'')

    monkeypatch.setattr('app.blueprints.cameras.subprocess.run', fake_run)
    response = client.get('/api/cameras/front/frame?stream=sub')
    assert response.status_code == 200
    assert response.mimetype == 'image/jpeg'
    assert response.data == b'jpeg-bytes'


def test_camera_stream_returns_mjpeg(client, monkeypatch):
    monkeypatch.setattr(
        'app.blueprints.cameras._ffmpeg_mjpeg_frames',
        lambda rtsp_url, fps=8, jpeg_quality=5: iter([b'jpeg-bytes']),
    )
    response = client.get('/api/cameras/front/stream?stream=sub&fps=6')
    assert response.status_code == 200
    assert response.mimetype == 'multipart/x-mixed-replace'
    assert b'Content-Type: image/jpeg' in response.data
    assert b'jpeg-bytes' in response.data


def test_camera_stream_rejects_invalid_quality(client):
    response = client.get('/api/cameras/front/stream?stream=sub&quality=abc')
    assert response.status_code == 400
    payload = response.get_json()
    assert payload['ok'] is False
    assert 'quality' in payload['error']


def test_camera_audio_returns_mp3_stream(client, monkeypatch):
    monkeypatch.setattr(
        'app.blueprints.cameras._ffmpeg_mp3_chunks',
        lambda rtsp_url, bitrate_kbps=96: iter([b'mp3-bytes']),
    )
    response = client.get('/api/cameras/front/audio?stream=main&bitrate=96')
    assert response.status_code == 200
    assert response.mimetype == 'audio/mpeg'
    assert b'mp3-bytes' in response.data


def test_camera_audio_rejects_invalid_bitrate(client):
    response = client.get('/api/cameras/front/audio?stream=main&bitrate=bad')
    assert response.status_code == 400
    payload = response.get_json()
    assert payload['ok'] is False
    assert 'bitrate' in payload['error']


def test_snapshot_requires_camera_admin(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Avery'
    response = client.post('/api/cameras/front/snapshot', json={'stream': 'main'})
    assert response.status_code == 403


def test_record_clip_returns_file_url(client, monkeypatch):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'

    def fake_run(cmd, **kwargs):
        output_path = cmd[-1]
        with open(output_path, 'wb') as file_obj:
            file_obj.write(b'video')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr('app.blueprints.cameras.subprocess.run', fake_run)
    response = client.post('/api/cameras/front/record', json={'duration_seconds': 6, 'stream': 'sub'})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ok'] is True
    assert payload['url'].startswith('/uploads/cameras/recordings/')


def test_ptz_calls_reolink_api(client, monkeypatch):
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'

    calls = []

    class FakeResponse:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        if 'cmd=Login' in url:
            return FakeResponse([{'code': 0, 'value': {'Token': {'name': 'abc-token'}}}])
        return FakeResponse([{'code': 0, 'value': {'rspCode': 200}}])

    monkeypatch.setattr('app.blueprints.cameras.requests.post', fake_post)
    response = client.post('/api/cameras/front/ptz', json={'action': 'up', 'speed': 24})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ok'] is True
    assert len(calls) == 2


def test_ptz_rejects_unsupported_capability(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'
    response = client.post('/api/cameras/front/ptz', json={'action': 'right_up', 'phase': 'start'})
    assert response.status_code == 400
    payload = response.get_json()
    assert payload['ok'] is False
    assert payload['unsupported'] is True


def test_ptz_renew_requires_matching_lease(client, monkeypatch):
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'

    class FakeResponse:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    def fake_post(url, json=None, timeout=None):
        if 'cmd=Login' in url:
            return FakeResponse([{'code': 0, 'value': {'Token': {'name': 'lease-token'}}}])
        return FakeResponse([{'code': 0, 'value': {'rspCode': 200}}])

    monkeypatch.setattr('app.blueprints.cameras.requests.post', fake_post)
    started = client.post('/api/cameras/front/ptz', json={'action': 'up', 'phase': 'start', 'client_id': 'tab-a'})
    assert started.status_code == 200
    lease_id = started.get_json()['lease_id']

    ok_response = client.post('/api/cameras/front/ptz', json={'action': 'up', 'phase': 'renew', 'lease_id': lease_id, 'client_id': 'tab-a'})
    assert ok_response.status_code == 200

    wrong_owner = client.post('/api/cameras/front/ptz', json={'action': 'up', 'phase': 'renew', 'lease_id': lease_id, 'client_id': 'tab-b'})
    assert wrong_owner.status_code == 409

    stale_response = client.post('/api/cameras/front/ptz', json={'action': 'up', 'phase': 'renew', 'lease_id': 'stale-lease'})
    assert stale_response.status_code == 409
    assert stale_response.get_json()['stale'] is True


def test_ptz_stop_ignores_stale_lease(client, monkeypatch):
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'

    class FakeResponse:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        if 'cmd=Login' in url:
            return FakeResponse([{'code': 0, 'value': {'Token': {'name': 'stop-token'}}}])
        return FakeResponse([{'code': 0, 'value': {'rspCode': 200}}])

    monkeypatch.setattr('app.blueprints.cameras.requests.post', fake_post)
    started = client.post('/api/cameras/front/ptz', json={'action': 'left', 'phase': 'start'})
    assert started.status_code == 200

    response = client.post('/api/cameras/front/ptz', json={'action': 'stop', 'phase': 'stop', 'lease_id': 'old-lease'})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ok'] is True
    assert payload['stale'] is True
    assert len(calls) == 2


def test_ptz_rejects_other_tab_without_takeover(client, monkeypatch):
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'

    class FakeResponse:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    def fake_post(url, json=None, timeout=None):
        if 'cmd=Login' in url:
            return FakeResponse([{'code': 0, 'value': {'Token': {'name': 'tab-token'}}}])
        return FakeResponse([{'code': 0, 'value': {'rspCode': 200}}])

    monkeypatch.setattr('app.blueprints.cameras.requests.post', fake_post)
    start = client.post('/api/cameras/front/ptz', json={'action': 'left', 'phase': 'start', 'client_id': 'tab-a'})
    assert start.status_code == 200
    conflict = client.post('/api/cameras/front/ptz', json={'action': 'right', 'phase': 'start', 'client_id': 'tab-b'})
    assert conflict.status_code == 409
    payload = conflict.get_json()
    assert payload['takeover_required'] is True


def test_ptz_takeover_allows_other_tab_when_forced(client, monkeypatch):
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'

    class FakeResponse:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    def fake_post(url, json=None, timeout=None):
        if 'cmd=Login' in url:
            return FakeResponse([{'code': 0, 'value': {'Token': {'name': 'takeover-token'}}}])
        return FakeResponse([{'code': 0, 'value': {'rspCode': 200}}])

    monkeypatch.setattr('app.blueprints.cameras.requests.post', fake_post)
    first = client.post('/api/cameras/front/ptz', json={'action': 'up', 'phase': 'start', 'client_id': 'tab-a'})
    assert first.status_code == 200
    takeover = client.post('/api/cameras/front/ptz', json={'action': 'right', 'phase': 'start', 'client_id': 'tab-b', 'force_takeover': True})
    assert takeover.status_code == 200
    payload = takeover.get_json()
    assert payload['ok'] is True
    assert payload['watchdog_scope'] == 'process_local'


def test_ptz_protocol_not_configured(client, app):
    with app.app_context():
        app.config['HOMEHUB_CONFIG']['cameras']['items'][0]['control_protocol'] = 'onvif'
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'
    response = client.post('/api/cameras/front/ptz', json={'action': 'up', 'phase': 'start'})
    assert response.status_code == 400
    assert 'protocol' in response.get_json()['error'].lower()


def test_ptz_blocks_new_movement_after_stop_failure(client, monkeypatch):
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'

    class FakeResponse:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    call_index = {'value': 0}

    def fake_post(url, json=None, timeout=None):
        call_index['value'] += 1
        if 'cmd=Login' in url:
            return FakeResponse([{'code': 0, 'value': {'Token': {'name': f"tok-{call_index['value']}"}}}])
        if call_index['value'] >= 4:
            raise RuntimeError('camera unreachable')
        return FakeResponse([{'code': 0, 'value': {'rspCode': 200}}])

    monkeypatch.setattr('app.blueprints.cameras.requests.post', fake_post)
    start = client.post('/api/cameras/front/ptz', json={'action': 'up', 'phase': 'start'})
    assert start.status_code == 200

    stop = client.post('/api/cameras/front/ptz', json={'action': 'stop', 'phase': 'stop', 'force_stop': True})
    assert stop.status_code == 502
    stop_payload = stop.get_json()
    assert stop_payload['stop_failed'] is True

    blocked = client.post('/api/cameras/front/ptz', json={'action': 'left', 'phase': 'start'})
    assert blocked.status_code == 409
    blocked_payload = blocked.get_json()
    assert blocked_payload['stop_required'] is True


def test_ptz_lease_expiration_stops_camera(client, monkeypatch):
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'

    class FakeResponse:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        if 'cmd=Login' in url:
            return FakeResponse([{'code': 0, 'value': {'Token': {'name': f"lease-{len(calls)}"}}}])
        return FakeResponse([{'code': 0, 'value': {'rspCode': 200}}])

    monkeypatch.setattr('app.blueprints.cameras.requests.post', fake_post)
    started = client.post('/api/cameras/front/ptz', json={'action': 'down', 'phase': 'start'})
    assert started.status_code == 200
    lease_id = started.get_json()['lease_id']
    camera_module._ptz_expire_lease('front', lease_id)

    stop_ops = [entry for entry in calls if any((item.get('cmd') == 'PtzCtrl' and item.get('param', {}).get('op') == 'Stop') for item in (entry[1] or []))]
    assert stop_ops
