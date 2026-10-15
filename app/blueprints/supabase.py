import re
from datetime import datetime

from flask import current_app, jsonify, request, session
from sqlalchemy import func

from . import main_bp
from ..models import FamilyMember, db
from ..permissions import is_admin_name
from ..security import sanitize_text
from ..services.supabase_service import supabase_service


def _supabase_auth_enabled() -> bool:
    cfg = current_app.config.get('HOMEHUB_CONFIG', {})
    supa = cfg.get('supabase') if isinstance(cfg, dict) else {}
    if not isinstance(supa, dict):
        return False
    return bool(supa.get('auth_enabled', False))


def _session_has_member() -> bool:
    return bool((session.get('current_member') or '').strip())


def _current_account_payload() -> dict:
    account = session.get('supabase_account') or {}
    if not isinstance(account, dict):
        account = {}
    return {
        'member_name': session.get('current_member') or '',
        'account': {
            'id': account.get('id') or session.get('supabase_user_id') or '',
            'email': account.get('email') or session.get('supabase_email') or '',
            'display_name': account.get('display_name') or (session.get('current_member') or ''),
            'role': account.get('role') or session.get('supabase_role') or 'standard',
            'homehub_access': bool(account.get('homehub_access', False)),
            'homehub_member_name': account.get('homehub_member_name') or '',
        },
        'authenticated': bool(session.get('supabase_user_id') or session.get('supabase_user')),
    }


def _normalize_member_role(value: str | None) -> str:
    role = sanitize_text((value or 'standard').strip().lower())
    return 'admin' if role == 'admin' else 'standard'


def _find_member_by_email(email: str) -> FamilyMember | None:
    clean_email = sanitize_text((email or '').strip().lower())
    if not clean_email:
        return None
    return FamilyMember.query.filter(func.lower(FamilyMember.email) == clean_email).first()


def _find_member_by_name(name: str) -> FamilyMember | None:
    clean_name = sanitize_text((name or '').strip())
    if not clean_name:
        return None
    return FamilyMember.query.filter(func.lower(FamilyMember.name) == clean_name.lower()).first()


def _suggest_member_name_from_email(email: str) -> str:
    clean_email = sanitize_text((email or '').strip().lower())
    if not clean_email or '@' not in clean_email:
        return ''
    local = clean_email.split('@', 1)[0]
    token = re.split(r'[._\-]+', local)[0].strip()
    if not token:
        token = local.strip()
    if not token:
        return ''
    return token[:1].upper() + token[1:]


def _make_unique_member_name(base_name: str) -> str:
    seed = sanitize_text((base_name or '').strip()) or 'HomeHub User'
    candidate = seed
    suffix = 2
    while _find_member_by_name(candidate):
        candidate = f"{seed} {suffix}"
        suffix += 1
    return candidate


def _resolve_or_create_member_from_supabase(account: dict) -> tuple[FamilyMember | None, str | None]:
    email = sanitize_text(str(account.get('email') or '')).strip().lower()
    display_name = sanitize_text(str(account.get('display_name') or '')).strip()
    mapped_name = sanitize_text(str(account.get('homehub_member_name') or '')).strip()
    role = _normalize_member_role(str(account.get('role') or 'standard'))
    has_homehub_access = bool(account.get('homehub_access'))

    member = _find_member_by_email(email)
    if not member and mapped_name:
        member = _find_member_by_name(mapped_name)
    if not member and display_name:
        member = _find_member_by_name(display_name)
    if not member and email:
        member = _find_member_by_name(_suggest_member_name_from_email(email))

    if member:
        changed = False
        if email and (not member.email or member.email.strip().lower() != email):
            member.email = email
            changed = True
        if role == 'admin' and _normalize_member_role(member.role) != 'admin':
            member.role = 'admin'
            changed = True
        if changed:
            db.session.add(member)
            db.session.commit()
        return member, None

    if not has_homehub_access:
        return None, 'Supabase account is not granted HomeHub access.'

    seed_name = mapped_name or display_name or _suggest_member_name_from_email(email)
    member_name = _make_unique_member_name(seed_name)
    member = FamilyMember(
        name=member_name,
        email=email or None,
        role=role,
    )
    db.session.add(member)
    db.session.commit()
    return member, None


