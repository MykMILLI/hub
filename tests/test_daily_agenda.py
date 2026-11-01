from pathlib import Path

import pytest
from requests import Response

from app import create_app, db
from app.models import DailyAgendaEntry, FamilyCalendarEvent
from app.blueprints import daily_agenda, immich


@pytest.fixture()
def client():
    app = create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'HOMEHUB_CONFIG': {
            'admin_name': 'Administrator',
            'family_members': ['Alice', 'Bob'],
            'feature_toggles': {'daily_agenda': True},
            'menu_visibility': {'daily_agenda': True},
        },
        'SECRET_KEY': 'test',
    })
    with app.app_context():
        db.create_all()
    client = app.test_client()
    with client.session_transaction() as session:
        session['current_member'] = 'Administrator'
        session['authed'] = True
    return client


def test_agenda_entry_snapshots_location_and_syncs_calendar(client):
    location = client.post('/api/daily-agenda/locations', json={
        'name': 'Airport', 'address': '1 Terminal Way', 'notes': 'Use arrivals level',
    })
    assert location.status_code == 201
    location_id = location.get_json()['location']['id']

    created = client.post('/api/daily-agenda/entries', json={
        'member_name': 'Alice',
        'date': '2026-10-15',
        'start_time': '09:00',
        'end_time': '10:30',
        'location_id': location_id,
        'details': 'Pick up grandparents',
    })
    assert created.status_code == 201
    entry = created.get_json()['entry']
    assert entry['location_name'] == 'Airport'
    event_id = entry['calendar_event_id']

    with client.application.app_context():
        event = db.session.get(FamilyCalendarEvent, event_id)
        assert event.title == 'Alice: Airport'
        assert event.start_time == '09:00'
        assert event.end_time == '10:30'
        assert event.start_date.isoformat() == '2026-10-15'

    updated = client.put(f"/api/daily-agenda/entries/{entry['id']}", json={
        'member_name': 'Bob', 'date': '2026-10-16', 'start_time': '11:00',
        'end_time': '12:00', 'details': 'Updated pickup',
    })
    assert updated.status_code == 200
    assert updated.get_json()['entry']['calendar_event_id'] == event_id

    with client.application.app_context():
        event = db.session.get(FamilyCalendarEvent, event_id)
        assert event.title == 'Bob: Airport'
        assert event.start_date.isoformat() == '2026-10-16'

    deleted = client.delete(f"/api/daily-agenda/entries/{entry['id']}")
    assert deleted.status_code == 200
    with client.application.app_context():
        assert db.session.get(DailyAgendaEntry, entry['id']) is None
        assert db.session.get(FamilyCalendarEvent, event_id) is None


def test_agenda_rejects_invalid_time_range(client):
    response = client.post('/api/daily-agenda/entries', json={
        'member_name': 'Alice', 'date': '2026-10-15', 'start_time': '12:00', 'end_time': '11:00',
    })
    assert response.status_code == 400
    assert 'after' in response.get_json()['error']


def test_address_search_returns_sanitized_nominatim_suggestions(client, monkeypatch):
    captured = {}

    def fake_get(url, *, params, headers, timeout):
        captured.update(url=url, params=params, headers=headers, timeout=timeout)
        response = Response()
        response.status_code = 200
        response._content = b'[{"display_name":"1 Main St, Springfield","lat":"40.1","lon":"-75.2","type":"house"}]'
        return response

    monkeypatch.setattr(daily_agenda.requests, 'get', fake_get)
    response = client.get('/api/daily-agenda/address-search?q=1%20Main%20St')

    assert response.status_code == 200
    assert response.get_json()['suggestions'] == [{
        'display_name': '1 Main St, Springfield',
        'latitude': '40.1',
        'longitude': '-75.2',
        'type': 'house',
    }]
    assert captured['params']['format'] == 'jsonv2'
    assert captured['params']['addressdetails'] == '1'
    assert captured['headers']['User-Agent'].startswith('HomeHub/')
    assert captured['timeout'] == 8


