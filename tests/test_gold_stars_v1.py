"""Gold Star Club v1 (persistent) — accounting and permission rules."""
import pytest

from app import create_app, db
from app.models import FamilyMember, GoldStarPurchase


@pytest.fixture()
def app():
    app = create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'SECRET_KEY': 'test',
        'HOMEHUB_CONFIG': {
            'admin_name': 'Rachel',
            'family_members': ['Rachel', 'Mykal', 'Avery', 'Harley'],
            'feature_toggles': {'reward_system': True},
            'menu_visibility': {'reward_system': True},
        },
    })
    with app.app_context():
        db.create_all()
        for name, role in [('Rachel', 'admin'), ('Mykal', 'admin'), ('Avery', 'child'), ('Harley', 'child')]:
            if not FamilyMember.query.filter_by(name=name).first():
                db.session.add(FamilyMember(name=name, role=role))
        db.session.commit()
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def as_member(client, name):
    with client.session_transaction() as session:
        session['current_member'] = name


def state(client):
    return client.get('/api/gold-stars/state').get_json()['state']


def check_invariant(child):
    assert child['earned'] == child['available'] + child['waiting_count'] + child['spent']
    assert child['invariant_ok'] is True


def test_fresh_start_is_empty_and_children_come_from_roles(client):
    as_member(client, 'Rachel')
    s = state(client)
    assert s['child_names'] == ['Avery', 'Harley']
    for name in ('Avery', 'Harley'):
        assert s['children'][name]['earned'] == 0
        assert s['children'][name]['available'] == 0
    assert s['catalog'] == []


def test_award_collect_once_and_double_tap_token(client):
    as_member(client, 'Mykal')
    r1 = client.post('/api/gold-stars/awards', json={'child_name': 'Avery', 'note': 'Shared toys', 'client_token': 'tap-1'})
    r2 = client.post('/api/gold-stars/awards', json={'child_name': 'Avery', 'note': 'Shared toys', 'client_token': 'tap-1'})
    assert r1.status_code == 200 and r2.status_code == 200
    avery = r2.get_json()['state']['children']['Avery']
    assert avery['earned'] == 1 and avery['waiting_count'] == 1 and avery['available'] == 0
    award_id = avery['waiting_awards'][0]['id']

    as_member(client, 'Avery')
    ok = client.post('/api/gold-stars/awards/collect', json={'child_name': 'Avery', 'award_id': award_id})
    assert ok.status_code == 200
    again = client.post('/api/gold-stars/awards/collect', json={'child_name': 'Avery', 'award_id': award_id})
    assert again.status_code == 409
    avery = ok.get_json()['state']['children']['Avery']
    assert avery['earned'] == 1 and avery['available'] == 1 and avery['waiting_count'] == 0
    check_invariant(avery)


def test_child_cannot_award_or_collect_for_sibling_and_sees_only_self(client):
    as_member(client, 'Rachel')
    award = client.post('/api/gold-stars/awards', json={'child_name': 'Harley'}).get_json()
    harley_award = award['state']['children']['Harley']['waiting_awards'][0]['id']

    as_member(client, 'Avery')
    assert client.post('/api/gold-stars/awards', json={'child_name': 'Avery'}).status_code == 403
    assert client.post('/api/gold-stars/awards/collect', json={'child_name': 'Harley', 'award_id': harley_award}).status_code == 403
    s = state(client)
    assert list(s['children'].keys()) == ['Avery']
    assert all(r['child_name'] == 'Avery' for r in s['requests'])


def _give_and_collect(client, child, n):
    as_member(client, 'Rachel')
    for _ in range(n):
        client.post('/api/gold-stars/awards', json={'child_name': child})
    s = state(client)
    as_member(client, child)
    for a in s['children'][child]['waiting_awards']:
        client.post('/api/gold-stars/awards/collect', json={'child_name': child, 'award_id': a['id']})


