import pytest

from app import create_app, db
from datetime import datetime, timezone

from app.blueprints.dashboard import emit_due_parent_helper_notifications_for_member
from app.models import AppNotification, Chore, FamilyMember, KidRewardParentActionTask, KidRewardPendingGrant


@pytest.fixture()
def app():
    app = create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'SECRET_KEY': 'test',
        'HOMEHUB_CONFIG': {
            'admin_name': 'Rachel',
            'family_members': ['Rachel', 'Mykal', 'Avery', 'Harley'],
            'feature_toggles': {
                'reward_system': True,
            },
            'menu_visibility': {
                'reward_system': True,
            },
        },
    })
    with app.app_context():
        db.create_all()
    return app


def test_routines_include_book_and_movie_picker(client):
    response = client.get('/api/routines')
    assert response.status_code == 200
    payload = response.get_json()
    bedtime = payload['routines']['bedtime']
    assert bedtime['reader'] == 'Rachel'
    assert bedtime['book_picker'] == 'Harley'
    assert bedtime['movie_picker'] == 'Avery'


@pytest.fixture()
def client(app):
    with app.test_client() as client:
        yield client


def test_rewards_reject_non_admin_updates(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Avery'
    response = client.post('/api/rewards/stars', json={'child_name': 'Avery', 'delta': 1})
    assert response.status_code == 403
    payload = response.get_json()
    assert payload['ok'] is False


def test_rewards_reject_self_star_awards_for_parents(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    response = client.post('/api/rewards/stars', json={'child_name': 'Rachel', 'delta': 1, 'reason': 'Great planning'})
    assert response.status_code == 403
    payload = response.get_json()
    assert payload['ok'] is False
    assert 'cannot give yourself stars' in payload['error'].lower()


def test_rewards_allow_rachel_or_mykal_updates(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    response = client.post('/api/rewards/stars', json={'child_name': 'Avery', 'delta': 2})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ok'] is True
    children = payload['state']['children']
    avery = next((row for row in children if row['child_name'] == 'Avery'), None)
    assert avery is not None
    assert avery['stars'] == 2
    assert payload['state']['reward_options_by_child']['Avery']
    assert payload['state']['reward_options_by_child']['Harley']
    assert payload['state']['weekly_summary']['week_label']
    assert isinstance(payload['state']['behavior_trend']['daily'], list)
    assert payload['state']['event_history']


def test_rewards_emit_parent_notification_on_milestone(client, app):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    # Cross the first milestone (10 stars) in one update.
    response = client.post('/api/rewards/stars', json={'child_name': 'Avery', 'delta': 10})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ok'] is True
    reached = payload['effects']['milestones_reached']
    assert reached
    with app.app_context():
        rows = AppNotification.query.filter_by(event_type='kid_reward_milestone').all()
    targets = sorted(row.target_member for row in rows)
    assert targets == ['Mykal', 'Rachel']


def test_rewards_emit_family_notifications_on_star_and_consequence(client, app):
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'
    plus = client.post('/api/rewards/stars', json={'child_name': 'Avery', 'delta': 1, 'reason': 'Helped clean up'})
    assert plus.status_code == 200
    minus = client.post('/api/rewards/stars', json={'child_name': 'Avery', 'delta': -1, 'reason': 'Unsafe rough play'})
    assert minus.status_code == 200
    with app.app_context():
        star_rows = AppNotification.query.filter_by(event_type='kid_reward_star_awarded').all()
        consequence_rows = AppNotification.query.filter_by(event_type='kid_reward_consequence').all()
        assert star_rows
        assert consequence_rows
    star_targets = sorted({row.target_member for row in star_rows})
    consequence_targets = sorted({row.target_member for row in consequence_rows})
    assert star_targets == ['Avery', 'Harley', 'Mykal', 'Rachel']
    assert consequence_targets == ['Avery', 'Harley', 'Mykal', 'Rachel']


def test_rewards_redeem_records_history_and_reduces_stars(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    client.post('/api/rewards/stars', json={'child_name': 'Avery', 'delta': 8})
    state = client.get('/api/rewards/state').get_json()['state']
    option = state['reward_options_by_child']['Avery'][0]
    client.post('/api/rewards/stars', json={'child_name': 'Avery', 'delta': option['stars_required']})
    redeem = client.post('/api/rewards/redeem', json={
        'child_name': 'Avery',
        'reward_option_id': option['id'],
    })
    assert redeem.status_code == 200
    payload = redeem.get_json()
    avery = next((row for row in payload['state']['children'] if row['child_name'] == 'Avery'), None)
    assert avery is not None
    assert avery['stars'] == 8
    history = payload['state']['recent_redemptions']
    assert history
    assert history[0]['reward_label'] == option['reward_label']
    parent_tasks = payload['state']['parent_action_tasks']
    assert parent_tasks
    assert sorted({entry['parent_name'] for entry in parent_tasks}) == ['Mykal', 'Rachel']


def test_rewards_redeem_can_skip_parent_action_task_creation(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    client.post('/api/rewards/stars', json={'child_name': 'Avery', 'delta': 8})
    state = client.get('/api/rewards/state').get_json()['state']
    option = state['reward_options_by_child']['Avery'][0]
    client.post('/api/rewards/stars', json={'child_name': 'Avery', 'delta': option['stars_required']})
    redeem = client.post('/api/rewards/redeem', json={
        'child_name': 'Avery',
        'reward_option_id': option['id'],
        'requires_parent_action': False,
    })
    assert redeem.status_code == 200
    payload = redeem.get_json()
    assert payload['state']['parent_action_tasks'] == []


def test_rewards_timeout_applies_consequence_and_history(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'
    client.post('/api/rewards/stars', json={'child_name': 'Harley', 'delta': 6})
    timeout = client.post('/api/rewards/timeout', json={
        'child_name': 'Harley',
        'reason': 'Unsafe rough play',
        'timeout_minutes': 7,
        'stars_deducted': 2,
        'reward_minutes_lost': 15,
    })
    assert timeout.status_code == 200
    payload = timeout.get_json()
    harley = next((row for row in payload['state']['children'] if row['child_name'] == 'Harley'), None)
    assert harley is not None
    assert harley['stars'] == 4
    logs = payload['state']['recent_timeouts']
    assert logs
    assert logs[0]['reason'] == 'Unsafe rough play'
    repairs = payload['state']['repair_tasks']
    assert repairs
    assert repairs[0]['task_text']
    assert repairs[0]['completed'] is False


def test_rewards_pending_repair_blocks_positive_stars_and_redeem_until_completed(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    timeout = client.post('/api/rewards/timeout', json={
        'child_name': 'Avery',
        'reason': 'Yelling at sibling',
        'timeout_minutes': 5,
        'stars_deducted': 0,
        'reward_minutes_lost': 10,
        'repair_task': 'Apologize and help clean up blocks',
    })
    assert timeout.status_code == 200
    timeout_payload = timeout.get_json()
    repair = timeout_payload['state']['repair_tasks'][0]
    assert repair['child_name'] == 'Avery'
    assert repair['completed'] is False

    blocked_stars = client.post('/api/rewards/stars', json={'child_name': 'Avery', 'delta': 1})
    assert blocked_stars.status_code == 409

    client.post('/api/rewards/stars', json={'child_name': 'Avery', 'delta': 5})
    state = client.get('/api/rewards/state').get_json()['state']
    option = state['reward_options_by_child']['Avery'][0]
    blocked_redeem = client.post('/api/rewards/redeem', json={
        'child_name': 'Avery',
        'reward_option_id': option['id'],
    })
    assert blocked_redeem.status_code == 409

    complete = client.post('/api/rewards/repair/complete', json={'repair_id': repair['id']})
    assert complete.status_code == 200
    complete_payload = complete.get_json()
    finished = next((row for row in complete_payload['state']['repair_tasks'] if row['id'] == repair['id']), None)
    assert finished is not None
    assert finished['completed'] is True

    allowed_stars = client.post('/api/rewards/stars', json={'child_name': 'Avery', 'delta': 1})
    assert allowed_stars.status_code == 200


def test_rewards_settings_admin_only(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Avery'
    denied = client.put('/api/rewards/settings', json={'confetti_enabled': False})
    assert denied.status_code == 403


def test_rewards_redeem_creates_parent_todo_items(client, app):
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'
    client.post('/api/rewards/stars', json={'child_name': 'Harley', 'delta': 9})
    state = client.get('/api/rewards/state').get_json()['state']
    option = state['reward_options_by_child']['Harley'][0]
    client.post('/api/rewards/stars', json={'child_name': 'Harley', 'delta': option['stars_required']})
    redeem = client.post('/api/rewards/redeem', json={
        'child_name': 'Harley',
        'reward_option_id': option['id'],
        'requires_parent_action': True,
    })
    assert redeem.status_code == 200
    with app.app_context():
        rows = KidRewardParentActionTask.query.all()
        assert len(rows) == 2
        chores = Chore.query.filter(Chore.task_type == 'todo').all()
        assert len([row for row in chores if 'Reward follow-through for Harley' in (row.description or '')]) == 2


def test_parent_reward_follow_through_goes_to_other_parent_only(client, app):
    with client.session_transaction() as session:
        session['current_member'] = 'Mykal'
    client.post('/api/rewards/stars', json={'child_name': 'Rachel', 'delta': 10})
    client.post('/api/rewards/stars', json={'child_name': 'Rachel', 'delta': 10})
    state = client.get('/api/rewards/state').get_json()['state']
    option = state['reward_options_by_child']['Rachel'][0]
    redeem = client.post('/api/rewards/redeem', json={
        'child_name': 'Rachel',
        'reward_option_id': option['id'],
        'requires_parent_action': True,
    })
    assert redeem.status_code == 200
    payload = redeem.get_json()
    task_rows = [row for row in payload['state']['parent_action_tasks'] if row['child_name'] == 'Rachel']
    assert len(task_rows) == 1
    assert task_rows[0]['parent_name'] == 'Mykal'


def test_parent_helper_notifications_emit_meal_plan_and_inspiration(app):
    with app.app_context():
        sunday = datetime(2026, 11, 15, 15, 5, tzinfo=timezone.utc)  # 10:05 AM Eastern Sunday
        sent = emit_due_parent_helper_notifications_for_member('Rachel', now_utc=sunday)
        assert sent >= 1
        sent_non_parent = emit_due_parent_helper_notifications_for_member('Avery', now_utc=sunday)
        assert sent_non_parent == 0
        rows = AppNotification.query.filter_by(event_type='meal_plan_helper').all()
        assert rows


def test_welcome_parent_helper_card_shows_debug_for_rachel(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    response = client.get('/')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Parent Helper' in html
    assert 'This Sunday meal-plan status' in html
    assert "Kids' screen" in html
    assert 'id="goldStarLiveRows"' in html
    # The old discipline summary was retired with Gold Star Club v1.
    assert 'Weekly Star Behavior' not in html
    assert 'Consequence reasons this week' not in html
    assert 'id="quickStarModal"' in html
    assert 'id="quickConsequenceModal"' in html
    assert 'Debug: next inspirational-notification windows' not in html


def test_welcome_parent_helper_card_hides_debug_for_non_admin(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Avery'
    response = client.get('/')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Parent Helper' in html
    assert 'This Sunday meal-plan status' in html
    assert "Kids' screen" in html
    assert 'text-xs gs-quick-star' not in html  # only parents get the +1 Star button
    assert 'id="quickStarModal"' not in html
    assert 'id="quickConsequenceModal"' not in html
    assert 'Debug: next inspirational-notification windows' not in html


def test_gold_stars_kid_view_hides_admin_controls(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    response = client.get('/gold-stars/legacy?kid_view=1')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Kid-safe mode is on.' in html
    assert 'Admin: Edit Do &amp; Don&#39;t Rules' not in html
    assert 'id="rewardOptionsEditor" class="border-t dark:border-gray-700 pt-4 hidden"' in html


def test_kids_stars_tablet_view_renders(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Avery'
    response = client.get('/kids-stars/legacy')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Kid Star Board' in html
    assert 'Tap claim when a parent dispenses stars to you.' in html


def test_rewards_options_can_be_set_per_child_with_up_to_three(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    update = client.put('/api/rewards/reward-options', json={
        'options': {
            'Avery': [
                {'reward_label': 'Pick family dance party song', 'stars_required': 6},
                {'reward_label': 'Choose bedtime movie', 'stars_required': 11},
                {'reward_label': 'Special game helper', 'stars_required': 18},
            ],
            'Harley': [
                {'reward_label': 'Choose snack', 'stars_required': 5},
                {'reward_label': 'Pick storybook', 'stars_required': 9},
            ],
            'Rachel': [
                {'reward_label': 'Pick family dinner', 'stars_required': 8},
            ],
            'Mykal': [
                {'reward_label': 'Pick weekend game', 'stars_required': 7},
            ],
        },
    })
    assert update.status_code == 200
    payload = update.get_json()
    assert payload['ok'] is True
    avery_options = payload['state']['reward_options_by_child']['Avery']
    harley_options = payload['state']['reward_options_by_child']['Harley']
    rachel_options = payload['state']['reward_options_by_child']['Rachel']
    mykal_options = payload['state']['reward_options_by_child']['Mykal']
    assert len(avery_options) == 3
    assert len(harley_options) == 2
    assert len(rachel_options) == 1
    assert len(mykal_options) == 1
    assert avery_options[0]['reward_label'] == 'Pick family dance party song'
    assert avery_options[0]['stars_required'] == 6


def test_link_existing_local_member_to_supabase_workflow(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    created = client.post('/api/family-members', json={'name': 'LocalKid', 'role': 'child', 'password': 'secret12'})
    assert created.status_code == 200
    member_id = created.get_json()['member']['id']

    linked = client.post(
        f'/api/family-members/{member_id}/link-supabase',
        json={'email': 'localkid@example.com', 'password': 'secret12'},
    )
    assert linked.status_code == 200
    payload = linked.get_json()
    assert payload['ok'] is True
    assert payload['member']['email'] == 'localkid@example.com'
    assert payload['member']['supabase_linked'] is True


def test_kiosk_mode_locks_non_admin_to_kids_view(client):
    with client.application.app_context():
        rachel = FamilyMember.query.filter_by(name='Rachel').first()
        if not rachel:
            rachel = FamilyMember(name='Rachel', role='admin')
            db.session.add(rachel)
        rachel.pin = '1234'
        db.session.commit()

    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    enabled = client.post('/api/rewards/kiosk-mode', json={'enabled': True})
    assert enabled.status_code == 200
    assert enabled.get_json()['enabled'] is True

    with client.session_transaction() as session:
        session['current_member'] = 'Avery'
    blocked_api = client.get('/api/chores')
    assert blocked_api.status_code == 403
    blocked_page = client.get('/settings')
    assert blocked_page.status_code in {301, 302}
    assert '/kids-stars' in (blocked_page.headers.get('Location') or '')
    kids_page = client.get('/kids-stars')
    assert kids_page.status_code == 200

    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    disabled_without_pin = client.post('/api/rewards/kiosk-mode', json={'enabled': False})
    assert disabled_without_pin.status_code == 400
    disabled_bad_pin = client.post('/api/rewards/kiosk-mode', json={'enabled': False, 'parent_name': 'Rachel', 'unlock_pin': '0000'})
    assert disabled_bad_pin.status_code == 403
    disabled = client.post('/api/rewards/kiosk-mode', json={'enabled': False, 'parent_name': 'Rachel', 'unlock_pin': '1234'})
    assert disabled.status_code == 200
    assert disabled.get_json()['enabled'] is False


def test_rewards_catalog_adds_reward_with_slider_and_applies_to_members(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    update = client.put('/api/rewards/reward-options', json={
        'rewards': [
            {
                'reward_label': 'Pick family dance party song',
                'stars_required': 6,
                'applies_to': ['Avery', 'Harley'],
            },
            {
                'reward_label': 'Pick Friday dinner',
                'stars_required': 12,
                'applies_to': ['Rachel', 'Mykal'],
            },
        ],
    })
    assert update.status_code == 200
    payload = update.get_json()
    assert payload['ok'] is True
    catalog = payload['state']['reward_options']
    assert len(catalog) == 2
    assert catalog[0]['stars_required'] == 6
    assert sorted(catalog[0]['applies_to']) == ['Avery', 'Harley']
    assert payload['state']['reward_options_by_child']['Avery'][0]['reward_label'] == 'Pick family dance party song'
    assert payload['state']['reward_options_by_child']['Mykal'][0]['reward_label'] == 'Pick Friday dinner'


def test_rewards_dispense_then_child_claims_stars(client, app):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    dispense = client.post('/api/rewards/dispense', json={'child_name': 'Avery', 'stars_awarded': 2, 'reason': 'Great listening'})
    assert dispense.status_code == 200
    state = dispense.get_json()['state']
    grants = state['pending_star_grants']
    assert grants
    assert grants[0]['child_name'] == 'Avery'
    with app.app_context():
        rows = KidRewardPendingGrant.query.filter_by(child_name='Avery', claimed=False).all()
        assert len(rows) == 1

    with client.session_transaction() as session:
        session['current_member'] = 'Avery'
    claim = client.post('/api/rewards/claim', json={'grant_id': grants[0]['id']})
    assert claim.status_code == 200
    payload = claim.get_json()
    avery = next((row for row in payload['state']['children'] if row['child_name'] == 'Avery'), None)
    assert avery is not None
    assert avery['stars'] == 2
    assert avery['pending_stars'] == 0
    with app.app_context():
        row = KidRewardPendingGrant.query.get(grants[0]['id'])
        assert row is not None
        assert bool(row.claimed) is True


def test_gold_star_preview_requires_parent_or_admin(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Avery'
    page = client.get('/gold-stars/preview')
    assert page.status_code == 403
    character_lab_page = client.get('/gold-stars/preview/character-lab')
    assert character_lab_page.status_code == 403
    child_page = client.get('/gold-stars/preview/child/Avery')
    assert child_page.status_code == 403
    api_state = client.get('/api/gold-stars/preview/state')
    assert api_state.status_code == 403
    payload = api_state.get_json()
    assert payload['ok'] is False

    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    allowed_page = client.get('/gold-stars/preview')
    assert allowed_page.status_code == 200
    html = allowed_page.get_data(as_text=True)
    assert 'Gold Star Club preview' in html
    assert 'Overview' in html
    assert 'Reduced motion override' in html

    allowed_character_lab_page = client.get('/gold-stars/preview/character-lab')
    assert allowed_character_lab_page.status_code == 200
    character_lab_html = allowed_character_lab_page.get_data(as_text=True)
    assert '3D Character Lab' in character_lab_html
    assert 'Review mode' in character_lab_html


def test_gold_star_preview_two_child_independent_accounting_and_collect_once(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'
    initial = client.get('/api/gold-stars/preview/state').get_json()['state']
    assert initial['children']['Avery']['earned'] == 32
    assert initial['children']['Avery']['available'] == 7
    assert initial['children']['Avery']['spent'] == 25
    assert initial['children']['Harley']['earned'] == 24
    assert initial['children']['Harley']['available'] == 4
    assert initial['children']['Harley']['spent'] == 20

    award = client.post('/api/gold-stars/preview/awards', json={'child_name': 'Avery', 'note': 'Good listening'})
    assert award.status_code == 200
    award_state = award.get_json()['state']
    assert award_state['children']['Avery']['earned'] == 33
    assert award_state['children']['Avery']['waiting_count'] == 1
    assert award_state['children']['Harley']['earned'] == 24
    waiting_id = award_state['children']['Avery']['waiting_awards'][0]['id']

    collect = client.post('/api/gold-stars/preview/awards/collect', json={'child_name': 'Avery', 'award_id': waiting_id})
    assert collect.status_code == 200
    collect_state = collect.get_json()['state']
    assert collect_state['children']['Avery']['available'] == 8
    assert collect_state['children']['Avery']['waiting_count'] == 0
    assert collect_state['children']['Avery']['earned'] == 33
    assert collect_state['children']['Avery']['invariant_ok'] is True
    assert collect_state['children']['Harley']['available'] == 4

    duplicate_collect = client.post('/api/gold-stars/preview/awards/collect', json={'child_name': 'Avery', 'award_id': waiting_id})
    assert duplicate_collect.status_code == 409


def test_gold_star_preview_catalog_validation_and_eligibility(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'

    invalid_cost = client.post('/api/gold-stars/preview/catalog', json={
        'name': 'Bad cost',
        'cost': 0,
        'image_ref': 'star',
        'eligible_children': ['Avery'],
    })
    assert invalid_cost.status_code == 400

    create_reward = client.post('/api/gold-stars/preview/catalog', json={
        'name': 'Avery only treat',
        'cost': 6,
        'image_ref': 'treat',
        'eligible_children': ['Avery'],
        'active': True,
    })
    assert create_reward.status_code == 200
    reward_id = create_reward.get_json()['reward_id']

    harley_request = client.post('/api/gold-stars/preview/requests', json={'child_name': 'Harley', 'reward_id': reward_id})
    assert harley_request.status_code == 400

    avery_request = client.post('/api/gold-stars/preview/requests', json={'child_name': 'Avery', 'reward_id': reward_id})
    assert avery_request.status_code == 200
    duplicate_avery_request = client.post('/api/gold-stars/preview/requests', json={'child_name': 'Avery', 'reward_id': reward_id})
    assert duplicate_avery_request.status_code == 409


def test_gold_star_preview_request_approval_decline_and_delivery_flow(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'

    request = client.post('/api/gold-stars/preview/requests', json={'child_name': 'Avery', 'reward_id': 1})
    assert request.status_code == 200
    request_id = request.get_json()['request_id']

    duplicate_request = client.post('/api/gold-stars/preview/requests', json={'child_name': 'Avery', 'reward_id': 2})
    assert duplicate_request.status_code == 409

    approve = client.post(f'/api/gold-stars/preview/requests/{request_id}/approve', json={})
    assert approve.status_code == 200
    approved = approve.get_json()['state']
    assert approved['children']['Avery']['available'] == 2
    assert approved['children']['Avery']['earned'] == 32
    assert approved['children']['Avery']['spent'] == 30
    assert approved['children']['Avery']['invariant_ok'] is True
    delivery_id = approved['delivery_queue'][0]['id']

    repeated_approve = client.post(f'/api/gold-stars/preview/requests/{request_id}/approve', json={})
    assert repeated_approve.status_code == 409

    deliver = client.post(f'/api/gold-stars/preview/delivery/{delivery_id}/deliver', json={})
    assert deliver.status_code == 200
    delivered = deliver.get_json()['state']
    assert delivered['delivery_queue'][0]['status'] == 'delivered'

    repeated_delivery = client.post(f'/api/gold-stars/preview/delivery/{delivery_id}/deliver', json={})
    assert repeated_delivery.status_code == 409

    decline_missing_pending = client.post(f'/api/gold-stars/preview/requests/{request_id}/decline', json={})
    assert decline_missing_pending.status_code == 409

    mini_reward = client.post('/api/gold-stars/preview/catalog', json={
        'name': 'Harley mini choice',
        'cost': 3,
        'image_ref': 'star',
        'eligible_children': ['Harley'],
    })
    assert mini_reward.status_code == 200
    mini_reward_id = mini_reward.get_json()['reward_id']
    harley_request = client.post('/api/gold-stars/preview/requests', json={'child_name': 'Harley', 'reward_id': mini_reward_id})
    assert harley_request.status_code == 200
    harley_request_id = harley_request.get_json()['request_id']
    decline = client.post(f'/api/gold-stars/preview/requests/{harley_request_id}/decline', json={})
    assert decline.status_code == 200
    declined = decline.get_json()['state']
    declined_row = next((row for row in declined['requests'] if row['id'] == harley_request_id), None)
    assert declined_row is not None
    assert declined_row['status'] == 'declined'


def test_gold_star_preview_price_change_archive_invalidation_and_reset_scope(client):
    with client.session_transaction() as session:
        session['current_member'] = 'Rachel'

    request = client.post('/api/gold-stars/preview/requests', json={'child_name': 'Harley', 'reward_id': 1})
    assert request.status_code == 400  # Harley starts with 4, reward #1 costs 5.

    award_harley = client.post('/api/gold-stars/preview/awards', json={'child_name': 'Harley', 'note': 'Setup request test'})
    assert award_harley.status_code == 200
    waiting_id = award_harley.get_json()['state']['children']['Harley']['waiting_awards'][0]['id']
    collect_harley = client.post('/api/gold-stars/preview/awards/collect', json={'child_name': 'Harley', 'award_id': waiting_id})
    assert collect_harley.status_code == 200

    request_ok = client.post('/api/gold-stars/preview/requests', json={'child_name': 'Harley', 'reward_id': 1})
    assert request_ok.status_code == 200
    request_id = request_ok.get_json()['request_id']

    price_change = client.put('/api/gold-stars/preview/catalog/1', json={'cost': 6})
    assert price_change.status_code == 200
    changed_state = price_change.get_json()['state']
    target = next((row for row in changed_state['requests'] if row['id'] == request_id), None)
    assert target is not None
    assert target['status'] == 'invalidated'

    request_again = client.post('/api/gold-stars/preview/requests', json={'child_name': 'Harley', 'reward_id': 2})
    assert request_again.status_code == 400

    archive = client.post('/api/gold-stars/preview/catalog/2/archive', json={})
    assert archive.status_code == 200

    reset = client.post('/api/gold-stars/preview/reset', json={})
    assert reset.status_code == 200
    reset_state = reset.get_json()['state']
    assert reset_state['children']['Avery']['earned'] == 32
    assert reset_state['children']['Avery']['available'] == 7
    assert reset_state['children']['Avery']['spent'] == 25
    assert reset_state['children']['Harley']['earned'] == 24
    assert reset_state['children']['Harley']['available'] == 4
    assert reset_state['children']['Harley']['spent'] == 20
    assert reset_state['requests'] == []