def test_address_search_does_not_call_provider_for_short_queries(client, monkeypatch):
    monkeypatch.setattr(daily_agenda.requests, 'get', lambda *args, **kwargs: pytest.fail('provider should not be called'))

    response = client.get('/api/daily-agenda/address-search?q=St')

    assert response.status_code == 200
    assert response.get_json() == {'ok': True, 'suggestions': []}


def test_daily_agenda_slideshow_waits_for_video_to_end(client):
    html = Path('templates/daily_agenda.html').read_text(encoding='utf-8')
    assert "video.addEventListener('ended'" in html
    assert "slideAssets[slideIndex]?.type === 'VIDEO'" in html
    assert "'playback' : 'thumbnail'" in html
    assert 'video.muted = true' in html
    assert 'Play video with sound' in html
    assert "video.addEventListener('loadedmetadata'" in html
    assert "video.addEventListener('canplay'" in html
    assert 'showVideoError(frame, asset, url)' in html
    assert 'Retry video' in html
    assert "video.load();" in html
    assert 'setInterval(()=>$' not in html


def test_daily_agenda_slideshow_always_shuffles_complete_deck(client):
    html = Path('templates/daily_agenda.html').read_text(encoding='utf-8')

    assert 'slideOriginalAssets=uniqueAssets(d.assets||[])' in html
    assert 'reshuffleSlides(); playSlides(true);' in html
    assert "slideAssets = shuffleAssets(slideOriginalAssets, previousId);" in html
    assert "localStorage.getItem('shared-family-album-shuffle')" not in html


def test_immich_album_fetches_all_pages_and_deduplicates(monkeypatch):
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    pages = []

    monkeypatch.setattr(
        immich,
        '_settings',
        lambda: ('https://immich.example', 'secret', 'Shared Family Album', 'album-1', 'user-1'),
    )
    monkeypatch.setattr(
        immich.requests,
        'get',
        lambda *args, **kwargs: FakeResponse({'id': 'album-1', 'assets': [{'id': 'old-1', 'type': 'IMAGE'}]}),
    )

    def fake_post(url, *, headers, json, timeout):
        pages.append(json['page'])
        if json['page'] == 1:
            return FakeResponse({
                'assets': {
                    'items': [
                        {'id': 'image-1', 'type': 'IMAGE'},
                        {'id': 'video-1', 'type': 'VIDEO'},
                    ],
                    'nextPage': '2',
                },
            })
        return FakeResponse({
            'assets': {
                'items': [
                    {'id': 'video-1', 'type': 'VIDEO'},
                    {'id': 'image-2', 'type': 'IMAGE'},
                ],
                'nextPage': None,
            },
        })

    monkeypatch.setattr(immich.requests, 'post', fake_post)

    _, _, album = immich._album()

    assert pages == [1, 2]
    assert [asset['id'] for asset in album['assets']] == ['image-1', 'video-1', 'image-2']


def test_immich_video_playback_proxy_forwards_range_response(client, monkeypatch):
    captured = {}

    def fake_get(url, *, headers, stream, timeout):
        captured.update(url=url, headers=headers, stream=stream, timeout=timeout)
        response = Response()
        response.status_code = 206
        response._content = b'video-segment'
        response._content_consumed = True
        response.headers.update({
            'Content-Type': 'video/mp4',
            'Accept-Ranges': 'bytes',
            'Content-Length': '13',
            'Content-Range': 'bytes 0-12/100',
        })
        return response

    monkeypatch.setattr(immich, '_connection', lambda: ('https://immich.example', 'secret'))
    monkeypatch.setattr(immich.requests, 'get', fake_get)

    response = client.get(
        '/api/immich/assets/video-1/playback',
        headers={'Range': 'bytes=0-12'},
    )

    assert response.status_code == 206
    assert response.data == b'video-segment'
    assert response.content_type == 'video/mp4'
    assert response.headers['Accept-Ranges'] == 'bytes'
    assert response.headers['Content-Range'] == 'bytes 0-12/100'
    assert captured['url'] == 'https://immich.example/api/assets/video-1/video/playback'
    assert captured['headers']['Range'] == 'bytes=0-12'
    assert captured['stream'] is True
