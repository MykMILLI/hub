import json

import pytest

from app import create_app, db
from app.models import AppNotification, Chore


def make_app():
    test_config = {
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'HOMEHUB_CONFIG': {
            'admin_name': 'Administrator',
            'family_members': ['Alice', 'Bob', 'Mykal'],
            'notifications': {
                'mobile_enabled': True,
                'categories': {},
            },
        },
        'WTF_CSRF_ENABLED': False,
        'SECRET_KEY': 'test',
    }
    app = create_app(test_config)
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture()
def client():
    app = make_app()
    return app.test_client()


def test_create_todo_with_assignment_due_mentions_and_rich_payload(client):
    payload = {
        'description': 'Prepare monthly budget review with @Alice',
        'creator': 'Mykal',
        'task_type': 'todo',
        'assignee': 'Alice',
        'due_date': '2026-05-25',
        'status': 'blocked',
        'status_options': ['todo', 'in_progress', 'completed', 'blocked'],
        'tags': ['Finance', 'Review'],
        'mentions': ['Alice', 'Bob'],
        'details_html': '<p>Bring summary table.</p><table><tr><th>Item</th><th>Owner</th></tr><tr><td>Savings</td><td>Alice</td></tr></table>',
        'checklist': [
            {'text': 'Gather statements', 'done': False},
            {'text': 'Draft chart', 'done': True},
        ],
        'chart_data': [
            {'label': 'Income', 'value': 10},
            {'label': 'Expense', 'value': 6},
        ],
        'table_data': {
            'headers': ['Item', 'Owner'],
            'rows': [['Budget', 'Alice']],
        },
    }
    resp = client.post('/api/chores', json=payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True
    item = body['item']

    assert item['description'] == payload['description']
    assert item['task_type'] == 'todo'
    assert item['assignee'] == 'Alice'
    assert item['due_date'] == '2026-05-25'
    assert item['status'] == 'blocked'
    assert 'blocked' in item['status_options']
    assert set(item['mentions']) == {'Alice', 'Bob'}
    assert len(item['checklist']) == 2
    assert len(item['chart_data']) == 2
    assert item['table_data']['headers'] == ['Item', 'Owner']


def test_assignment_and_mentions_create_collaborative_notifications(client):
    resp = client.post(
        '/api/chores',
        json={
            'description': 'Coordinate pantry cleanup with @Bob',
            'creator': 'Mykal',
            'assignee': 'Alice',
            'mentions': ['Bob'],
            'status': 'todo',
        },
    )
    assert resp.status_code == 200

    with client.application.app_context():
        notifications = AppNotification.query.order_by(AppNotification.id.asc()).all()
        event_types = [n.event_type for n in notifications]
        assert 'task_assigned' in event_types
        assert 'task_mention' in event_types

        assigned = [n for n in notifications if n.event_type == 'task_assigned']
        mention = [n for n in notifications if n.event_type == 'task_mention']
        assert assigned and assigned[-1].target_member == 'Alice'
        assert mention and mention[-1].target_member == 'Bob'


def test_done_endpoint_updates_status_completed(client):
    create_resp = client.post(
        '/api/chores',
        json={
            'description': 'Fold laundry',
            'creator': 'Mykal',
            'status': 'todo',
        },
    )
    assert create_resp.status_code == 200
    chore_id = create_resp.get_json()['item']['id']

    done_resp = client.post(f'/api/chores/{chore_id}/done', json={'done': True, 'actor': 'Mykal'})
    assert done_resp.status_code == 200
    done_item = done_resp.get_json()['item']
    assert done_item['done'] is True
    assert done_item['status'] == 'completed'

    list_resp = client.get('/api/chores?include_completed=1')
    rows = list_resp.get_json()
    target = [r for r in rows if r['id'] == chore_id][0]
    assert target['done'] is True
    assert target['status'] == 'completed'

    with client.application.app_context():
        row = Chore.query.get(chore_id)
        assert row is not None
        assert bool(row.done) is True
        assert row.status == 'completed'
