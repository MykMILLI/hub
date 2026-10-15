import io
import zipfile
from datetime import datetime, timedelta

import pytest

from app import create_app, db
from app.models import PhotoAsset, PhotoDownloadJob


def make_app():
    test_config = {
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'HOMEHUB_CONFIG': {
            'admin_name': 'Administrator',
            'family_members': ['Alice', 'Bob'],
            'reminders': {'calendar_start_day': 'sunday'},
        },
        'WTF_CSRF_ENABLED': False,
        'SECRET_KEY': 'test',
    }
    app = create_app(test_config)
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app.blueprints.photos as photos_bp
    import app.services.media_storage as media_storage

    photos_dir = tmp_path / 'photos'
    sync_temp_dir = tmp_path / 'sync_temp'
    jobs_dir = tmp_path / 'photo_download_jobs'
    photos_dir.mkdir(parents=True, exist_ok=True)
    sync_temp_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(media_storage, 'PHOTO_UPLOAD_FOLDER', str(photos_dir))
    monkeypatch.setattr(media_storage, 'SYNC_TEMP_FOLDER', str(sync_temp_dir))
    monkeypatch.setattr(media_storage, 'PHOTO_DOWNLOAD_JOBS_FOLDER', str(jobs_dir))
    monkeypatch.setattr(photos_bp, 'PHOTO_UPLOAD_FOLDER', str(photos_dir))

    app = make_app()
    return app.test_client()


def _set_member(client, member_name):
    with client.session_transaction() as sess:
        sess['current_member'] = member_name


def _upload(client, creator, filename, content, visibility='family'):
    data = {
        'creator': creator,
        'visibility': visibility,
        'file': (io.BytesIO(content), filename),
    }
    return client.post('/api/photos/upload', data=data, content_type='multipart/form-data')


def test_original_download_by_asset_id_enforces_permissions(client):
    _set_member(client, 'Administrator')
    up = _upload(client, 'Administrator', 'alice-private.jpg', b'private-a', visibility='private')
    assert up.status_code == 200
    asset_id = int(up.get_json()['items'][0]['id'])

    ok = client.get(f'/api/photos/assets/{asset_id}/download')
    assert ok.status_code == 200
    assert ok.headers.get('Content-Disposition', '').lower().find('attachment') >= 0

    _set_member(client, 'Bob')
    forbidden = client.get(f'/api/photos/assets/{asset_id}/download')
    assert forbidden.status_code == 403


def test_bulk_download_selected_assets_creates_zip_with_manifest(client):
    _set_member(client, 'Administrator')
    r1 = _upload(client, 'Administrator', 'one.jpg', b'one-bytes', visibility='family')
    r2 = _upload(client, 'Administrator', 'two.jpg', b'two-bytes', visibility='family')
    assert r1.status_code == 200
    assert r2.status_code == 200
    a1 = int(r1.get_json()['items'][0]['id'])
    a2 = int(r2.get_json()['items'][0]['id'])

    create = client.post('/api/photos/download-jobs', json={
        'mode': 'selected_assets',
        'asset_ids': [a1, a2],
    })
    assert create.status_code == 200
    payload = create.get_json()
    assert payload['ok'] is True
    job = payload['job']
    assert job['state'] == 'ready'
    assert int(job['included_assets']) == 2

    status = client.get(f"/api/photos/download-jobs/{int(job['id'])}")
    assert status.status_code == 200
    assert status.get_json()['job']['download_url']

    file_resp = client.get(f"/api/photos/download-jobs/{int(job['id'])}/file")
    assert file_resp.status_code == 200
    assert file_resp.headers.get('Content-Type', '').startswith('application/zip')

    archive = zipfile.ZipFile(io.BytesIO(file_resp.data), mode='r')
    names = set(archive.namelist())
    assert 'manifest.json' in names
    assert 'one.jpg' in names
    assert 'two.jpg' in names


def test_bulk_download_expired_job_returns_410(client):
    _set_member(client, 'Administrator')
    up = _upload(client, 'Administrator', 'expire.jpg', b'expiring-bytes', visibility='family')
    asset_id = int(up.get_json()['items'][0]['id'])

    create = client.post('/api/photos/download-jobs', json={
        'mode': 'selected_assets',
        'asset_ids': [asset_id],
    })
    assert create.status_code == 200
    job_id = int(create.get_json()['job']['id'])

    with client.application.app_context():
        job = PhotoDownloadJob.query.get(job_id)
        job.expires_at = datetime.utcnow() - timedelta(seconds=1)
        job.state = 'ready'
        db.session.commit()

    status = client.get(f'/api/photos/download-jobs/{job_id}')
    assert status.status_code == 200
    assert status.get_json()['job']['state'] == 'expired'

    file_resp = client.get(f'/api/photos/download-jobs/{job_id}/file')
    assert file_resp.status_code == 410


def test_album_mode_bulk_download_uses_viewable_assets(client):
    _set_member(client, 'Administrator')
    album_resp = client.post('/api/photos/albums', json={'name': 'Trip', 'visibility': 'family'})
    assert album_resp.status_code == 200
    album_id = int(album_resp.get_json()['album_id'])

    _upload(client, 'Administrator', 'trip-a.jpg', b'a', visibility='family')
    _upload(client, 'Administrator', 'trip-b.jpg', b'b', visibility='family')

    with client.application.app_context():
        rows = PhotoAsset.query.order_by(PhotoAsset.id.asc()).all()
        assert len(rows) >= 2
        rows[-1].album_id = album_id
        rows[-2].album_id = album_id
        db.session.commit()

    job_resp = client.post('/api/photos/download-jobs', json={
        'mode': 'album',
        'album_id': album_id,
    })
    assert job_resp.status_code == 200
    body = job_resp.get_json()
    assert body['ok'] is True
    assert int(body['job']['included_assets']) == 2
