import json
from pathlib import Path

import pytest

from app import create_app, db
from app.models import AppNotification, Chore, ChoreWeeklyCompletion


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
    client = app.test_client()
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'
        session['authed'] = True
    return client


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


def test_todo_supports_multiple_assignees_and_collaborator_filtering(client):
    created = client.post(
        '/api/chores',
        json={
            'description': 'Plan the family trip',
            'creator': 'Mykal',
            'task_type': 'todo',
            'assignees': ['Alice', 'Bob', 'Alice'],
            'status': 'todo',
        },
    )
    assert created.status_code == 200
    item = created.get_json()['item']
    assert item['assignee'] == 'Alice'
    assert item['assignees'] == ['Alice', 'Bob']

    bob_items = client.get('/api/chores?assignee=Bob&include_completed=1').get_json()
    assert [row['id'] for row in bob_items] == [item['id']]

    with client.application.app_context():
        assigned = AppNotification.query.filter_by(event_type='task_assigned').order_by(AppNotification.id.asc()).all()
        assert {notification.target_member for notification in assigned} == {'Alice', 'Bob'}

    updated = client.put(
        f"/api/chores/{item['id']}",
        json={'assignees': ['Bob', 'Mykal'], 'actor': 'Alice'},
    )
    assert updated.status_code == 200
    updated_item = updated.get_json()['item']
    assert updated_item['assignee'] == 'Bob'
    assert updated_item['assignees'] == ['Bob', 'Mykal']

    with client.application.app_context():
        mykal_notifications = AppNotification.query.filter_by(
            event_type='task_assigned',
            target_member='Mykal',
        ).all()
        assert len(mykal_notifications) == 1


def test_chore_supports_multiple_collaborators(client):
    created = client.post(
        '/api/chores',
        json={
            'description': 'Take out trash',
            'creator': 'Mykal',
            'task_type': 'chore',
            'assignees': ['Alice', 'Bob'],
        },
    )
    assert created.status_code == 200
    item = created.get_json()['item']
    assert item['assignee'] == 'Alice'
    assert item['assignees'] == ['Alice', 'Bob']


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


def test_checklist_defaults_to_steps_vocabulary(client):
    created = client.post(
        '/api/chores',
        json={
            'description': 'Deep clean garage',
            'creator': 'Mykal',
            'task_type': 'todo',
            'checklist': [{'text': 'Sort boxes'}, {'text': 'Sweep floor', 'done': True}],
        },
    )
    assert created.status_code == 200
    item = created.get_json()['item']
    assert item['checklist_meta'] == {
        'item_label': 'step',
        'item_label_plural': 'steps',
        'progress_verb': '',
    }
    assert item['checklist_progress_label'] == '1 of 2 steps'


def test_checklist_supports_custom_vocabulary_and_item_notes(client):
    created = client.post(
        '/api/chores',
        json={
            'description': 'Houses to Consider',
            'creator': 'Mykal',
            'task_type': 'todo',
            'checklist_meta': {'item_label_plural': 'houses', 'progress_verb': 'visited'},
            'checklist': [
                {'text': '1420 Maple St', 'note': 'Listed at 410k'},
                {'text': '88 Rosewood Ave'},
                {'text': '7 Birch Ct', 'done': True},
            ],
        },
    )
    assert created.status_code == 200
    item = created.get_json()['item']
    assert item['checklist_meta'] == {
        'item_label': 'house',
        'item_label_plural': 'houses',
        'progress_verb': 'visited',
    }
    assert item['checklist_progress_label'] == '1 of 3 houses visited'
    assert item['checklist'][0]['note'] == 'Listed at 410k'
    assert item['checklist'][1]['note'] == ''

    task_id = item['id']
    updated = client.put(
        f'/api/chores/{task_id}',
        json={'checklist_meta': {'item_label': 'property', 'progress_verb': 'toured'}},
    )
    assert updated.status_code == 200
    updated_item = updated.get_json()['item']
    assert updated_item['checklist_meta']['item_label_plural'] == 'properties'
    assert updated_item['checklist_progress_label'].endswith('toured')


