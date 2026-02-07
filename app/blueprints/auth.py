from flask import current_app, request, session, redirect, url_for, render_template, flash, g
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
    if cfg.get('password_hash'):
        if not session.get('authed') and not endpoint.startswith('static') and endpoint not in ('main.login',):
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
    flash('Logged out.', 'info')
    return redirect(url_for('main.login'))
