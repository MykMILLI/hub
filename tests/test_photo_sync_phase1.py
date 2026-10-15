import io
import os
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app import create_app, db
from app.models import MediaAlias, MediaContentIndex, PhotoAsset, SyncDevice, SyncJob, SyncJobItem, SyncSource


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
    photos_dir.mkdir(parents=True, exist_ok=True)
    sync_temp_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(media_storage, 'PHOTO_UPLOAD_FOLDER', str(photos_dir))
    monkeypatch.setattr(media_storage, 'SYNC_TEMP_FOLDER', str(sync_temp_dir))
    monkeypatch.setattr(photos_bp, 'PHOTO_UPLOAD_FOLDER', str(photos_dir))

    app = make_app()
    return app.test_client()


def _upload_photo(client, filename, content, creator='Administrator'):
    data = {
        'creator': creator,
        'file': (io.BytesIO(content), filename),
    }
    return client.post('/api/photos/upload', data=data, content_type='multipart/form-data')


def test_sqlite_foreign_keys_are_enforced(client):
    with client.application.app_context():
        db.session.add(SyncJob(source_id=999999, trigger_type='manual', state='queued'))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_duplicate_manual_upload_links_existing(client):
    r1 = _upload_photo(client, 'dup.jpg', b'same-bytes')
    r2 = _upload_photo(client, 'dup.jpg', b'same-bytes')

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.get_json()['items'][0]['dedupe_action'] == 'stored_new'
    assert r2.get_json()['items'][0]['dedupe_action'] == 'linked_existing'

    with client.application.app_context():
        assert PhotoAsset.query.count() == 1
        assert MediaContentIndex.query.count() == 1


def test_renamed_same_content_creates_media_alias(client):
    r1 = _upload_photo(client, 'original.jpg', b'alias-me')
    r2 = _upload_photo(client, 'renamed.jpg', b'alias-me')

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.get_json()['items'][0]['dedupe_action'] == 'linked_existing'

    with client.application.app_context():
        assert PhotoAsset.query.count() == 1
        alias = MediaAlias.query.first()
        assert alias is not None
        assert alias.alias_filename == 'renamed.jpg'


def test_same_filename_different_content_creates_new_asset(client):
    r1 = _upload_photo(client, 'same-name.jpg', b'content-a')
    r2 = _upload_photo(client, 'same-name.jpg', b'content-b')

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.get_json()['items'][0]['dedupe_action'] == 'stored_new'
    assert r2.get_json()['items'][0]['dedupe_action'] == 'stored_new'

    with client.application.app_context():
        assert PhotoAsset.query.count() == 2
        assert MediaContentIndex.query.count() == 2


def test_browser_import_register_enqueue_upload_creates_job_and_items(client):
    register = client.post('/api/photos/sync/sources/register', json={
        'member': 'Administrator',
        'platform': 'web',
        'device_label': 'Chrome',
        'app_instance_id': 'web-instance-1',
        'source_type': 'browser_import',
        'source_uri': 'browser://upload',
        'scope': 'family',
    })
    assert register.status_code == 200
    source_id = register.get_json()['source']['id']

    enqueue = client.post(f'/api/photos/sync/sources/{source_id}/enqueue-scan', json={'trigger_type': 'manual'})
    assert enqueue.status_code == 200
    job_id = enqueue.get_json()['job_id']

    manifest = client.post(f'/api/photos/sync/jobs/{job_id}/items', json={
        'items': [
            {
                'client_item_id': 'item-1',
                'source_rel_path': 'camera/item-1.jpg',
                'filename': 'item-1.jpg',
                'size_bytes': 11,
                'mime_type': 'image/jpeg',
                'mtime_utc': datetime.utcnow().isoformat(),
            }
        ]
    })
    assert manifest.status_code == 200
    assert manifest.get_json()['accepted_count'] == 1

    detail = client.get(f'/api/photos/sync/jobs/{job_id}')
    assert detail.status_code == 200
    item_id = detail.get_json()['items'][0]['id']

    ingest = client.post(
        f'/api/photos/sync/jobs/{job_id}/ingest-item',
        data={'item_id': str(item_id), 'file': (io.BytesIO(b'hello-world!'), 'item-1.jpg')},
        content_type='multipart/form-data',
    )
    assert ingest.status_code == 200
    assert ingest.get_json()['item']['state'] == 'uploaded_temp'

    with client.application.app_context():
        job = SyncJob.query.get(job_id)
        item = SyncJobItem.query.get(item_id)
        assert job is not None
        assert item is not None
        assert item.state == 'uploaded_temp'