def test_request_approve_deducts_exactly_once_and_earned_never_drops(client):
    as_member(client, 'Rachel')
    reward = client.post('/api/gold-stars/catalog', json={'name': 'Ice cream', 'cost': 3, 'image_ref': 'dessert', 'eligible_children': ['Avery']}).get_json()
    reward_id = reward['reward_id']
    _give_and_collect(client, 'Avery', 5)

    as_member(client, 'Avery')
    req = client.post('/api/gold-stars/requests', json={'child_name': 'Avery', 'reward_id': reward_id})
    assert req.status_code == 200
    request_id = req.get_json()['request_id']
    # no deduction yet, and only one pending request at a time
    assert req.get_json()['state']['children']['Avery']['available'] == 5
    assert client.post('/api/gold-stars/requests', json={'child_name': 'Avery', 'reward_id': reward_id}).status_code == 409
    # children cannot approve
    assert client.post(f'/api/gold-stars/requests/{request_id}/approve').status_code == 403

    as_member(client, 'Rachel')
    first = client.post(f'/api/gold-stars/requests/{request_id}/approve')
    as_member(client, 'Mykal')
    second = client.post(f'/api/gold-stars/requests/{request_id}/approve')
    assert first.status_code == 200
    assert second.status_code == 409
    avery = second.get_json()['state']['children']['Avery']
    assert avery['available'] == 2 and avery['spent'] == 3 and avery['earned'] == 5
    check_invariant(avery)
    with client.application.app_context():
        assert GoldStarPurchase.query.count() == 1

    delivery_id = first.get_json()['delivery_id']
    assert client.post(f'/api/gold-stars/delivery/{delivery_id}/deliver').status_code == 200
    assert client.post(f'/api/gold-stars/delivery/{delivery_id}/deliver').status_code == 409


def test_price_change_invalidates_pending_request_never_charges_new_price(client):
    as_member(client, 'Rachel')
    reward_id = client.post('/api/gold-stars/catalog', json={'name': 'Park', 'cost': 2, 'eligible_children': ['Harley']}).get_json()['reward_id']
    _give_and_collect(client, 'Harley', 4)
    as_member(client, 'Harley')
    request_id = client.post('/api/gold-stars/requests', json={'child_name': 'Harley', 'reward_id': reward_id}).get_json()['request_id']

    as_member(client, 'Rachel')
    client.put(f'/api/gold-stars/catalog/{reward_id}', json={'cost': 3})
    s = state(client)
    req = next(r for r in s['requests'] if r['id'] == request_id)
    assert req['status'] == 'invalidated'
    assert 'price changed' in req['status_message']
    assert client.post(f'/api/gold-stars/requests/{request_id}/approve').status_code == 409
    assert s['children']['Harley']['available'] == 4


def test_not_enough_stars_and_cost_validation(client):
    as_member(client, 'Rachel')
    assert client.post('/api/gold-stars/catalog', json={'name': 'X', 'cost': 0, 'eligible_children': ['Avery']}).status_code == 400
    assert client.post('/api/gold-stars/catalog', json={'name': 'X', 'cost': 51, 'eligible_children': ['Avery']}).status_code == 400
    assert client.post('/api/gold-stars/catalog', json={'name': 'X', 'cost': 2.5, 'eligible_children': ['Avery']}).status_code == 400
    assert client.post('/api/gold-stars/catalog', json={'name': 'X', 'cost': 2, 'eligible_children': []}).status_code == 400
    reward_id = client.post('/api/gold-stars/catalog', json={'name': 'Big', 'cost': 10, 'eligible_children': ['Avery']}).get_json()['reward_id']
    as_member(client, 'Avery')
    assert client.post('/api/gold-stars/requests', json={'child_name': 'Avery', 'reward_id': reward_id}).status_code == 409


def test_corrections_are_audited_and_keep_invariant(client):
    as_member(client, 'Rachel')
    award = client.post('/api/gold-stars/awards', json={'child_name': 'Avery'}).get_json()
    award_id = award['state']['children']['Avery']['waiting_awards'][0]['id']
    assert client.post(f'/api/gold-stars/awards/{award_id}/void', json={}).status_code == 400  # reason required
    voided = client.post(f'/api/gold-stars/awards/{award_id}/void', json={'reason': 'Wrong child'})
    assert voided.status_code == 200
    avery = voided.get_json()['state']['children']['Avery']
    assert avery['earned'] == 0
    assert any(e['event_type'] == 'corrected' for e in voided.get_json()['state']['history'])

    reward_id = client.post('/api/gold-stars/catalog', json={'name': 'Toy', 'cost': 2, 'eligible_children': ['Avery']}).get_json()['reward_id']
    _give_and_collect(client, 'Avery', 2)
    as_member(client, 'Avery')
    request_id = client.post('/api/gold-stars/requests', json={'child_name': 'Avery', 'reward_id': reward_id}).get_json()['request_id']
    as_member(client, 'Rachel')
    delivery_id = client.post(f'/api/gold-stars/requests/{request_id}/approve').get_json()['delivery_id']
    refunded = client.post(f'/api/gold-stars/delivery/{delivery_id}/refund', json={'reason': 'Shop was closed'})
    avery = refunded.get_json()['state']['children']['Avery']
    assert avery['available'] == 2 and avery['spent'] == 0 and avery['earned'] == 2
    check_invariant(avery)
    assert client.post(f'/api/gold-stars/delivery/{delivery_id}/refund', json={'reason': 'again'}).status_code == 409


