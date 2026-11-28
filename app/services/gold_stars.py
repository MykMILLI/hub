"""Gold Star Club v1 — persistent accounting.

Rules (from the approved design):
  * Parents give a star with one tap; it waits until the child collects it.
  * Collecting never changes "earned".
  * A child may have one pending reward request; nothing is deducted until a
    parent approves. Approval rechecks balance, eligibility, active status and
    the snapshotted price, and deducts exactly once (one purchase row per
    request, enforced by a unique constraint).
  * Spending never reduces "All-time earned".
  * Corrections are explicit, audited reversals (void an uncollected star,
    refund a purchase). Nothing is silently edited.

Balances are derived from rows on every read, so
    earned == available + waiting + net_spent
holds by construction.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError

from .. import db
from ..models import (
    FamilyMember,
    GoldStarAward,
    GoldStarEvent,
    GoldStarPurchase,
    GoldStarRequest,
    GoldStarReward,
    GoldStarSetting,
)
from ..security import sanitize_text

MAX_COST = 50
IMAGE_REFS = ('dessert', 'game', 'weekend', 'star', 'treat', 'toy', 'outing', 'screen', 'book', 'art')
CHARACTER_STATES = ('idle', 'noticeNewStar', 'tapReaction', 'collecting', 'celebrate', 'rewardApproved')
DEFAULT_CHILDREN = ('Avery', 'Harley')
DEFAULT_PARENTS = ('rachel', 'mykal')
MASCOT_GLB_URL = '/static/assets/gold-star/star-hop-blink.glb'


class GoldStarError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------- helpers

def _now() -> datetime:
    return datetime.utcnow()


def _iso(value: datetime | None) -> str:
    if not value:
        return ''
    return value.replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z')


def _clean(value, limit: int = 140) -> str:
    return sanitize_text(str(value or '')).strip()[:limit]


def _cfg() -> dict:
    return current_app.config.get('HOMEHUB_CONFIG', {}) or {}


def children() -> list[str]:
    """Children are family members with role 'child'. Falls back to config
    `gold_star_children`, then to Avery/Harley, so a fresh install works."""
    rows = FamilyMember.query.filter(func.lower(FamilyMember.role) == 'child').order_by(FamilyMember.added_at).all()
    names = [_clean(r.name, 128) for r in rows if _clean(r.name, 128)]
    if names:
        return names
    configured = _cfg().get('gold_star_children')
    if isinstance(configured, list) and configured:
        return [_clean(n, 128) for n in configured if _clean(n, 128)]
    return list(DEFAULT_CHILDREN)


def resolve_child(name) -> str:
    wanted = _clean(name, 128).lower()
    for child in children():
        if child.lower() == wanted:
            return child
    return ''


def is_parent(actor: str) -> bool:
    from ..permissions import is_admin_name

    clean = _clean(actor, 128)
    if not clean:
        return False
    if resolve_child(clean):
        return False
    configured = _cfg().get('gold_star_parents')
    parents = {str(p).strip().lower() for p in configured} if isinstance(configured, list) and configured else set(DEFAULT_PARENTS)
    if clean.lower() in parents:
        return True
    return is_admin_name(clean)


def can_act_for_child(actor: str, child_name: str) -> bool:
    """Parents may act for any child; a child only for themself."""
    if is_parent(actor):
        return True
    return bool(child_name) and _clean(actor, 128).lower() == child_name.lower()


def _eligible(reward: GoldStarReward) -> list[str]:
    try:
        raw = json.loads(reward.eligible_children_json or '[]')
    except Exception:
        raw = []
    return [n for n in (resolve_child(x) for x in raw if isinstance(x, str)) if n]


def _log(event_type: str, actor: str, *, child_name: str = '', stars_delta: int = 0,
         message: str = '', context: str = '', reward: dict | None = None) -> None:
    db.session.add(GoldStarEvent(
        event_type=event_type,
        actor=_clean(actor, 128),
        child_name=child_name or '',
        stars_delta=int(stars_delta or 0),
        message=_clean(message, 220),
        context=_clean(context, 220),
        reward_json=json.dumps(reward) if reward else None,
    ))


def get_setting(key: str, default: str = '') -> str:
    row = GoldStarSetting.query.filter_by(key=key).first()
    return row.value if row and row.value is not None else default


def set_setting(key: str, value: str, actor: str) -> None:
    row = GoldStarSetting.query.filter_by(key=key).first()
    if not row:
        row = GoldStarSetting(key=key)
        db.session.add(row)
    row.value = value
    row.updated_by = actor
    row.updated_at = _now()


# --------------------------------------------------------------- balances

def balances(child_name: str) -> dict:
    earned = GoldStarAward.query.filter(
        GoldStarAward.child_name == child_name, GoldStarAward.voided_at.is_(None)
    ).count()
    waiting = GoldStarAward.query.filter(
        GoldStarAward.child_name == child_name,
        GoldStarAward.voided_at.is_(None),
        GoldStarAward.collected_at.is_(None),
    ).count()
    spent_row = db.session.query(
        func.coalesce(func.sum(GoldStarPurchase.cost), 0),
        func.coalesce(func.sum(GoldStarPurchase.refunded_cost), 0),
    ).filter(GoldStarPurchase.child_name == child_name).one()
    net_spent = int(spent_row[0] or 0) - int(spent_row[1] or 0)
    available = earned - waiting - net_spent
    return {
        'earned': earned,
        'waiting': waiting,
        'net_spent': net_spent,
        'available': available,
        'invariant_ok': available >= 0 and earned == available + waiting + net_spent,
    }


# ----------------------------------------------------------- invalidation

def _invalidate_stale_requests(actor: str) -> int:
    """Pending requests whose reward changed price/eligibility/active status
    are invalidated with an explanation. Never charge a different price."""
    count = 0
    pending = GoldStarRequest.query.filter_by(status='pending').all()
    for req in pending:
        reward = db.session.get(GoldStarReward, req.reward_id)
        reason = ''
        if not reward:
            reason = 'Reward is no longer available.'
        elif not reward.active:
            reason = 'Reward was archived and is no longer requestable.'
        elif req.child_name not in _eligible(reward):
            reason = 'Child is no longer eligible for this reward.'
        elif int(reward.cost) != int(req.requested_cost):
            reason = f'Reward price changed from {req.requested_cost} to {reward.cost}.'
        if not reason:
            continue
        req.status = 'invalidated'
        req.status_message = reason
        req.resolved_by = actor
        req.resolved_at = _now()
        _log('invalidated', actor, child_name=req.child_name, message='Reward request cancelled', context=reason,
             reward={'id': req.reward_id, 'name': req.reward_name, 'cost': req.requested_cost, 'image_ref': req.image_ref})
        count += 1
    return count


# ---------------------------------------------------------------- actions

def award_star(actor: str, child_name: str, note: str = '', client_token: str = '') -> GoldStarAward:
    if not is_parent(actor):
        raise GoldStarError('Only a parent can give stars.', 403)
    child = resolve_child(child_name)
    if not child:
        raise GoldStarError('Choose a child to give the star to.')
    token = _clean(client_token, 64) or None
    if token:
        existing = GoldStarAward.query.filter_by(client_token=token).first()
        if existing:
            return existing  # idempotent double-tap / retry
    award = GoldStarAward(child_name=child, note=_clean(note), awarded_by=_clean(actor, 128), client_token=token)
    db.session.add(award)
    _log('awarded', actor, child_name=child, stars_delta=1, message=f'{actor} gave {child} a star.', context=_clean(note))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = GoldStarAward.query.filter_by(client_token=token).first()
        if existing:
            return existing
        raise
    return award


def collect_star(actor: str, child_name: str, award_id: int) -> None:
    child = resolve_child(child_name)
    if not child:
        raise GoldStarError('Unknown child.')
    if not can_act_for_child(actor, child):
        raise GoldStarError('You can only collect your own stars.', 403)
    # Conditional update = collect exactly once even with rapid taps.
    result = db.session.execute(
        update(GoldStarAward)
        .where(
            GoldStarAward.id == int(award_id),
            GoldStarAward.child_name == child,
            GoldStarAward.collected_at.is_(None),
            GoldStarAward.voided_at.is_(None),
        )
        .values(collected_at=_now(), collected_by=_clean(actor, 128))
    )
    if result.rowcount != 1:
        db.session.rollback()
        raise GoldStarError('That star was already collected.', 409)
    _log('collected', actor, child_name=child, message=f'{child} put a star on the board.')
    db.session.commit()


def void_award(actor: str, award_id: int, reason: str) -> None:
    """Audited correction for a mistaken star that has not been collected."""
    if not is_parent(actor):
        raise GoldStarError('Only a parent can correct stars.', 403)
    reason = _clean(reason, 220)
    if not reason:
        raise GoldStarError('Please give a short reason for the correction.')
    award = db.session.get(GoldStarAward, int(award_id))
    if not award or award.voided_at:
        raise GoldStarError('Star not found.', 404)
    if award.collected_at:
        raise GoldStarError('Collected stars cannot be removed; that would erase the all-time record.', 409)
    award.voided_at = _now()
    award.voided_by = _clean(actor, 128)
    award.void_reason = reason
    _log('corrected', actor, child_name=award.child_name, stars_delta=-1,
         message=f'{actor} removed a star given by mistake.', context=reason)
    db.session.commit()


def _validate_reward_fields(data: dict, partial: bool = False) -> dict:
    out = {}
    if not partial or 'name' in data:
        name = _clean(data.get('name'))
        if not name:
            raise GoldStarError('Reward name is required.')
        out['name'] = name
    if not partial or 'cost' in data:
        try:
            raw_cost = data.get('cost')
            if isinstance(raw_cost, bool) or (isinstance(raw_cost, float) and not raw_cost.is_integer()):
                raise ValueError
            cost = int(str(raw_cost).strip())
        except (TypeError, ValueError):
            raise GoldStarError(f'Cost must be a whole number from 1 to {MAX_COST}.')
        if cost < 1 or cost > MAX_COST:
            raise GoldStarError(f'Cost must be a whole number from 1 to {MAX_COST}.')
        out['cost'] = cost
    if not partial or 'image_ref' in data:
        ref = _clean(data.get('image_ref') or 'star', 64)
        out['image_ref'] = ref if ref in IMAGE_REFS else 'star'
    if 'image_url' in data:
        url = _clean(data.get('image_url'), 512)
        out['image_url'] = url if url.startswith('/uploads/') or url.startswith('/static/') else None
    if not partial or 'eligible_children' in data:
        raw = data.get('eligible_children')
        names = [resolve_child(x) for x in raw] if isinstance(raw, list) else []
        names = [n for n in dict.fromkeys(names) if n]
        if not names:
            raise GoldStarError('Choose at least one child for this reward.')
        out['eligible_children'] = names
    if not partial or 'active' in data:
        out['active'] = bool(data.get('active', True))
    return out


def add_reward(actor: str, data: dict) -> GoldStarReward:
    if not is_parent(actor):
        raise GoldStarError('Only a parent can edit rewards.', 403)
    fields = _validate_reward_fields(data)
    reward = GoldStarReward(
        name=fields['name'], cost=fields['cost'], image_ref=fields['image_ref'],
        image_url=fields.get('image_url'),
        eligible_children_json=json.dumps(fields['eligible_children']),
        active=fields['active'], created_by=_clean(actor, 128), updated_by=_clean(actor, 128),
    )
    db.session.add(reward)
    db.session.flush()
    _log('catalog', actor, message=f'Added reward "{reward.name}" ({reward.cost} stars).')
    db.session.commit()
    return reward


def edit_reward(actor: str, reward_id: int, data: dict) -> GoldStarReward:
    if not is_parent(actor):
        raise GoldStarError('Only a parent can edit rewards.', 403)
    reward = db.session.get(GoldStarReward, int(reward_id))
    if not reward:
        raise GoldStarError('Reward not found.', 404)
    fields = _validate_reward_fields(data, partial=True)
    if 'name' in fields:
        reward.name = fields['name']
    if 'cost' in fields:
        reward.cost = fields['cost']
    if 'image_ref' in fields:
        reward.image_ref = fields['image_ref']
    if 'image_url' in fields:
        reward.image_url = fields['image_url']
    if 'eligible_children' in fields:
        reward.eligible_children_json = json.dumps(fields['eligible_children'])
    if 'active' in fields:
        reward.active = fields['active']
        reward.archived_at = None if reward.active else _now()
    reward.version = int(reward.version or 1) + 1
    reward.updated_by = _clean(actor, 128)
    reward.updated_at = _now()
    _log('catalog', actor, message=f'Updated reward "{reward.name}" ({reward.cost} stars).')
    db.session.flush()
    _invalidate_stale_requests(actor)
    db.session.commit()
    return reward


def archive_reward(actor: str, reward_id: int) -> None:
    edit_reward(actor, reward_id, {'active': False})


def request_reward(actor: str, child_name: str, reward_id: int) -> GoldStarRequest:
    child = resolve_child(child_name)
    if not child:
        raise GoldStarError('Unknown child.')
    if not can_act_for_child(actor, child):
        raise GoldStarError('You can only ask for your own rewards.', 403)
    reward = db.session.get(GoldStarReward, int(reward_id))
    if not reward or not reward.active:
        raise GoldStarError('That reward is not available right now.', 409)
    if child not in _eligible(reward):
        raise GoldStarError('That reward is not on your list.', 409)
    if GoldStarRequest.query.filter_by(child_name=child, status='pending').first():
        raise GoldStarError('You already have a reward waiting for a parent.', 409)
    bal = balances(child)
    if bal['available'] < reward.cost:
        raise GoldStarError('Not enough stars yet.', 409)
    req = GoldStarRequest(
        child_name=child, reward_id=reward.id, reward_version=reward.version or 1,
        reward_name=reward.name, image_ref=reward.image_ref, image_url=reward.image_url,
        requested_cost=reward.cost, status='pending', status_message='Waiting for a parent.',
        requested_by=_clean(actor, 128),
    )
    db.session.add(req)
    _log('requested', actor, child_name=child, message=f'{child} asked for "{reward.name}".',
         context=f'{reward.cost} stars (not deducted until a parent approves).',
         reward={'id': reward.id, 'name': reward.name, 'cost': reward.cost, 'image_ref': reward.image_ref})
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # partial unique index: one pending request per child
        raise GoldStarError('You already have a reward waiting for a parent.', 409)
    return req


def cancel_request(actor: str, request_id: int) -> None:
    req = db.session.get(GoldStarRequest, int(request_id))
    if not req:
        raise GoldStarError('Request not found.', 404)
    if not can_act_for_child(actor, req.child_name):
        raise GoldStarError('Not allowed.', 403)
    result = db.session.execute(
        update(GoldStarRequest).where(GoldStarRequest.id == req.id, GoldStarRequest.status == 'pending')
        .values(status='cancelled', status_message='Changed my mind.', resolved_by=_clean(actor, 128), resolved_at=_now())
    )
    if result.rowcount != 1:
        db.session.rollback()
        raise GoldStarError('Only waiting requests can be cancelled.', 409)
    _log('cancelled', actor, child_name=req.child_name, message=f'Request for "{req.reward_name}" was cancelled.')
    db.session.commit()


def approve_request(actor: str, request_id: int) -> GoldStarPurchase:
    if not is_parent(actor):
        raise GoldStarError('Only a parent can approve rewards.', 403)
    req = db.session.get(GoldStarRequest, int(request_id))
    if not req:
        raise GoldStarError('Request not found.', 404)
    if req.status != 'pending':
        raise GoldStarError('This request was already handled.', 409)

    reward = db.session.get(GoldStarReward, req.reward_id)
    reason = ''
    if not reward or not reward.active:
        reason = 'Reward was archived before approval.'
    elif req.child_name not in _eligible(reward):
        reason = 'Child is no longer eligible for this reward.'
    elif int(reward.cost) != int(req.requested_cost):
        reason = f'Reward price changed from {req.requested_cost} to {reward.cost}.'
    if reason:
        req.status = 'invalidated'
        req.status_message = reason
        req.resolved_by = _clean(actor, 128)
        req.resolved_at = _now()
        _log('invalidated', actor, child_name=req.child_name, message='Reward request cancelled', context=reason)
        db.session.commit()
        raise GoldStarError(reason, 409)

    # Claim the request atomically: only one approver can move it out of pending.
    claimed = db.session.execute(
        update(GoldStarRequest).where(GoldStarRequest.id == req.id, GoldStarRequest.status == 'pending')
        .values(status='approved', status_message='Approved! Ready for a parent to hand over.',
                resolved_by=_clean(actor, 128), resolved_at=_now())
    )
    if claimed.rowcount != 1:
        db.session.rollback()
        raise GoldStarError('This request was already handled.', 409)

    bal = balances(req.child_name)
    if bal['available'] < req.requested_cost:
        db.session.rollback()
        raise GoldStarError('Not enough available stars at approval time.', 409)

    purchase = GoldStarPurchase(
        request_id=req.id, child_name=req.child_name, reward_id=req.reward_id,
        reward_name=req.reward_name, image_ref=req.image_ref, image_url=req.image_url,
        cost=req.requested_cost, status='ready', approved_by=_clean(actor, 128),
    )
    db.session.add(purchase)
    _log('approved', actor, child_name=req.child_name, stars_delta=-req.requested_cost,
         message=f'{actor} approved "{req.reward_name}".', context='Stars deducted once at approval.',
         reward={'id': req.reward_id, 'name': req.reward_name, 'cost': req.requested_cost, 'image_ref': req.image_ref})
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # unique(request_id): a second approval can never deduct again
        raise GoldStarError('This request was already approved.', 409)
    return purchase


def decline_request(actor: str, request_id: int, reason: str = '') -> None:
    if not is_parent(actor):
        raise GoldStarError('Only a parent can decline rewards.', 403)
    req = db.session.get(GoldStarRequest, int(request_id))
    if not req:
        raise GoldStarError('Request not found.', 404)
    msg = _clean(reason, 220) or 'Not this time.'
    result = db.session.execute(
        update(GoldStarRequest).where(GoldStarRequest.id == req.id, GoldStarRequest.status == 'pending')
        .values(status='declined', status_message=msg, resolved_by=_clean(actor, 128), resolved_at=_now())
    )
    if result.rowcount != 1:
        db.session.rollback()
        raise GoldStarError('This request was already handled.', 409)
    _log('declined', actor, child_name=req.child_name, message=f'{actor} declined "{req.reward_name}".', context=msg)
    db.session.commit()


def mark_delivered(actor: str, purchase_id: int) -> None:
    if not is_parent(actor):
        raise GoldStarError('Only a parent can mark rewards delivered.', 403)
    purchase = db.session.get(GoldStarPurchase, int(purchase_id))
    if not purchase:
        raise GoldStarError('Delivery not found.', 404)
    result = db.session.execute(
        update(GoldStarPurchase).where(GoldStarPurchase.id == purchase.id, GoldStarPurchase.status == 'ready')
        .values(status='delivered', delivered_by=_clean(actor, 128), delivered_at=_now())
    )
    if result.rowcount != 1:
        db.session.rollback()
        raise GoldStarError('Already marked delivered.', 409)
    _log('delivered', actor, child_name=purchase.child_name, message=f'{actor} gave {purchase.child_name} "{purchase.reward_name}".')
    db.session.commit()


def refund_purchase(actor: str, purchase_id: int, reason: str) -> None:
    """Explicit, audited reversal. Returns the stars to Available; earned is unchanged."""
    if not is_parent(actor):
        raise GoldStarError('Only a parent can refund.', 403)
    reason = _clean(reason, 220)
    if not reason:
        raise GoldStarError('Please give a short reason for the refund.')
    purchase = db.session.get(GoldStarPurchase, int(purchase_id))
    if not purchase:
        raise GoldStarError('Purchase not found.', 404)
    result = db.session.execute(
        update(GoldStarPurchase).where(GoldStarPurchase.id == purchase.id, GoldStarPurchase.refunded_cost == 0)
        .values(refunded_cost=purchase.cost, status='refunded', refunded_by=_clean(actor, 128),
                refunded_at=_now(), refund_reason=reason)
    )
    if result.rowcount != 1:
        db.session.rollback()
        raise GoldStarError('Already refunded.', 409)
    _log('refunded', actor, child_name=purchase.child_name, stars_delta=purchase.cost,
         message=f'{purchase.cost} stars returned for "{purchase.reward_name}".', context=reason)
    db.session.commit()


def set_motion(actor: str, enabled: bool) -> None:
    if not is_parent(actor):
        raise GoldStarError('Only a parent can change this.', 403)
    set_setting('motion_enabled', '1' if enabled else '0', actor)
    db.session.commit()


# ----------------------------------------------------------------- payload

def _request_dict(req: GoldStarRequest, purchase_by_request: dict) -> dict:
    p = purchase_by_request.get(req.id)
    return {
        'id': req.id,
        'child_name': req.child_name,
        'reward_id': req.reward_id,
        'reward_name': req.reward_name,
        'image_ref': req.image_ref or 'star',
        'image_url': req.image_url or '',
        'requested_cost': req.requested_cost,
        'status': req.status,
        'status_message': req.status_message or '',
        'requested_by': req.requested_by,
        'requested_at': _iso(req.requested_at),
        'resolved_by': req.resolved_by or '',
        'resolved_at': _iso(req.resolved_at),
        'delivery_id': p.id if p else 0,
    }


def _purchase_dict(p: GoldStarPurchase) -> dict:
    return {
        'id': p.id,
        'request_id': p.request_id,
        'child_name': p.child_name,
        'reward_name': p.reward_name,
        'reward_cost': p.cost,
        'image_ref': p.image_ref or 'star',
        'image_url': p.image_url or '',
        'status': p.status,
        'approved_by': p.approved_by,
        'approved_at': _iso(p.approved_at),
        'delivered_by': p.delivered_by or '',
        'delivered_at': _iso(p.delivered_at),
        'refund_reason': p.refund_reason or '',
    }


def _event_dict(e: GoldStarEvent) -> dict:
    reward = None
    if e.reward_json:
        try:
            reward = json.loads(e.reward_json)
        except Exception:
            reward = None
    return {
        'id': e.id,
        'event_type': e.event_type,
        'actor': e.actor,
        'child_name': e.child_name or '',
        'stars_delta': e.stars_delta or 0,
        'message': e.message or '',
        'context': e.context or '',
        'created_at': _iso(e.created_at),
        'reward': reward,
    }


def state_payload(actor: str, *, only_child: str | None = None) -> dict:
    """Same shape as the approved preview payload, so the approved screens
    render live data unchanged. `only_child` limits data for a child tablet."""
    names = children()
    if only_child:
        names = [n for n in names if n == only_child]

    rewards = GoldStarReward.query.order_by(GoldStarReward.id).all()
    requests = GoldStarRequest.query.order_by(GoldStarRequest.id.desc()).limit(200).all()
    purchases = GoldStarPurchase.query.order_by(GoldStarPurchase.id.desc()).limit(200).all()
    purchase_by_request = {p.request_id: p for p in purchases}
    events = GoldStarEvent.query.order_by(GoldStarEvent.id.desc()).limit(240).all()

    req_dicts = [_request_dict(r, purchase_by_request) for r in requests if not only_child or r.child_name == only_child]
    purchase_dicts = [_purchase_dict(p) for p in purchases if not only_child or p.child_name == only_child]
    event_dicts = [_event_dict(e) for e in events if not only_child or e.child_name in ('', only_child)]

    child_payloads = {}
    for name in names:
        bal = balances(name)
        waiting_rows = GoldStarAward.query.filter(
            GoldStarAward.child_name == name, GoldStarAward.voided_at.is_(None), GoldStarAward.collected_at.is_(None)
        ).order_by(GoldStarAward.id).all()
        active_rewards = []
        for reward in rewards:
            if not reward.active or name not in _eligible(reward):
                continue
            remaining = bal['available'] - int(reward.cost)
            active_rewards.append({
                'id': reward.id,
                'name': reward.name,
                'image_ref': reward.image_ref or 'star',
                'image_url': reward.image_url or '',
                'cost': reward.cost,
                'affordable': remaining >= 0,
                'remaining_after_request': remaining,
            })
        child_reqs = [r for r in req_dicts if r['child_name'] == name]
        latest_delivery = next((p for p in purchase_dicts if p['child_name'] == name and p['status'] in ('ready', 'delivered')), None)
        child_payloads[name] = {
            'name': name,
            'earned': bal['earned'],
            'available': bal['available'],
            'spent': bal['net_spent'],
            'waiting_count': bal['waiting'],
            'waiting_awards': [
                {'id': a.id, 'note': a.note or '', 'awarded_by': a.awarded_by, 'awarded_at': _iso(a.awarded_at)}
                for a in waiting_rows
            ],
            'board_stars': bal['available'],
            'invariant_ok': bal['invariant_ok'],
            'active_rewards': active_rewards,
            'pending_request': next((r for r in child_reqs if r['status'] == 'pending'), None),
            'latest_delivery': latest_delivery,
            'history': [e for e in event_dicts if e['child_name'] in ('', name)][:80],
        }

    catalog = [] if only_child else [
        {
            'id': r.id,
            'name': r.name,
            'image_ref': r.image_ref or 'star',
            'image_url': r.image_url or '',
            'cost': r.cost,
            'eligible_children': _eligible(r),
            'active': bool(r.active),
            'archived_at': _iso(r.archived_at),
            'updated_at': _iso(r.updated_at),
        }
        for r in rewards
    ]
    return {
        'preview_mode': False,
        'live_mode': True,
        'child_names': names,
        'is_parent': is_parent(actor),
        'children': child_payloads,
        'catalog': catalog,
        'requests': req_dicts,
        'delivery_queue': purchase_dicts,
        'history': event_dicts,
        'motion_enabled': get_setting('motion_enabled', '1') != '0',
        'sound_available': False,
        'character': {
            'asset_label': 'Gold Star mascot (animated)',
            'asset_url': MASCOT_GLB_URL,
            'temporary_asset': False,
            'supported_states': list(CHARACTER_STATES),
        },
        'overview': {
            'pending_requests': sum(1 for r in req_dicts if r['status'] == 'pending'),
            'ready_to_deliver': sum(1 for p in purchase_dicts if p['status'] == 'ready'),
        },
        'notes': {
            'cost_bound': MAX_COST,
            'image_refs': list(IMAGE_REFS),
        },
    }


# ------------------------------------------------------------- poster board
# The home-screen "star poster": a weekly sticker chart on a shared screen.
#
# Kids can only stick a star a parent already gave ("released" / waiting).
# Anything else - a new star, a star on an earlier day, editing or removing
# a star - needs a parent PIN. A correct PIN opens "parent mode" on that
# screen for a few minutes; every change is written to the audit history.

POSTER_UNDO_SECONDS = 120
PARENT_MODE_SECONDS = 300
MAX_BACKDATE_DAYS = 400


class NeedsParent(GoldStarError):
    def __init__(self, message='Ask a parent for help! A grown-up needs to give you this star.'):
        super().__init__(message, 403)
        self.code = 'needs_parent'


def _parse_when(value, *, default=None) -> datetime | None:
    if value in (None, ''):
        return default
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        raise GoldStarError('That date is not valid.')
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_week_start(value) -> datetime:
    from datetime import timedelta
    try:
        parsed = _parse_when(value)
    except GoldStarError:
        parsed = None
    if parsed:
        return parsed
    now = _now()
    start = now - timedelta(days=(now.weekday() + 1) % 7)
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def _check_star_day(when: datetime) -> datetime:
    from datetime import timedelta
    now = _now()
    if when > now + timedelta(hours=14):  # allow for time zones; never a future day
        raise GoldStarError("Stars can't be added to a day that hasn't happened yet.")
    if when < now - timedelta(days=MAX_BACKDATE_DAYS):
        raise GoldStarError('That date is too far back.')
    return when


def parents_with_pin() -> list[str]:
    names = []
    for m in FamilyMember.query.order_by(FamilyMember.added_at).all():
        if (m.pin or '').strip() and is_parent(m.name):
            names.append(m.name)
    return names


def verify_parent_pin(parent_name: str, pin: str) -> str:
    """Returns the parent's name if the PIN is right, else raises."""
    name = _clean(parent_name, 128)
    row = FamilyMember.query.filter(func.lower(FamilyMember.name) == name.lower()).first() if name else None
    if not row or not is_parent(row.name):
        raise GoldStarError('Choose a parent.', 400)
    if not (row.pin or '').strip():
        raise GoldStarError(f'{row.name} needs to set a PIN in Settings first.', 400)
    if str(pin or '').strip() != str(row.pin).strip():
        raise GoldStarError('That PIN is not right.', 403)
    return row.name


