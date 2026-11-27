"""Invites for new family members + family access levels (immediate/extended)."""
from datetime import datetime

from flask import jsonify, render_template, request, session

from . import main_bp
from .. import db
from ..models import FamilyMember
from ..permissions import (
    ACCESS_TIER_LABELS,
    ACCESS_TIERS,
    APP_MODULE_NAV,
    APP_MODULES,
    DEFAULT_TIER_MODULES,
    DISABLED_APP_MODULES,
    MODULE_PLAIN_DESCRIPTIONS,
    get_tier_modules,
    is_admin_name,
    normalize_tier,
    set_tier_modules,
)
from ..security import sanitize_text
from ..services import invites as inv

MIN_PASSWORD_LENGTH = 8


def _actor() -> str:
    return sanitize_text(session.get('current_member') or '').strip()


def _admin_required():
    actor = _actor()
    if not actor or not is_admin_name(actor):
        return jsonify({'ok': False, 'error': 'Only admins can do this.'}), 403
    return None


# ------------------------------------------------------- access levels

def _module_catalog() -> list[dict]:
    labels = {e['key']: e['label'] for e in APP_MODULE_NAV}
    out = []
    for key in APP_MODULES:
        if key in DISABLED_APP_MODULES or key == 'settings':
            continue
        out.append({
            'key': key,
            'label': labels.get(key, key.replace('_', ' ').title()),
            'description': MODULE_PLAIN_DESCRIPTIONS.get(key, ''),
        })
    return out


@main_bp.route('/api/access-tiers', methods=['GET'])
def api_access_tiers():
    denied = _admin_required()
    if denied:
        return denied
    members = FamilyMember.query.order_by(FamilyMember.added_at).all()
    return jsonify({
        'ok': True,
        'modules': _module_catalog(),
        'tiers': [
            {
                'key': tier,
                'label': ACCESS_TIER_LABELS[tier],
                'modules': get_tier_modules(tier),
                'defaults': DEFAULT_TIER_MODULES[tier],
                'members': [m.name for m in members if normalize_tier(getattr(m, 'access_tier', 'immediate')) == tier and (m.role or '') not in {'admin', 'child'}],
            }
            for tier in ACCESS_TIERS
        ],
        'notes': [
            'Admins always see everything. Children only see their kid areas.',
            'Extended family can never open more than the extended list, unless you add a specific grant for that person.',
            'Calendar events marked "Household only" are hidden from extended family unless you invite them to the event.',
        ],
    })


@main_bp.route('/api/access-tiers/<tier>', methods=['PUT'])
def api_access_tiers_update(tier: str):
    denied = _admin_required()
    if denied:
        return denied
    if tier not in ACCESS_TIERS:
        return jsonify({'ok': False, 'error': 'Unknown access level.'}), 400
    data = request.get_json(silent=True) or {}
    modules = data.get('modules') if isinstance(data.get('modules'), list) else []
    saved = set_tier_modules(tier, [str(m) for m in modules], _actor())
    return jsonify({'ok': True, 'tier': tier, 'modules': saved})


@main_bp.route('/api/family-members/<int:member_id>/access-tier', methods=['PUT'])
def api_member_access_tier(member_id: int):
    denied = _admin_required()
    if denied:
        return denied
    member = db.session.get(FamilyMember, member_id)
    if not member:
        return jsonify({'ok': False, 'error': 'Family member not found'}), 404
    data = request.get_json(silent=True) or {}
    member.access_tier = normalize_tier(data.get('access_tier'))
    db.session.commit()
    return jsonify({'ok': True, 'member': {'id': member.id, 'name': member.name, 'access_tier': member.access_tier}})


@main_bp.route('/api/family-members/<int:member_id>/can-see', methods=['GET'])
def api_member_can_see(member_id: int):
    """Plain-language preview of what a member can open (used in Settings)."""
    denied = _admin_required()
    if denied:
        return denied
    member = db.session.get(FamilyMember, member_id)
    if not member:
        return jsonify({'ok': False, 'error': 'Family member not found'}), 404
    return jsonify({'ok': True, 'name': member.name, 'access_tier': normalize_tier(member.access_tier), 'areas': inv.visible_areas_for(member)})