def test_pages_route_parents_and_children(client):
    as_member(client, 'Rachel')
    page = client.get('/gold-stars')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'Gold Star Club' in html and '/api/gold-stars' in html
    assert 'Reset preview state' not in html
    legacy = client.get('/gold-stars/legacy')
    assert legacy.status_code == 200

    as_member(client, 'Avery')
    redirected = client.get('/gold-stars')
    assert redirected.status_code in (301, 302)
    assert '/kids-stars' in redirected.headers['Location']
    kid = client.get('/kids-stars')
    assert kid.status_code == 200
    kid_html = kid.get_data(as_text=True)
    assert 'Grown-ups only' in kid_html
    assert 'Harley' not in kid_html.split('const initialState')[1].split(';')[0]


# ---------------------------------------------------------------- access tiers + invites

def test_extended_family_sees_only_extended_modules_and_shared_events(client, app):
    from app.models import FamilyCalendarEvent
    from datetime import date
    from app.permissions import can_access_app_module
    with app.app_context():
        db.session.add(FamilyMember(name='Grandma', role='standard', access_tier='extended', email='g@example.com'))
        db.session.add(FamilyMember(name='Aunt', role='standard', access_tier='immediate'))
        db.session.add(FamilyCalendarEvent(title='Household dentist', start_date=date(2026, 12, 1), end_date=date(2026, 12, 1), visibility='family', created_by='Rachel', invited_members_json='[]'))
        db.session.add(FamilyCalendarEvent(title='Thanksgiving', start_date=date(2026, 12, 2), end_date=date(2026, 12, 2), visibility='everyone', created_by='Rachel', invited_members_json='[]'))
        db.session.commit()
    # permission checks read the signed-in session, so they need a request context
    with app.test_request_context('/'):
        assert can_access_app_module('shared_calendar', 'Grandma') is True
        assert can_access_app_module('shopping_list', 'Grandma') is True
        assert can_access_app_module('cameras', 'Grandma') is False
        assert can_access_app_module('reward_system', 'Grandma') is False
        assert can_access_app_module('cameras', 'Aunt') is True
        assert can_access_app_module('settings', 'Aunt') is False

    as_member(client, 'Grandma')
    events = client.get('/api/calendar/events?start=2026-12-01&end=2026-12-03').get_json()
    titles = {e['title'] for e in events.get('events', [])}
    assert 'Thanksgiving' in titles
    assert 'Household dentist' not in titles


def test_invite_flow_creates_one_time_link_and_sets_pin(client, app):
    with app.app_context():
        db.session.add(FamilyMember(name='Grandpa', role='standard', access_tier='extended', email='gp@example.com'))
        db.session.commit()
        member_id = FamilyMember.query.filter_by(name='Grandpa').first().id
    as_member(client, 'Rachel')
    preview = client.get(f'/api/family-members/{member_id}/invite-preview').get_json()
    assert preview['ok'] and 'Grandpa' in preview['html'] and 'What you can see' in preview['html']
    sent = client.post(f'/api/family-members/{member_id}/invite').get_json()
    assert sent['ok'] is True
    token = sent['invite_url'].rsplit('/', 1)[1]

    as_member(client, 'Avery')
    assert client.post(f'/api/family-members/{member_id}/invite').status_code == 403

    with client.session_transaction() as session:
        session.clear()
    page = client.get(f'/invite/{token}')
    assert page.status_code == 200 and 'Welcome, Grandpa!' in page.get_data(as_text=True)
    assert client.post(f'/api/invite/{token}/accept', json={'pin': '12'}).status_code == 400
    ok = client.post(f'/api/invite/{token}/accept', json={'pin': '4321'})
    assert ok.status_code == 200
    with client.session_transaction() as session:
        assert session['current_member'] == 'Grandpa'
    reused = client.post(f'/api/invite/{token}/accept', json={'pin': '4321'})
    assert reused.status_code == 400
    with app.app_context():
        assert FamilyMember.query.filter_by(name='Grandpa').first().pin == '4321'


# ---------------------------------------------------------------- home screen