@main_bp.route('/api/supabase/client-config', methods=['GET'])
def api_supabase_client_config():
    data = supabase_service.client_config()
    return jsonify({'ok': True, **data})


@main_bp.route('/api/supabase/auth/login', methods=['POST'])
def api_supabase_auth_login():
    if not _supabase_auth_enabled():
        return jsonify({'ok': False, 'error': 'Supabase auth is disabled'}), 403

    data = request.get_json(silent=True) or {}
    email = str(data.get('email') or '').strip()
    password = str(data.get('password') or '')
    login = supabase_service.auth_password_login(email=email, password=password)
    if not login.get('ok'):
        return jsonify({'ok': False, 'error': login.get('error') or 'Authentication failed'}), 401

    account = supabase_service.account_from_user(login.get('user') or {}, fallback_email=email)
    member, member_error = _resolve_or_create_member_from_supabase(account)
    if not member:
        return jsonify({'ok': False, 'error': member_error or 'Unable to map Supabase user to HomeHub member'}), 403

    member_name = sanitize_text((member.name or '').strip()) or (account.get('display_name') or account.get('email') or 'HomeHub User')
    member_role = _normalize_member_role(member.role)
    account['display_name'] = member_name
    account['homehub_member_name'] = member_name
    account['homehub_access'] = True
    account['role'] = member_role

    previous_seen = getattr(member, 'last_hub_seen_at', None)
    member.last_hub_login_at = datetime.utcnow()
    db.session.add(member)
    db.session.commit()

    supabase_sync = None
    if account.get('email'):
        supabase_sync = supabase_service.provision_or_update_member_user(
            email=str(account.get('email') or ''),
            member_name=member_name,
            role=member_role,
            actor=member_name,
        )

    session['authed'] = True
    session['current_member'] = member_name
    session['supabase_account'] = account
    session['supabase_user'] = login.get('user') or {}
    session['supabase_user_id'] = account.get('id') or ''
    session['supabase_email'] = account.get('email') or ''
    session['supabase_role'] = member_role
    session['supabase_access_token'] = login.get('access_token') or ''
    session['supabase_refresh_token'] = login.get('refresh_token') or ''
    session['show_welcome_modal'] = True
    session['welcome_last_seen_at'] = previous_seen.isoformat() if previous_seen else ''
    session.permanent = True

    return jsonify({'ok': True, **_current_account_payload(), 'supabase_sync': supabase_sync})


@main_bp.route('/api/supabase/auth/me', methods=['GET'])
def api_supabase_auth_me():
    payload = _current_account_payload()
    if not payload.get('authenticated'):
        return jsonify({'ok': False, 'error': 'Not authenticated'}), 401
    return jsonify({'ok': True, **payload})


@main_bp.route('/api/supabase/auth/logout', methods=['POST'])
def api_supabase_auth_logout():
    session.pop('authed', None)
    session.pop('current_member', None)
    session.pop('supabase_account', None)
    session.pop('supabase_user', None)
    session.pop('supabase_user_id', None)
    session.pop('supabase_email', None)
    session.pop('supabase_role', None)
    session.pop('supabase_access_token', None)
    session.pop('supabase_refresh_token', None)
    session.pop('show_welcome_modal', None)
    session.pop('welcome_last_seen_at', None)
    return jsonify({'ok': True})


@main_bp.route('/api/supabase/status', methods=['GET'])
def api_supabase_status():
    if not _session_has_member():
        return jsonify({'ok': False, 'error': 'Authentication required'}), 401
    return jsonify({'ok': True, **supabase_service.status()})


@main_bp.route('/api/supabase/probe', methods=['GET'])
def api_supabase_probe():
    member_name = session.get('current_member')
    if not member_name:
        return jsonify({'ok': False, 'error': 'Authentication required'}), 401
    if not is_admin_name(member_name):
        return jsonify({'ok': False, 'error': 'Admin access required'}), 403
    return jsonify(supabase_service.probe())