# ------------------------------------------------------------- invites

@main_bp.route('/api/family-members/<int:member_id>/invite', methods=['POST'])
def api_member_send_invite(member_id: int):
    denied = _admin_required()
    if denied:
        return denied
    member = db.session.get(FamilyMember, member_id)
    if not member:
        return jsonify({'ok': False, 'error': 'Family member not found'}), 404
    try:
        result = inv.send_invite(member, _actor())
    except ValueError as err:
        return jsonify({'ok': False, 'error': str(err)}), 400
    return jsonify({'ok': True, **result})


@main_bp.route('/api/family-members/<int:member_id>/invite-preview', methods=['GET'])
def api_member_invite_preview(member_id: int):
    denied = _admin_required()
    if denied:
        return denied
    member = db.session.get(FamilyMember, member_id)
    if not member:
        return jsonify({'ok': False, 'error': 'Family member not found'}), 404
    base = inv.public_base_url()
    subject, text, html_body = inv.build_invite_email(member, f'{base}/invite/EXAMPLE-LINK', _actor() or 'Your family', f'{base}/')
    return jsonify({'ok': True, 'subject': subject, 'text': text, 'html': html_body})


@main_bp.route('/invite/<token>', methods=['GET'])
def invite_accept_page(token: str):
    invite, member, error = inv.find_valid_invite(token)
    areas = inv.visible_areas_for(member) if member else []
    return render_template(
        'invite_accept.html',
        token=token,
        error=error,
        member_name=member.name if member else '',
        invited_by=invite.invited_by if invite else '',
        hub_name=inv.hub_name(),
        tier_label=ACCESS_TIER_LABELS[normalize_tier(getattr(member, 'access_tier', 'immediate'))] if member else '',
        areas=areas,
        mode=inv.sign_in_mode(),
        min_password=MIN_PASSWORD_LENGTH,
    )


@main_bp.route('/api/invite/<token>/accept', methods=['POST'])
def api_invite_accept(token: str):
    invite, member, error = inv.find_valid_invite(token)
    if error or not member:
        return jsonify({'ok': False, 'error': error or 'This invite is not valid.'}), 400
    data = request.get_json(silent=True) or {}
    pin = str(data.get('pin') or '').strip()
    password = str(data.get('password') or '')
    mode = inv.sign_in_mode()

    if pin and (not pin.isdigit() or len(pin) != 4):
        return jsonify({'ok': False, 'error': 'Your PIN must be exactly 4 numbers.'}), 400
    if mode == 'pin' and not pin:
        return jsonify({'ok': False, 'error': 'Please choose a 4-digit PIN.'}), 400
    if mode == 'password':
        if len(password) < MIN_PASSWORD_LENGTH:
            return jsonify({'ok': False, 'error': f'Please choose a password with at least {MIN_PASSWORD_LENGTH} characters.'}), 400
        try:
            from .settings import _sync_member_upsert_to_supabase

            sync = _sync_member_upsert_to_supabase(
                member_name=member.name, member_email=member.email, member_role=member.role, password=password,
            )
            if not sync or not sync.get('ok'):
                return jsonify({'ok': False, 'error': 'Could not finish setting up your account. Please ask your family admin.'}), 502
        except Exception:
            return jsonify({'ok': False, 'error': 'Could not finish setting up your account. Please ask your family admin.'}), 502

    if pin:
        member.pin = pin
    invite.accepted_at = datetime.utcnow()
    member.last_hub_login_at = datetime.utcnow()
    db.session.commit()

    session['authed'] = True
    session['current_member'] = member.name
    session.permanent = True
    return jsonify({'ok': True, 'name': member.name, 'redirect': '/'})