def test_home_screen_summary_memos_and_visibility(client, app):
    with app.app_context():
        db.session.add(FamilyMember(name='Grandma', role='standard', access_tier='extended'))
        db.session.commit()
    as_member(client, 'Rachel')
    assert client.get('/home-screen').status_code == 200
    summary = client.get('/api/home-screen/summary').get_json()
    assert summary['ok'] and summary['access']['shopping_list'] is True
    assert summary['gold_stars'] is not None
    assert client.post('/api/home-screen/memos', json={'text': ''}).status_code == 400
    client.post('/api/home-screen/memos', json={'text': 'Household only memo', 'visibility': 'family'})
    client.post('/api/home-screen/memos', json={'text': 'Everyone memo', 'visibility': 'everyone'})

    as_member(client, 'Grandma')
    summary = client.get('/api/home-screen/summary').get_json()
    texts = [m['text'] for m in summary['memos']]
    assert 'Everyone memo' in texts and 'Household only memo' not in texts
    assert summary['access']['reward_system'] is False
    assert summary['gold_stars'] is None
    # extended family land on the home screen instead of the household welcome page
    landing = client.get('/')
    assert landing.status_code in (301, 302) and '/home-screen' in landing.headers['Location']


def _set_pin(app, name, pin):
    with app.app_context():
        row = FamilyMember.query.filter_by(name=name).first()
        row.pin = pin
        db.session.commit()


def _poster_child(body, name):
    return next(c for c in body['poster']['children'] if c['name'] == name)


def test_star_poster_kids_need_a_released_star_or_parent_pin(client, app):
    from datetime import datetime, timedelta
    week_start = (datetime.utcnow() - timedelta(days=3)).isoformat() + 'Z'
    _set_pin(app, 'Mykal', '2468')
    # the shared screen is signed in as a parent, but a kid tapping still does not count
    as_member(client, 'Mykal')
    tapped = client.post('/api/gold-stars/poster/add', json={'child_name': 'Avery', 'week_start': week_start})
    assert tapped.status_code == 403
    body = tapped.get_json()
    assert body['code'] == 'needs_parent' and 'parent' in body['error'].lower()
    assert _poster_child(body, 'Avery')['all_time'] == 0
    assert body['poster']['parents'] == ['Mykal']
    # giving without parent mode is refused too
    assert client.post('/api/gold-stars/poster/add', json={'child_name': 'Avery', 'mode': 'give'}).status_code == 403

    # a star a parent released can be stuck by the kid
    client.post('/api/gold-stars/awards', json={'child_name': 'Avery', 'note': 'Cleaned up'})
    stuck = client.post('/api/gold-stars/poster/add', json={'child_name': 'Avery', 'week_start': week_start})
    assert stuck.status_code == 200
    avery = _poster_child(stuck.get_json(), 'Avery')
    assert avery['waiting'] == 0 and avery['all_time'] == 1 and len(avery['week_stars']) == 1

    # wrong PIN, then right PIN opens parent mode
    assert client.post('/api/gold-stars/poster/unlock', json={'parent': 'Mykal', 'pin': '0000'}).status_code == 403
    unlocked = client.post('/api/gold-stars/poster/unlock', json={'parent': 'Mykal', 'pin': '2468'})
    assert unlocked.status_code == 200 and unlocked.get_json()['poster']['parent_mode']['by'] == 'Mykal'
    given = client.post('/api/gold-stars/poster/add', json={'child_name': 'Harley', 'mode': 'give', 'note': 'Kind words', 'client_token': 't1', 'week_start': week_start})
    assert given.status_code == 200
    harley = _poster_child(given.get_json(), 'Harley')
    assert harley['all_time'] == 1 and harley['week_stars'][0]['note'] == 'Kind words' and harley['week_stars'][0]['by'] == 'Mykal'

    # locking ends parent mode
    client.post('/api/gold-stars/poster/lock')
    assert client.post('/api/gold-stars/poster/add', json={'child_name': 'Harley', 'mode': 'give'}).status_code == 403


