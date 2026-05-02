from flask import current_app, request, session, redirect, url_for, render_template, flash, g, jsonify
from ..blueprints import main_bp
from ..config import load_config
from ..models import HubSettings, FamilyMember
import hashlib
import bleach


@main_bp.before_app_request
def reload_config_and_auth():
    try:
        current_app.config['HOMEHUB_CONFIG'] = load_config()
    except Exception:
        pass
    cfg = current_app.config.get('HOMEHUB_CONFIG', {})
    
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

    shared_guest_mode = bool(session.get('shared_guest_mode'))
    shared_calendar_token = str(session.get('shared_calendar_token') or '').strip()
    if shared_guest_mode and shared_calendar_token:
        allowed_shared_endpoints = {
            'main.calendar_shared',
            'main.api_calendar_shared_events',
            'main.api_calendar_ics_feed',
            'main.uploaded_calendar_photo',
            'main.api_auth_status',
            'main.api_auth_login',
            'main.api_member_login',
            'main.api_member_set_pin',
            'main.login',
        }
        is_static = endpoint.startswith('static')
        if not is_static and endpoint not in allowed_shared_endpoints:
            if endpoint.startswith('main.api_'):
                return jsonify({'ok': False, 'error': 'Shared calendar links only allow calendar access'}), 403
            return redirect(url_for('main.calendar_shared', token=shared_calendar_token))

    if endpoint.startswith('main.api_'):
        return None
    public_endpoints = {
        'main.login',
        'main.calendar_shared',
        'main.api_calendar_shared_events',
        'main.api_auth_status',
        'main.api_auth_login',
        'main.api_member_login',
        'main.api_member_set_pin',
    }
    if cfg.get('password_hash'):
        if not session.get('authed') and not endpoint.startswith('static') and endpoint not in public_endpoints:
            return redirect(url_for('main.login'))
    else:
        if endpoint == 'main.login':
            return redirect(url_for('main.index'))


@main_bp.route('/login', methods=['GET', 'POST'])
def login():
    config = current_app.config['HOMEHUB_CONFIG']
    if not config.get('password_hash'):
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        supplied = bleach.clean(request.form.get('password', ''))
        if hashlib.sha256(supplied.encode()).hexdigest() == config.get('password_hash'):
            session['authed'] = True
            flash('Logged in successfully.', 'success')
            return redirect(url_for('main.index'))
        flash('Invalid password', 'error')
    return render_template('login.html', config=config, hide_user_ui=True)


@main_bp.route('/logout')
def logout():
    session.pop('authed', None)
    session.pop('shared_guest_mode', None)
    session.pop('shared_calendar_token', None)
    flash('Logged out.', 'info')
    return redirect(url_for('main.login'))


@main_bp.route('/api/auth/status', methods=['GET'])
def api_auth_status():
    config = current_app.config.get('HOMEHUB_CONFIG', {})
    requires_password = bool(config.get('password_hash'))
    authed = bool(session.get('authed')) if requires_password else True

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
        'requires_password': requires_password,
        'authed': authed,
        'current_member': session.get('current_member'),
        'members': members_payload,
    })


@main_bp.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    config = current_app.config.get('HOMEHUB_CONFIG', {})
    if not config.get('password_hash'):
        session['authed'] = True
        return jsonify({'ok': True, 'message': 'Password disabled'})

    data = request.get_json(silent=True) or {}
    supplied = str(data.get('password') or '')
    if hashlib.sha256(supplied.encode()).hexdigest() != config.get('password_hash'):
        return jsonify({'ok': False, 'error': 'Invalid password'}), 403

    session['authed'] = True
    session.pop('shared_guest_mode', None)
    session.pop('shared_calendar_token', None)
    return jsonify({'ok': True, 'message': 'Authenticated'})


@main_bp.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    session.pop('authed', None)
    session.pop('current_member', None)
    session.pop('shared_guest_mode', None)
    session.pop('shared_calendar_token', None)
    return jsonify({'ok': True, 'message': 'Logged out'})
