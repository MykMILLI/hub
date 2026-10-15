from flask import current_app, redirect, render_template

from ..blueprints import main_bp
from ..security import is_http_url, sanitize_text

@main_bp.route('/home-assistant')
def home_assistant_page():
    config = current_app.config['HOMEHUB_CONFIG']
    home_assistant = config.get('home_assistant') or {}

    base_url = sanitize_text(home_assistant.get('url') or '').strip()
    auto_login_url = sanitize_text(home_assistant.get('auto_login_url') or '').strip()
    direct_url = auto_login_url or base_url

    if direct_url and is_http_url(direct_url):
        return redirect(direct_url, code=302)

    return render_template(
        'home_assistant.html',
        config=config,
        home_assistant_embed_url='',
        home_assistant_direct_url=direct_url,
        home_assistant_base_url=base_url,
        home_assistant_auto_login_enabled=bool(auto_login_url),
    )
