"""Always-on family home screen (wall tablet / TV) with quick add.

GET  /home-screen                     the screen
GET  /api/home-screen/summary         who is signed in, what they can open, memos, star counts
POST /api/home-screen/memos           add a memo (fridge-note style)
POST /api/home-screen/memos/<id>/done remove a memo from the board (creator or admin)
"""
import json
from datetime import datetime

from flask import current_app, jsonify, render_template, request, session

from . import main_bp
from .. import db
from ..models import FamilyMember, HomeScreenInspiration, Note
from ..permissions import (
    can_access_app_module,
    has_visibility_access,
    is_admin_name,
    is_extended_member,
    sanitize_visibility,
)
from ..security import sanitize_text


def _actor() -> str:
    return sanitize_text(session.get('current_member') or '').strip()


def _hub_name() -> str:
    try:
        from ..services.invites import hub_name
        return hub_name()
    except Exception:
        return 'Home Hub'


def _memo_dict(note: Note) -> dict:
    return {
        'id': note.id,
        'text': note.title or '',
        'creator': note.creator or '',
        'visibility': note.visibility or 'family',
        'created_at': (note.timestamp.isoformat() + 'Z') if note.timestamp else '',
    }


def _visible_memos(actor: str, limit: int = 12) -> list[dict]:
    """The home screen is shared by the whole family, so it only shows memos
    meant for everyone in the household (never someone's private memos), and
    only what the signed-in account is allowed to see."""
    rows = (
        Note.query.filter(Note.note_type == 'memo')
        .filter(Note.is_task.is_(False))
        .order_by(Note.timestamp.desc())
        .limit(80)
        .all()
    )
    out = []
    for row in rows:
        if (row.visibility or 'family') not in {'family', 'everyone'}:
            continue
        if has_visibility_access(row.visibility or 'family', row.creator or '', row.allowed_members_json or '[]', actor):
            out.append(_memo_dict(row))
        if len(out) >= limit:
            break
    return out


@main_bp.route('/home-screen', methods=['GET'])
def home_screen():
    actor = _actor()
    cfg = current_app.config.get('HOMEHUB_CONFIG', {}) or {}
    import os
    millitoptv_url = str(os.environ.get('HOMEHUB_MILLITOPTV_URL') or cfg.get('millitoptv_url') or 'https://tvadmin.millitop.net').strip()
    return render_template('home_screen.html', actor_name=actor, hub_name=_hub_name(), config=cfg, millitoptv_url=millitoptv_url)


@main_bp.route('/api/home-screen/summary', methods=['GET'])
def api_home_screen_summary():
    actor = _actor()
    if not actor:
        return jsonify({'ok': False, 'error': 'Please sign in.'}), 401
    access = {key: can_access_app_module(key, actor) for key in (
        'shopping_list', 'chores', 'shared_calendar', 'reward_system', 'daily_agenda', 'meal_planner', 'notifications', 'timeline',
    )}
    extended = is_extended_member(actor)
    members = FamilyMember.query.order_by(FamilyMember.added_at).all()
    # Extended family only see/assign other extended members + admins on this screen.
    people = [
        {'name': m.name, 'avatar': m.avatar or '', 'role': m.role or 'standard'}
        for m in members
        if not extended or (m.access_tier or 'immediate') == 'extended' or (m.role or '') == 'admin' or m.name == actor
    ]
    # Names the greeting rotates through: the household (not extended family).
    household = [m.name for m in members if (m.access_tier or 'immediate') != 'extended']
    gold = None
    if access.get('reward_system'):
        try:
            from ..services import gold_stars as gs
            if gs.is_parent(actor):
                state = gs.state_payload(actor)
                gold = {
                    'children': [
                        {'name': c['name'], 'available': c['available'], 'waiting': c['waiting_count']}
                        for c in state['children'].values()
                    ],
                    'pending_requests': state['overview']['pending_requests'],
                    'ready_to_deliver': state['overview']['ready_to_deliver'],
                }
        except Exception:
            gold = None
    return jsonify({
        'ok': True,
        'member': actor,
        'is_admin': is_admin_name(actor),
        'is_extended': extended,
        'access': access,
        'people': people,
        'household': household,
        'memos': _visible_memos(actor),
        'gold_stars': gold,
        'server_time': datetime.utcnow().isoformat() + 'Z',
    })


@main_bp.route('/api/home-screen/memos', methods=['POST'])
def api_home_screen_add_memo():
    actor = _actor()
    if not actor:
        return jsonify({'ok': False, 'error': 'Please sign in.'}), 401
    data = request.get_json(silent=True) or {}
    text = sanitize_text(str(data.get('text') or '')).strip()[:500]
    if not text:
        return jsonify({'ok': False, 'error': 'Write something first.'}), 400
    visibility = sanitize_visibility(data.get('visibility') or 'family')
    if is_extended_member(actor) and visibility == 'family':
        # Household-only memos would be invisible to their own author's group.
        visibility = 'everyone'
    allowed = data.get('for') if isinstance(data.get('for'), list) else []
    allowed = [sanitize_text(str(n)).strip() for n in allowed if sanitize_text(str(n)).strip()]
    if allowed:
        visibility = 'restricted'
    note = Note(
        title=text,
        content=text,
        creator=actor,
        updated_by=actor,
        note_type='memo',
        visibility=visibility,
        allowed_members_json=json.dumps(allowed),
        require_pin=False,
    )
    db.session.add(note)
    db.session.commit()
    return jsonify({'ok': True, 'memo': _memo_dict(note), 'memos': _visible_memos(actor)})