def test_failed_item_retry_state_transitions(client):
    with client.application.app_context():
        device = SyncDevice(member_name='Administrator', platform='web', device_label='Web Device', app_instance_id='retry-web-1')
        db.session.add(device)
        db.session.flush()

        source = SyncSource(device_id=device.id, source_type='browser_import', source_uri='browser://retry', scope='private', is_active=True, requires_client_scan=True)
        db.session.add(source)
        db.session.flush()

        job = SyncJob(source_id=source.id, trigger_type='manual', state='failed')
        db.session.add(job)
        db.session.flush()

        item = SyncJobItem(
            job_id=job.id,
            client_item_id='retry-1',
            source_rel_path='retry/a.jpg',
            filename='a.jpg',
            size_bytes=1,
            state='failed',
            uploaded_blob_path=None,
            last_error='boom',
        )
        db.session.add(item)
        db.session.commit()
        first_item_id = item.id

    retry1 = client.post(f'/api/photos/sync/items/{first_item_id}/retry')
    assert retry1.status_code == 200
    assert retry1.get_json()['item']['state'] == 'awaiting_upload'

    with client.application.app_context():
        item = SyncJobItem.query.get(first_item_id)
        job = SyncJob.query.get(item.job_id)
        item.state = 'failed'
        item.uploaded_blob_path = os.path.abspath(__file__)
        item.last_error = 'again'
        db.session.commit()

        retry_item_id = item.id

    retry2 = client.post(f'/api/photos/sync/items/{retry_item_id}/retry')
    assert retry2.status_code == 200
    assert retry2.get_json()['item']['state'] == 'uploaded_temp'

    with client.application.app_context():
        item = SyncJobItem.query.get(retry_item_id)
        job = SyncJob.query.get(item.job_id)
        assert job.state == 'ingesting'
        assert item.last_error is None


def test_server_path_enqueue_becomes_ingestable_and_processed(client, tmp_path):
    from app.services.sync_worker import process_next_sync_item

    watch_dir = tmp_path / 'watch-source'
    watch_dir.mkdir(parents=True, exist_ok=True)
    source_file = watch_dir / 'server-file.jpg'
    source_file.write_bytes(b'server-path-file-content')

    register = client.post('/api/photos/sync/sources/register', json={
        'member': 'Administrator',
        'platform': 'server',
        'device_label': 'Server Scanner',
        'source_type': 'server_path',
        'source_uri': str(watch_dir.resolve()),
        'scope': 'family',
    })
    assert register.status_code == 200
    source_id = register.get_json()['source']['id']

    enqueue = client.post(f'/api/photos/sync/sources/{source_id}/enqueue-scan', json={'trigger_type': 'manual'})
    assert enqueue.status_code == 200
    job_id = enqueue.get_json()['job_id']

    with client.application.app_context():
        item = SyncJobItem.query.filter_by(job_id=job_id).first()
        assert item is not None
        assert item.state == 'uploaded_temp'
        assert os.path.abspath(item.uploaded_blob_path) == str(source_file.resolve())

        processed = process_next_sync_item()
        assert processed is True

        db.session.refresh(item)
        assert item.state in {'completed', 'linked_existing'}
        assert item.photo_asset_id is not None
        assert source_file.exists(), 'Server source file must not be moved or deleted'
