from types import SimpleNamespace

import pytest

from app import create_app, db


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