def test_checklist_progress_label_uses_singular_for_one_item(client):
    created = client.post(
        '/api/chores',
        json={
            'description': 'Books to finish',
            'creator': 'Mykal',
            'task_type': 'todo',
            'checklist_meta': {'item_label': 'book', 'progress_verb': 'read'},
            'checklist': [{'text': 'Dune'}],
        },
    )
    assert created.status_code == 200
    assert created.get_json()['item']['checklist_progress_label'] == '0 of 1 book read'


def test_chore_tracks_days_of_week_and_notifies_watchers_on_completion(client):
    created = client.post(
        '/api/chores',
        json={
            'description': 'Wash dishes',
            'creator': 'Mykal',
            'task_type': 'chore',
            'assignees': ['Mykal', 'Alice'],
            'watchers': ['Bob'],
            'days_of_week': ['Tue', 'Thu'],
        },
    )
    assert created.status_code == 200
    item = created.get_json()['item']
    assert item['assignees'] == ['Mykal', 'Alice']
    assert item['days_of_week'] == ['Tue', 'Thu']
    assert item['watchers'] == ['Bob']

    first_occurrence = client.post(
        f"/api/chores/{item['id']}/done",
        json={
            'done': True,
            'actor': 'Mykal',
            'occurrence_date': '2026-10-20',
        },
    )
    assert first_occurrence.status_code == 200
    first_item = first_occurrence.get_json()['item']
    assert first_item['done'] is False
    first_progress = first_item['weekly_progress']
    assert {key: first_progress[key] for key in (
        'week_start', 'week_end', 'completed_days', 'completed_count',
        'required_count', 'current_day', 'current_occurrence_scheduled',
        'current_occurrence_completed', 'complete', 'next_open_day',
    )} == {
        'week_start': '2026-10-18',
        'week_end': '2026-10-24',
        'completed_days': ['Tue'],
        'completed_count': 1,
        'required_count': 2,
        'current_day': 'Tue',
        'current_occurrence_scheduled': True,
        'current_occurrence_completed': True,
        'complete': False,
        'next_open_day': 'Thu',
    }

    second_occurrence = client.post(
        f"/api/chores/{item['id']}/done",
        json={
            'done': True,
            'actor': 'Mykal',
            'occurrence_date': '2026-10-22',
        },
    )
    assert second_occurrence.status_code == 200
    second_item = second_occurrence.get_json()['item']
    assert second_item['done'] is True
    assert second_item['weekly_progress']['completed_days'] == ['Tue', 'Thu']
    assert second_item['weekly_progress']['complete'] is True

    with client.application.app_context():
        completions = ChoreWeeklyCompletion.query.order_by(ChoreWeeklyCompletion.scheduled_day.asc()).all()
        assert {(row.week_start.isoformat(), row.scheduled_day) for row in completions} == {
            ('2026-10-18', 'Tue'),
            ('2026-10-18', 'Thu'),
        }
        notifications = AppNotification.query.filter_by(event_type='task_completed').order_by(AppNotification.id.asc()).all()
        assert {notification.target_member for notification in notifications} >= {'Alice', 'Bob'}
        assert 'Mykal' not in {notification.target_member for notification in notifications}

    next_device = client.application.test_client()
    with next_device.session_transaction() as session:
        session['current_member'] = 'Alice'
        session['authed'] = True
    next_week = next_device.get('/api/chores?as_of=2026-10-27')
    assert next_week.status_code == 200
    next_week_item = next(row for row in next_week.get_json() if row['id'] == item['id'])
    assert next_week_item['done'] is False
    assert next_week_item['weekly_progress']['week_start'] == '2026-10-25'
    assert next_week_item['weekly_progress']['completed_count'] == 0
    assert next_week_item['weekly_progress']['current_occurrence_scheduled'] is True
    assert next_week_item['weekly_progress']['current_occurrence_completed'] is False