def test_star_poster_backdate_edit_remove_and_lockout(client, app):
    from datetime import datetime, timedelta
    _set_pin(app, 'Rachel', '1357')
    as_member(client, 'Rachel')
    week_start = (datetime.utcnow() - timedelta(days=6)).isoformat() + 'Z'
    client.post('/api/gold-stars/poster/unlock', json={'parent': 'Rachel', 'pin': '1357'})
    two_days_ago = (datetime.utcnow() - timedelta(days=2)).replace(hour=17, minute=0).isoformat() + 'Z'
    added = client.post('/api/gold-stars/poster/add', json={'child_name': 'Avery', 'mode': 'give', 'note': 'Helped with dishes', 'when': two_days_ago, 'week_start': week_start})
    assert added.status_code == 200
    star = _poster_child(added.get_json(), 'Avery')['week_stars'][0]
    assert star['at'].startswith(two_days_ago[:10])
    # future days are refused
    tomorrow = (datetime.utcnow() + timedelta(days=2)).isoformat() + 'Z'
    assert client.post('/api/gold-stars/poster/add', json={'child_name': 'Avery', 'mode': 'give', 'when': tomorrow}).status_code == 400

    # edit description and move to yesterday
    yesterday = (datetime.utcnow() - timedelta(days=1)).replace(hour=17, minute=0).isoformat() + 'Z'
    edited = client.post(f"/api/gold-stars/poster/stars/{star['id']}/edit", json={'note': 'Helped with the dishes', 'when': yesterday, 'week_start': week_start})
    assert edited.status_code == 200
    star2 = _poster_child(edited.get_json(), 'Avery')['week_stars'][0]
    assert star2['note'] == 'Helped with the dishes' and star2['at'].startswith(yesterday[:10])
    history = client.get('/api/gold-stars/state').get_json()['state']['history']
    assert any(h['event_type'] == 'edited' for h in history)

    # remove needs a reason, then counts drop and history records it
    assert client.post(f"/api/gold-stars/poster/stars/{star['id']}/remove", json={}).status_code == 400
    removed = client.post(f"/api/gold-stars/poster/stars/{star['id']}/remove", json={'reason': 'Wrong child', 'week_start': week_start})
    assert removed.status_code == 200
    assert _poster_child(removed.get_json(), 'Avery')['all_time'] == 0

    # five wrong PINs lock the PIN pad for a while
    client.post('/api/gold-stars/poster/lock')
    for _ in range(5):
        client.post('/api/gold-stars/poster/unlock', json={'parent': 'Rachel', 'pin': '9999'})
    assert client.post('/api/gold-stars/poster/unlock', json={'parent': 'Rachel', 'pin': '1357'}).status_code == 429


def test_home_screen_inspiration_seed_add_and_household_greeting(client, app):
    with app.app_context():
        db.session.add(FamilyMember(name='Grandma', role='standard', access_tier='extended'))
        db.session.commit()
    as_member(client, 'Rachel')
    items = client.get('/api/home-screen/inspiration').get_json()['items']
    assert any(i['kind'] == 'verse' and '(CEB)' in i['reference'] for i in items)
    assert any(i['kind'] == 'quote' for i in items)
    assert client.post('/api/home-screen/inspiration', json={'kind': 'verse', 'text': 'x'}).status_code == 400
    ok = client.post('/api/home-screen/inspiration', json={'kind': 'quote', 'text': 'Be kind today.'})
    assert ok.status_code == 200
    summary = client.get('/api/home-screen/summary').get_json()
    assert 'Grandma' not in summary['household'] and 'Avery' in summary['household']
    page = client.get('/home-screen').get_data(as_text=True)
    assert 'MILLITOP TV' in page and 'https://tvadmin.millitop.net/app' in page
    # private memos never show on the shared screen
    client.post('/api/home-screen/memos', json={'text': 'Secret', 'visibility': 'private'})
    assert all(m['text'] != 'Secret' for m in client.get('/api/home-screen/summary').get_json()['memos'])


def test_calendar_event_for_members_create_update_and_partial_edit(client):
    as_member(client, 'Mykal')
    created = client.post('/api/calendar/events', json={
        'title': 'Pig roast', 'start_date': '2026-12-05', 'end_date': '2026-12-05',
        'is_all_day': 'false', 'start_time': '16:00', 'end_time': '20:00',
    })
    assert created.status_code == 200
    event = created.get_json()['event']
    assert event['for_members'] == [] and event['created_by'] == 'Mykal'
    # set who it's for without touching anything else (home screen "who is it for")
    updated = client.put(f"/api/calendar/events/{event['id']}", json={'for_members': ['Mykal', 'Rachel']})
    assert updated.status_code == 200
    ev2 = updated.get_json()['event']
    assert ev2['for_members'] == ['Mykal', 'Rachel']
    assert ev2['title'] == 'Pig roast' and ev2['start_time'] == '16:00'
    listed = client.get('/api/calendar/events?start=2026-12-01&end=2026-12-07').get_json()['events']
    assert next(e for e in listed if e['title'] == 'Pig roast')['for_members'] == ['Mykal', 'Rachel']
    # created with people from the start
    with_people = client.post('/api/calendar/events', json={'title': 'Swim', 'start_date': '2026-12-06', 'for_members': ['Avery']}).get_json()['event']
    assert with_people['for_members'] == ['Avery']
