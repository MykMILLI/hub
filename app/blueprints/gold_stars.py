"""Gold Star Club v1 live routes (persistent, replaces the old discipline flow).

Pages
  /gold-stars                 parent screen (redirects children to /kids-stars)
  /kids-stars                 child tablet screen (picture-led, no adult nav)
  /gold-stars/legacy          old Gold Star page, parents only, read for reference
API (all under module permission 'reward_system')
  /api/gold-stars/state ...   see routes below
"""
from flask import jsonify, redirect, render_template, request, session

from . import main_bp
from .. import db
from ..security import sanitize_text
from ..services import gold_stars as gs


def _actor() -> str:
    return sanitize_text(session.get('current_member') or '').strip()


def _scope(actor: str) -> str | None:
    """Children only ever receive their own data."""
    if gs.is_parent(actor):
        return None
    return gs.resolve_child(actor) or '__none__'


def _ok(actor: str, **extra):
    payload = {'ok': True, 'state': gs.state_payload(actor, only_child=_scope(actor))}
    payload.update(extra)
    return jsonify(payload)


def _run(fn):
    actor = _actor()
    if not actor:
        return jsonify({'ok': False, 'error': 'Please sign in.'}), 401
    try:
        extra = fn(actor) or {}
    except gs.GoldStarError as err:
        db.session.rollback()
        return jsonify({'ok': False, 'error': err.message, 'state': gs.state_payload(actor, only_child=_scope(actor))}), err.status
    return _ok(actor, **extra)


def _json() -> dict:
    return request.get_json(silent=True) or {}


# ------------------------------------------------------------------ pages

@main_bp.route('/gold-stars', methods=['GET'])
def gold_stars_live():
    actor = _actor()
    if not gs.is_parent(actor):
        return redirect('/kids-stars')
    view = sanitize_text(request.args.get('view') or 'parent-overview').strip()
    if view not in {'parent-overview', 'parent-rewards', 'parent-requests', 'parent-history', 'child-board', 'child-rewards', 'child-story'}:
        view = 'parent-overview'
    names = gs.children()
    child = gs.resolve_child(request.args.get('child')) or (names[0] if names else '')
    return render_template(
        'gold_stars_preview.html',
        preview_state=gs.state_payload(actor),
        actor_name=actor,
        initial_view=view,
        initial_child=child,
        live_mode=True,
        child_only=False,
    )


