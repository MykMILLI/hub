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
from ..models import FamilyMember, Note
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
    rows = (
        Note.query.filter(Note.note_type == 'memo')
        .filter(Note.is_task.is_(False))
        .order_by(Note.timestamp.desc())
        .limit(80)
        .all()
    )
    out = []
    for row in rows:
        if has_visibility_access(row.visibility or 'family', row.creator or '', row.allowed_members_json or '[]', actor):
            out.append(_memo_dict(row))
        if len(out) >= limit:
            break
    return out


@main_bp.route('/home-screen', methods=['GET'])
def home_screen():
    actor = _actor()
    cfg = current_app.config.get('HOMEHUB_CONFIG', {}) or {}
    return render_template('home_screen.html', actor_name=actor, hub_name=_hub_name(), config=cfg)


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