@main_bp.route('/api/home-screen/memos/<int:memo_id>/done', methods=['POST'])
def api_home_screen_memo_done(memo_id: int):
    actor = _actor()
    note = db.session.get(Note, memo_id)
    if not note or note.note_type != 'memo':
        return jsonify({'ok': False, 'error': 'Memo not found.'}), 404
    if not (is_admin_name(actor) or (note.creator or '') == actor or
            has_visibility_access(note.visibility or 'family', note.creator or '', note.allowed_members_json or '[]', actor) and not is_extended_member(actor)):
        return jsonify({'ok': False, 'error': 'Only the person who wrote it can clear this memo.'}), 403
    note.is_task = True  # archived (kept for history, hidden from the board)
    note.updated_by = actor
    db.session.commit()
    return jsonify({'ok': True, 'memos': _visible_memos(actor)})


# ------------------------------------------------------- quotes & verses

# A few Common English Bible verses to start with (CEB is copyrighted; short
# quotations with attribution). Families add their own favourites on the screen.
SEED_VERSES = [
    ("I've commanded you to be brave and strong, haven't I? Don't be alarmed or terrified, because the LORD your God is with you wherever you go.", 'Joshua 1:9 (CEB)'),
    ('This is the day the LORD acted; we will rejoice and celebrate in it!', 'Psalm 118:24 (CEB)'),
    ('I can endure all these things through the power of the one who gives me strength.', 'Philippians 4:13 (CEB)'),
    ('Be kind, compassionate, and forgiving to each other, in the same way God forgave you in Christ.', 'Ephesians 4:32 (CEB)'),
    ('We love because God first loved us.', '1 John 4:19 (CEB)'),
]
# Original family encouragement lines.
SEED_QUOTES = [
    'Small steps every day add up to big things.',
    'Be the reason someone in this house smiles today.',
    'Mistakes mean you are trying. Keep going.',
    'Kind words are free, so give them away often.',
    'We do hard things together.',
    'Listen first, then speak with love.',
    'Your best is enough. Try your best today.',
    'A thank-you makes everybody\'s day brighter.',
    'Clean up, help out, and cheer each other on.',
    'Brave is doing it even when you feel a little scared.',
    'Today is a fresh start.',
    'Teamwork makes our home work.',
    'Be patient. Good things are growing.',
    'Share, take turns, and play fair.',
    'Every day is a good day to learn something new.',
    'Home is where we love each other on the hard days too.',
]


def _ensure_inspiration_seed() -> None:
    if HomeScreenInspiration.query.first():
        return
    for text, ref in SEED_VERSES:
        db.session.add(HomeScreenInspiration(kind='verse', text=text, reference=ref, created_by='Home Hub'))
    for text in SEED_QUOTES:
        db.session.add(HomeScreenInspiration(kind='quote', text=text, reference='', created_by='Home Hub'))
    db.session.commit()


def _inspiration_dict(row: HomeScreenInspiration) -> dict:
    return {'id': row.id, 'kind': row.kind, 'text': row.text, 'reference': row.reference or '', 'created_by': row.created_by or ''}


@main_bp.route('/api/home-screen/inspiration', methods=['GET'])
def api_home_screen_inspiration():
    if not _actor():
        return jsonify({'ok': False, 'error': 'Please sign in.'}), 401
    _ensure_inspiration_seed()
    rows = HomeScreenInspiration.query.filter_by(active=True).order_by(HomeScreenInspiration.id).all()
    return jsonify({'ok': True, 'items': [_inspiration_dict(r) for r in rows]})


@main_bp.route('/api/home-screen/inspiration', methods=['POST'])
def api_home_screen_inspiration_add():
    actor = _actor()
    if not actor:
        return jsonify({'ok': False, 'error': 'Please sign in.'}), 401
    if is_extended_member(actor):
        return jsonify({'ok': False, 'error': 'Only household members can add to the home screen.'}), 403
    data = request.get_json(silent=True) or {}
    kind = 'verse' if str(data.get('kind') or '').lower() == 'verse' else 'quote'
    text = sanitize_text(str(data.get('text') or '')).strip()[:600]
    reference = sanitize_text(str(data.get('reference') or '')).strip()[:120]
    if not text:
        return jsonify({'ok': False, 'error': 'Type the words first.'}), 400
    if kind == 'verse' and not reference:
        return jsonify({'ok': False, 'error': 'Add the reference, like "Joshua 1:9 (CEB)".'}), 400
    _ensure_inspiration_seed()
    row = HomeScreenInspiration(kind=kind, text=text, reference=reference, created_by=actor)
    db.session.add(row)
    db.session.commit()
    return jsonify({'ok': True, 'item': _inspiration_dict(row)})


@main_bp.route('/api/home-screen/inspiration/<int:item_id>/remove', methods=['POST'])
def api_home_screen_inspiration_remove(item_id: int):
    actor = _actor()
    row = db.session.get(HomeScreenInspiration, item_id)
    if not row:
        return jsonify({'ok': False, 'error': 'Not found.'}), 404
    if not (is_admin_name(actor) or (row.created_by or '') == actor):
        return jsonify({'ok': False, 'error': 'Only an admin or the person who added it can remove it.'}), 403
    row.active = False
    db.session.commit()
    return jsonify({'ok': True})