@main_bp.route('/kids-stars', methods=['GET'])
def kids_stars_live():
    actor = _actor()
    if str(request.args.get('kiosk') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
        session['kid_kiosk_mode'] = True
        session['kid_kiosk_started_by'] = actor
    if gs.is_parent(actor):
        names = gs.children()
        child = gs.resolve_child(request.args.get('child')) or (names[0] if names else '')
        state = gs.state_payload(actor)
    else:
        child = gs.resolve_child(actor)
        if not child:
            return render_template('gold_stars_preview.html', preview_state=gs.state_payload(actor, only_child='__none__'),
                                   actor_name=actor, initial_view='child-board', initial_child='',
                                   live_mode=True, child_only=True)
        state = gs.state_payload(actor, only_child=child)
    view = sanitize_text(request.args.get('view') or 'child-board').strip()
    if view not in {'child-board', 'child-rewards', 'child-story'}:
        view = 'child-board'
    return render_template(
        'gold_stars_preview.html',
        preview_state=state,
        actor_name=actor,
        initial_view=view,
        initial_child=child,
        live_mode=True,
        child_only=True,
    )


# -------------------------------------------------------------------- API

@main_bp.route('/api/gold-stars/state', methods=['GET'])
def api_gold_stars_state():
    return _run(lambda actor: None)


@main_bp.route('/api/gold-stars/preferences', methods=['POST'])
def api_gold_stars_preferences():
    return _run(lambda actor: gs.set_motion(actor, bool(_json().get('motion_enabled', True))))


@main_bp.route('/api/gold-stars/awards', methods=['POST'])
def api_gold_stars_award():
    def go(actor):
        data = _json()
        award = gs.award_star(actor, data.get('child_name'), data.get('note') or '', data.get('client_token') or '')
        return {'award_id': award.id}
    return _run(go)


@main_bp.route('/api/gold-stars/awards/collect', methods=['POST'])
def api_gold_stars_collect():
    def go(actor):
        data = _json()
        try:
            award_id = int(data.get('award_id') or 0)
        except (TypeError, ValueError):
            award_id = 0
        gs.collect_star(actor, data.get('child_name'), award_id)
    return _run(go)


@main_bp.route('/api/gold-stars/awards/<int:award_id>/void', methods=['POST'])
def api_gold_stars_void(award_id: int):
    return _run(lambda actor: gs.void_award(actor, award_id, _json().get('reason') or ''))


@main_bp.route('/api/gold-stars/catalog', methods=['POST'])
def api_gold_stars_catalog_add():
    def go(actor):
        reward = gs.add_reward(actor, _json())
        return {'reward_id': reward.id}
    return _run(go)


@main_bp.route('/api/gold-stars/catalog/<int:reward_id>', methods=['PUT'])
def api_gold_stars_catalog_edit(reward_id: int):
    return _run(lambda actor: gs.edit_reward(actor, reward_id, _json()) and None)


@main_bp.route('/api/gold-stars/catalog/<int:reward_id>/archive', methods=['POST'])
def api_gold_stars_catalog_archive(reward_id: int):
    return _run(lambda actor: gs.archive_reward(actor, reward_id))


@main_bp.route('/api/gold-stars/requests', methods=['POST'])
def api_gold_stars_request():
    def go(actor):
        data = _json()
        try:
            reward_id = int(data.get('reward_id') or 0)
        except (TypeError, ValueError):
            reward_id = 0
        req = gs.request_reward(actor, data.get('child_name'), reward_id)
        return {'request_id': req.id}
    return _run(go)


@main_bp.route('/api/gold-stars/requests/<int:request_id>/approve', methods=['POST'])
def api_gold_stars_approve(request_id: int):
    def go(actor):
        purchase = gs.approve_request(actor, request_id)
        return {'delivery_id': purchase.id}
    return _run(go)


@main_bp.route('/api/gold-stars/requests/<int:request_id>/decline', methods=['POST'])
def api_gold_stars_decline(request_id: int):
    return _run(lambda actor: gs.decline_request(actor, request_id, _json().get('reason') or ''))


@main_bp.route('/api/gold-stars/requests/<int:request_id>/cancel', methods=['POST'])
def api_gold_stars_cancel(request_id: int):
    return _run(lambda actor: gs.cancel_request(actor, request_id))


@main_bp.route('/api/gold-stars/delivery/<int:delivery_id>/deliver', methods=['POST'])
def api_gold_stars_deliver(delivery_id: int):
    return _run(lambda actor: gs.mark_delivered(actor, delivery_id))


@main_bp.route('/api/gold-stars/delivery/<int:delivery_id>/refund', methods=['POST'])
def api_gold_stars_refund(delivery_id: int):
    return _run(lambda actor: gs.refund_purchase(actor, delivery_id, _json().get('reason') or ''))


# ------------------------------------------------------------ star poster
# Shared-screen rules: kids may only stick a star a parent released; giving,
# back-dating, editing or removing stars needs a parent PIN ("parent mode").

_PIN_MAX_FAILS = 5
_PIN_LOCK_SECONDS = 300


def _parent_mode():
    from datetime import datetime
    by = session.get('poster_parent')
    until_raw = session.get('poster_parent_until')
    if not by or not until_raw:
        return None, None
    try:
        until = datetime.fromisoformat(until_raw)
    except ValueError:
        return None, None
    if until <= datetime.utcnow():
        session.pop('poster_parent', None)
        session.pop('poster_parent_until', None)
        return None, None
    return by, until


def _require_parent_mode():
    by, _ = _parent_mode()
    if not by:
        raise gs.NeedsParent('Parent mode is locked. A parent needs to enter their PIN.')
    return by


def _poster_response(actor: str, status: int = 200, **extra):
    data = request.get_json(silent=True) or {}
    week = request.args.get('week_start') or data.get('week_start')
    by, until = _parent_mode()
    payload = {'ok': status < 400, 'poster': gs.poster_payload(actor, week, parent_mode_by=by, parent_mode_until=until)}
    payload.update(extra)
    return jsonify(payload), status


def _poster_call(fn):
    actor = _actor()
    if not actor:
        return jsonify({'ok': False, 'error': 'Please sign in.'}), 401
    try:
        extra = fn(actor) or {}
    except gs.GoldStarError as err:
        db.session.rollback()
        return _poster_response(actor, err.status, error=err.message, code=getattr(err, 'code', ''))
    return _poster_response(actor, **extra)


@main_bp.route('/api/gold-stars/poster', methods=['GET'])
def api_gold_stars_poster():
    return _poster_call(lambda actor: None)


@main_bp.route('/api/gold-stars/poster/unlock', methods=['POST'])
def api_gold_stars_poster_unlock():
    from datetime import datetime, timedelta

    def go(actor):
        locked_until = session.get('poster_pin_locked_until')
        if locked_until and datetime.fromisoformat(locked_until) > datetime.utcnow():
            raise gs.GoldStarError('Too many wrong PINs. Try again in a few minutes.', 429)
        data = _json()
        try:
            parent = gs.verify_parent_pin(data.get('parent'), data.get('pin'))
        except gs.GoldStarError:
            fails = int(session.get('poster_pin_fails') or 0) + 1
            session['poster_pin_fails'] = fails
            if fails >= _PIN_MAX_FAILS:
                session['poster_pin_fails'] = 0
                session['poster_pin_locked_until'] = (datetime.utcnow() + timedelta(seconds=_PIN_LOCK_SECONDS)).isoformat()
            raise
        session['poster_pin_fails'] = 0
        session['poster_parent'] = parent
        session['poster_parent_until'] = (datetime.utcnow() + timedelta(seconds=gs.PARENT_MODE_SECONDS)).isoformat()
        return {'parent': parent}
    return _poster_call(go)


@main_bp.route('/api/gold-stars/poster/lock', methods=['POST'])
def api_gold_stars_poster_lock():
    session.pop('poster_parent', None)
    session.pop('poster_parent_until', None)
    return _poster_call(lambda actor: None)


@main_bp.route('/api/gold-stars/poster/add', methods=['POST'])
def api_gold_stars_poster_add():
    def go(actor):
        data = _json()
        if str(data.get('mode') or 'stick') == 'give':
            parent = _require_parent_mode()
            award = gs.poster_parent_add(parent, data.get('child_name'), note=data.get('note') or '',
                                         when=data.get('when'), client_token=data.get('client_token') or '')
        else:
            award = gs.poster_stick_released(actor, data.get('child_name'))
        return {'award_id': award.id}
    return _poster_call(go)


@main_bp.route('/api/gold-stars/poster/stars/<int:award_id>/edit', methods=['POST'])
def api_gold_stars_poster_edit(award_id: int):
    def go(actor):
        parent = _require_parent_mode()
        data = _json()
        gs.poster_edit_star(parent, award_id, note=data.get('note') if 'note' in data else None, when=data.get('when'))
    return _poster_call(go)


@main_bp.route('/api/gold-stars/poster/stars/<int:award_id>/remove', methods=['POST'])
def api_gold_stars_poster_remove(award_id: int):
    def go(actor):
        gs.poster_remove_star(_require_parent_mode(), award_id, _json().get('reason') or '')
    return _poster_call(go)


@main_bp.route('/api/gold-stars/poster/undo', methods=['POST'])
def api_gold_stars_poster_undo():
    def go(actor):
        gs.poster_undo(_require_parent_mode(), int(_json().get('award_id') or 0))
    return _poster_call(go)