def _star_dict(a: GoldStarAward) -> dict:
    return {'id': a.id, 'at': _iso(a.collected_at), 'note': a.note or '', 'by': a.awarded_by}


def poster_payload(actor: str, week_start, *, parent_mode_by: str | None = None, parent_mode_until: datetime | None = None) -> dict:
    from datetime import timedelta
    start = _parse_week_start(week_start)
    end = start + timedelta(days=7)
    rows = []
    for name in children():
        bal = balances(name)
        stars = GoldStarAward.query.filter(
            GoldStarAward.child_name == name,
            GoldStarAward.voided_at.is_(None),
            GoldStarAward.collected_at.isnot(None),
            GoldStarAward.collected_at >= start,
            GoldStarAward.collected_at < end,
        ).order_by(GoldStarAward.collected_at).all()
        rows.append({
            'name': name,
            'all_time': bal['earned'],
            'available': bal['available'],
            'waiting': bal['waiting'],
            'week_stars': [_star_dict(a) for a in stars],
        })
    remaining = 0
    if parent_mode_by and parent_mode_until:
        remaining = max(0, int((parent_mode_until - _now()).total_seconds()))
    return {
        'week_start': _iso(start),
        'children': rows,
        'parents': parents_with_pin(),
        'parent_mode': {'by': parent_mode_by, 'seconds_left': remaining} if remaining else None,
        # kept for older screens: True only while a parent has unlocked this screen
        'can_give': bool(remaining),
        'undo_seconds': POSTER_UNDO_SECONDS,
    }


