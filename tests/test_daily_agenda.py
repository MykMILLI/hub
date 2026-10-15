import pytest

from app import create_app, db
from app.models import DailyAgendaEntry, FamilyCalendarEvent


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
