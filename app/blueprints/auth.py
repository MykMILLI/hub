from flask import current_app, request, session, redirect, url_for, render_template, flash, g, jsonify
from ..blueprints import main_bp
from ..config import load_config
from ..models import HubSettings, FamilyMember
import hashlib
import bleach


def _supabase_auth_enabled(cfg: dict) -> bool:
    supa = cfg.get('supabase') if isinstance(cfg, dict) else {}
    if not isinstance(supa, dict):
        return False
    return bool(supa.get('auth_enabled', False))


@main_bp.before_app_request
def reload_config_and_auth():
    if not current_app.config.get('TESTING'):
        try:
            current_app.config['HOMEHUB_CONFIG'] = load_config()
        except Exception:
            pass
    cfg = current_app.config.get('HOMEHUB_CONFIG', {})
    supabase_auth = _supabase_auth_enabled(cfg)
    
    # Load hub settings and make available to all templates
    try:
        hub_settings = HubSettings.query.first()
        if not hub_settings:
            from ..models import db
            hub_settings = HubSettings(hub_name='HomeHub')
            db.session.add(hub_settings)
            db.session.commit()
        g.hub_settings = hub_settings
    except Exception:
        g.hub_settings = None
    
    # Load family members from database
    try:
        family_members = [m.name for m in FamilyMember.query.order_by(FamilyMember.added_at).all()]
        # Merge with config.yml family_members (config takes precedence)
        config_members = cfg.get('family_members', [])
        # Use config members if they exist, otherwise use database members
        cfg['family_members'] = config_members if config_members else family_members
    except Exception:
        cfg['family_members'] = cfg.get('family_members', [])
    
    endpoint = request.endpoint or ''
    path = (request.path or '').strip()

    # Keep authenticated/member sessions alive with sliding expiration.
    if session.get('authed') or session.get('current_member'):
        session.permanent = True

    if path.startswith('/api/'):
        if not supabase_auth:
            return None

        public_api_paths = {
            '/api/auth/status',
            '/api/auth/login',
            '/api/auth/logout',
            '/api/member-login',
            '/api/member-logout',
            '/api/current-member',
            '/api/member/set-pin',
            '/api/notifications/mobile/poll',
            '/api/supabase/auth/login',
            '/api/supabase/auth/me',
            '/api/supabase/auth/logout',
            '/api/supabase/client-config',
        }
        if path in public_api_paths or path.startswith('/api/invite/'):
            return None
        if not session.get('current_member'):
            return jsonify({'ok': False, 'error': 'Authentication required'}), 401
        return None

    public_endpoints = {
        'main.login',
        'main.api_auth_status',
        'main.api_auth_login',
        'main.api_member_login',
        'main.api_member_set_pin',
        'main.api_supabase_auth_login',
        'main.api_supabase_auth_me',
        'main.api_supabase_auth_logout',
        'main.api_supabase_client_config',
        'main.invite_accept_page',
        'main.api_invite_accept',
    }

    if supabase_auth:
        if endpoint == 'main.login' and session.get('current_member'):
            return redirect(url_for('main.index'))
        if not session.get('current_member') and not endpoint.startswith('static') and endpoint not in public_endpoints:
            return redirect(url_for('main.login'))
        return None

    if cfg.get('password_hash'):
        if not session.get('authed') and not endpoint.startswith('static') and endpoint not in public_endpoints:
            return redirect(url_for('main.login'))
    else:
        if endpoint == 'main.login':
            return redirect(url_for('main.index'))


@main_bp.route('/login', methods=['GET', 'POST'])
def login():
    config = current_app.config['HOMEHUB_CONFIG']
    if _supabase_auth_enabled(config):
        if session.get('current_member'):
            return redirect(url_for('main.index'))
        return render_template('login.html', config=config, hide_user_ui=True, supabase_auth_enabled=True)

    if not config.get('password_hash'):
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        supplied = bleach.clean(request.form.get('password', ''))
        if hashlib.sha256(supplied.encode()).hexdigest() == config.get('password_hash'):
            session['authed'] = True
            session.permanent = True
            flash('Logged in successfully.', 'success')
            return redirect(url_for('main.index'))
        flash('Invalid password', 'error')
    return render_template('login.html', config=config, hide_user_ui=True, supabase_auth_enabled=False)


@main_bp.route('/logout')
def logout():
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
    flash('Logged out.', 'info')
    return redirect(url_for('main.login'))


@main_bp.route('/api/auth/status', methods=['GET'])
def api_auth_status():
    config = current_app.config.get('HOMEHUB_CONFIG', {})
    supabase_auth = _supabase_auth_enabled(config)
    requires_password = bool(config.get('password_hash')) and not supabase_auth
    supabase_authenticated = bool(session.get('supabase_user_id') or session.get('supabase_user'))
    has_member = bool((session.get('current_member') or '').strip())
    if supabase_auth:
        authed = supabase_authenticated and has_member
    else:
        base_authed = bool(session.get('authed')) if requires_password else True
        authed = base_authed and has_member

    members_payload = []
    try:
        rows = FamilyMember.query.order_by(FamilyMember.added_at.asc(), FamilyMember.id.asc()).all()
        members_payload = [
            {
                'id': row.id,
                'name': row.name,
                'has_pin': bool(row.pin),
                'is_admin': False,
            }
            for row in rows
            if row.name
        ]
    except Exception:
        members_payload = []

    admin_name = config.get('admin_name', 'Administrator')
    admin_present = any((m.get('name') or '') == admin_name for m in members_payload)
    if admin_name and not admin_present:
        members_payload.insert(0, {
            'id': None,
            'name': admin_name,
            'has_pin': False,
            'is_admin': True,
        })

    return jsonify({
        'ok': True,
        'auth_mode': 'supabase' if supabase_auth else 'password_pin',
        'supabase_auth_enabled': supabase_auth,
        'supabase_authenticated': supabase_authenticated,
        'account': session.get('supabase_account') if isinstance(session.get('supabase_account'), dict) else None,
        'requires_password': requires_password,
        'authed': authed,
        'current_member': session.get('current_member'),
        'members': members_payload,
    })


@main_bp.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    config = current_app.config.get('HOMEHUB_CONFIG', {})
    if _supabase_auth_enabled(config):
        return jsonify({'ok': False, 'error': 'Use Supabase login for this hub'}), 403

    if not config.get('password_hash'):
        session['authed'] = True
        session.permanent = True
        return jsonify({'ok': True, 'message': 'Password disabled'})

    data = request.get_json(silent=True) or {}
    supplied = str(data.get('password') or '')
    if hashlib.sha256(supplied.encode()).hexdigest() != config.get('password_hash'):
        return jsonify({'ok': False, 'error': 'Invalid password'}), 403

    session['authed'] = True
    session.permanent = True
    return jsonify({'ok': True, 'message': 'Authenticated'})


@main_bp.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
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
    return jsonify({'ok': True, 'message': 'Logged out'})