def poster_stick_released(actor: str, child_name: str) -> GoldStarAward:
    """Kid taps their button: stick the oldest star a parent already gave."""
    child = resolve_child(child_name)
    if not child:
        raise GoldStarError('Unknown child.')
    waiting = GoldStarAward.query.filter(
        GoldStarAward.child_name == child,
        GoldStarAward.voided_at.is_(None),
        GoldStarAward.collected_at.is_(None),
    ).order_by(GoldStarAward.id).first()
    if not waiting:
        raise NeedsParent()
    result = db.session.execute(
        update(GoldStarAward)
        .where(GoldStarAward.id == waiting.id, GoldStarAward.collected_at.is_(None), GoldStarAward.voided_at.is_(None))
        .values(collected_at=_now(), collected_by=_clean(actor, 128) or child)
    )
    if result.rowcount != 1:
        db.session.rollback()
        raise GoldStarError('That star was already stuck on the chart.', 409)
    _log('collected', actor, child_name=child, message=f'{child} stuck a star on the chart.')
    db.session.commit()
    return db.session.get(GoldStarAward, waiting.id)


def poster_parent_add(parent: str, child_name: str, *, note: str = '', when=None, client_token: str = '') -> GoldStarAward:
    """Parent mode: give and stick a star now, or on an earlier day."""
    child = resolve_child(child_name)
    if not child:
        raise GoldStarError('Unknown child.')
    note = _clean(note)
    stamp = _check_star_day(_parse_when(when, default=_now()))
    backdated = stamp < _now().replace(hour=0, minute=0, second=0, microsecond=0)
    token = _clean(client_token, 64) or None
    if token:
        existing = GoldStarAward.query.filter_by(client_token=token).first()
        if existing:
            return existing
    award = GoldStarAward(
        child_name=child, note=note, awarded_by=_clean(parent, 128), client_token=token,
        awarded_at=stamp, collected_at=stamp, collected_by=_clean(parent, 128),
    )
    db.session.add(award)
    day_label = stamp.strftime('%a %b %d')
    _log('awarded', parent, child_name=child, stars_delta=1,
         message=f'{parent} gave {child} a star' + (f' for {day_label} (added later).' if backdated else '.'),
         context=note)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = GoldStarAward.query.filter_by(client_token=token).first() if token else None
        if existing:
            return existing
        raise
    return award