def test_recurring_chore_checkbox_is_occurrence_specific(client):
    created = client.post(
        '/api/chores',
        json={
            'description': 'Clean kitchen',
            'creator': 'Mykal',
            'task_type': 'chore',
            'assignees': ['Mykal'],
            'days_of_week': ['Tue', 'Thu'],
        },
    ).get_json()['item']

    completed_tuesday = client.post(
        f"/api/chores/{created['id']}/done",
        json={'done': True, 'actor': 'Mykal', 'occurrence_date': '2026-10-20'},
    )
    assert completed_tuesday.status_code == 200

    thursday_view = client.get('/api/chores?include_completed=1&as_of=2026-10-22').get_json()
    task = next(row for row in thursday_view if row['id'] == created['id'])
    assert task['weekly_progress']['completed_count'] == 1
    assert task['weekly_progress']['current_day'] == 'Thu'
    assert task['weekly_progress']['current_occurrence_completed'] is False

    early_completion = client.post(
        f"/api/chores/{created['id']}/done",
        json={'done': True, 'actor': 'Mykal', 'occurrence_date': '2026-10-21'},
    )
    assert early_completion.status_code == 200
    early_item = early_completion.get_json()['item']
    progress = early_item['weekly_progress']
    assert progress['completed_days'] == ['Tue', 'Thu']
    assert progress['complete'] is True
    thursday_entry = next(entry for entry in progress['completions'] if entry['day'] == 'Thu')
    assert thursday_entry['completed_on'] == '2026-10-21'
    assert thursday_entry['due_on'] == '2026-10-22'
    assert thursday_entry['early'] is True
    assert thursday_entry['completed_by'] == 'Mykal'
    assert thursday_entry['completed_at']
    assert early_item['completed_at']

    reopened = client.post(
        f"/api/chores/{created['id']}/done",
        json={'done': False, 'actor': 'Mykal', 'occurrence_date': '2026-10-21'},
    )
    assert reopened.status_code == 200
    assert reopened.get_json()['item']['weekly_progress']['completed_days'] == ['Tue']


def test_early_chore_completion_timestamps_render_in_template():
    html = Path('templates/chores.html').read_text(encoding='utf-8')

    assert 'completionStampHtml(state)' in html
    assert 'formatCompletionStamp' in html
    assert 'id="completionCelebrationStamp"' in html
    assert "${state.canCompleteToday ? '' : 'disabled'}" not in html


def test_chore_template_wires_days_collaborators_and_watchers():
    html = Path('templates/chores.html').read_text(encoding='utf-8')

    assert 'class="new-task-day sr-only"' in html
    assert 'class="edit-task-day sr-only"' in html
    assert 'id="newTaskWatchers"' in html
    assert 'id="editTaskWatchers"' in html
    assert "days_of_week: type === 'chore' ? daysOfWeek : []" in html
    assert "watchers: parseAssignees($('editTaskWatchers').value)" in html
    assert "taskAssignees(t).some(assignee => sameUser(assignee, name))" in html
    assert 'homehub:weekly-chore-progress' not in html
    assert 'id="completionCelebrationModal"' in html
    assert 'id="completionCelebrationTitle"' in html
    assert 'Good job!' in html
    assert 'launchConfetti()' in html


def test_mobile_recipe_details_open_only_from_recipe_selection():
    html = Path('templates/recipes.html').read_text(encoding='utf-8')

    assert '#recipePages { display:none; }' in html
    assert 'id="recipeDetailModal"' in html
    assert 'max-h-[calc(100vh-2rem)] overflow-y-auto' in html
    assert "activateRecipePage(btn.getAttribute('data-target'), { openMobile: true })" in html
    assert "activateRecipePage(firstRecipe.id, { openMobile: false })" in html


def test_chore_and_recipe_pages_render(client):
    assert client.get('/chores').status_code == 200
    assert client.get('/recipes').status_code == 200
