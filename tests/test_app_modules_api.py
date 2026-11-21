import pytest

from app import create_app, db
from app.permissions import get_visible_app_modules


BASE_CONFIG = {
    'admin_name': 'Administrator',
    'family_members': ['Mykal', 'Rachel'],
    'menu_visibility': {
        'media_downloader': False,
        'pdf_compressor': False,
        'url_shortener': False,
    },
    'feature_toggles': {
        'notes': True,
        'shared_cloud': True,
        'shopping_list': True,
        'chores': True,
        'recipes': True,
        'expiry_tracker': True,
        'qr_generator': True,
        'notifications': True,
        'shared_calendar': True,
        'daily_agenda': True,
        'meal_planner': True,
        'reward_system': True,
    },
}


@pytest.fixture()
def app():
    app = create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'HOMEHUB_CONFIG': dict(BASE_CONFIG),
        'SECRET_KEY': 'test',
    })
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture()
def client(app):
    with app.test_client() as client:
        with client.session_transaction() as session:
            session['current_member'] = 'Administrator'
        yield client


def module_ids(payload):
    return [m['id'] for m in payload['modules']]


def test_app_modules_requires_a_signed_in_member(app):
    with app.test_client() as anon:
        response = anon.get('/api/app-modules')
    assert response.status_code == 401


def test_app_modules_lists_visible_modules_in_sidebar_order(client):
    response = client.get('/api/app-modules')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ok'] is True
    assert payload['member'] == 'Administrator'

    ids = module_ids(payload)
    assert ids[:4] == ['welcome', 'timeline', 'messages', 'polls']
    assert ids[-1] == 'settings'
    # Daily Agenda and Notice-adjacent modules must reach native clients.
    assert 'daily_agenda' in ids
    assert 'meal_planner' in ids
    assert 'reward_system' in ids
    assert 'cameras' in ids


def test_app_modules_hides_hard_disabled_and_toggled_off_modules(client):
    ids = module_ids(client.get('/api/app-modules').get_json())
    # Hard-disabled server-side; shipping them natively produces broken screens.
    assert 'expense_tracker' not in ids
    assert 'photos' not in ids
    assert 'home_assistant' not in ids
    # Turned off through menu_visibility in the config above.
    assert 'media_downloader' not in ids
    assert 'pdf_compressor' not in ids
    assert 'url_shortener' not in ids


def test_app_modules_carry_label_path_and_icon(client):
    modules = client.get('/api/app-modules').get_json()['modules']
    by_id = {m['id']: m for m in modules}
    assert by_id['daily_agenda']['path'] == '/daily-agenda'
    assert by_id['daily_agenda']['label'] == 'Daily Agenda'
    assert by_id['reward_system']['path'] == '/gold-stars'
    assert by_id['reward_system']['label'] == 'Gold Star Club'
    assert by_id['cameras']['path'] == '/cameras'
    assert by_id['cameras']['label'] == 'Cameras'
    assert by_id['chores']['path'] == '/chores'
    assert by_id['shared_notes']['path'] == '/notes'
    for module in modules:
        assert module['key']
        assert module['icon']
        assert module['path'].startswith('/')


def test_feature_toggle_off_hides_module(app):
    config = dict(BASE_CONFIG)
    config['feature_toggles'] = dict(BASE_CONFIG['feature_toggles'], chores=False)
    with app.test_request_context():
        visible = get_visible_app_modules(config, 'Administrator')
    assert 'chores' not in [m['id'] for m in visible]


def test_notifications_toggle_hides_messages_and_polls(app):
    config = dict(BASE_CONFIG)
    config['feature_toggles'] = dict(BASE_CONFIG['feature_toggles'], notifications=False)
    with app.test_request_context():
        ids = [m['id'] for m in get_visible_app_modules(config, 'Administrator')]
    assert 'messages' not in ids
    assert 'polls' not in ids
    assert 'notifications' not in ids