def poster_edit_star(parent: str, award_id: int, *, note=None, when=None) -> GoldStarAward:
    award = db.session.get(GoldStarAward, int(award_id))
    if not award or award.voided_at:
        raise GoldStarError('Star not found.', 404)
    changes = []
    if note is not None:
        new_note = _clean(note)
        if new_note != (award.note or ''):
            changes.append(f'description "{award.note or ""}" -> "{new_note}"')
            award.note = new_note
    if when not in (None, ''):
        stamp = _check_star_day(_parse_when(when))
        old = award.collected_at or award.awarded_at
        if not old or old.date() != stamp.date():
            changes.append(f'day {old.strftime("%a %b %d") if old else "?"} -> {stamp.strftime("%a %b %d")}')
            award.awarded_at = stamp
            if award.collected_at:
                award.collected_at = stamp
    if not changes:
        return award
    _log('edited', parent, child_name=award.child_name, message=f'{parent} edited a star.', context='; '.join(changes))
    db.session.commit()
    return award


def poster_remove_star(parent: str, award_id: int, reason: str) -> None:
    reason = _clean(reason, 220)
    if not reason:
        raise GoldStarError('Please give a short reason for removing the star.')
    award = db.session.get(GoldStarAward, int(award_id))
    if not award or award.voided_at:
        raise GoldStarError('Star not found.', 404)
    if award.collected_at and balances(award.child_name)['available'] < 1:
        raise GoldStarError('That star was already spent on a reward. Refund the reward first on the Gold Star Club page.', 409)
    award.voided_at = _now()
    award.voided_by = _clean(parent, 128)
    award.void_reason = reason
    _log('corrected', parent, child_name=award.child_name, stars_delta=-1,
         message=f'{parent} removed a star.', context=reason)
    db.session.commit()


def poster_undo(parent: str, award_id: int) -> None:
    """Oops button right after a star was stuck (parent mode)."""
    from datetime import timedelta
    award = db.session.get(GoldStarAward, int(award_id))
    if not award or award.voided_at or not award.collected_at:
        raise GoldStarError('Star not found.', 404)
    if award.collected_at < _now() - timedelta(seconds=POSTER_UNDO_SECONDS) and award.awarded_at < _now() - timedelta(seconds=POSTER_UNDO_SECONDS):
        raise GoldStarError('Too late to undo here. Tap the star in parent mode to remove it.', 409)
    poster_remove_star(parent, award_id, 'Undone on the star chart (tapped by mistake)')
