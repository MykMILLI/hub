import yaml
import os
import hashlib

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.yml')
DEFAULT_HOME_ASSISTANT_URL = 'https://home.millitop.net'


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or '').strip().lower()
    if raw in {'1', 'true', 'yes', 'on'}:
        return True
    if raw in {'0', 'false', 'no', 'off'}:
        return False
    return bool(default)

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
    else:
        # Allow app startup with sensible defaults when config.yml is not present yet.
        config = {}
    # Hash password if present
    if 'password' in config and config['password']:
        config['password_hash'] = hashlib.sha256(config['password'].encode()).hexdigest()
        del config['password']
    # Ensure feature_toggles exists
    config.setdefault('feature_toggles', {})
    # Ensure Who is Home widget is enabled by default unless explicitly disabled in config.yml
    config['feature_toggles'].setdefault('who_is_home', True)
    # Personal status feature toggle (new)
    config['feature_toggles'].setdefault('personal_status', True)
    config['feature_toggles'].setdefault('shared_calendar', True)
    config['feature_toggles'].setdefault('daily_agenda', True)
    config['feature_toggles'].setdefault('meal_planner', True)
    config['feature_toggles'].setdefault('notifications', True)
    config['feature_toggles'].setdefault('photos', True)
    config['feature_toggles'].setdefault('home_assistant', True)
    # Reminders defaults & calendar start day (supports sunday..saturday or 0-6)
    rem = config.setdefault('reminders', {})
    # Do not overwrite existing user value
    if 'calendar_start_day' not in rem or rem.get('calendar_start_day') in (None, ''):
        rem.setdefault('calendar_start_day', 'sunday')  # default Sunday to align with expense tracker
    # Admin name default
    config.setdefault('admin_name', 'Administrator')
    config.setdefault('instance_name', 'HomeHub')
    # Family members default list
    config.setdefault('family_members', [])
    # Theme defaults
    theme = config.setdefault('theme', {})
    theme.setdefault('primary_color', '#1d4ed8')
    theme.setdefault('secondary_color', '#a0aec0')
    theme.setdefault('background_color', '#f7fafc')
    theme.setdefault('card_background_color', '#ffffff')
    theme.setdefault('text_color', '#333333')
    theme.setdefault('sidebar_background_color', '#2563eb')
    theme.setdefault('sidebar_text_color', '#ffffff')
    theme.setdefault('sidebar_link_color', 'rgba(255,255,255,0.95)')
    theme.setdefault('sidebar_link_border_color', 'rgba(255,255,255,0.18)')
    # Weather widget defaults
    weather = config.setdefault('weather', {})
    weather.setdefault('enabled', False)
    weather.setdefault('label', '')
    weather.setdefault('latitude', '')
    weather.setdefault('longitude', '')
    weather.setdefault('timezone', '')
    weather.setdefault('units', 'metric')
    weather.setdefault('view', 'compact')
    # SMTP + notifications defaults
    smtp = config.setdefault('smtp', {})
    smtp.setdefault('enabled', False)
    smtp.setdefault('host', '')
    smtp.setdefault('port', 587)
    smtp.setdefault('secure', False)
    smtp.setdefault('username', '')
    smtp.setdefault('password', '')
    smtp.setdefault('sender_email', '')
    smtp.setdefault('sender', '')

    notify = config.setdefault('notifications', {})
    notify.setdefault('email_enabled', False)
    notify.setdefault('mobile_enabled', False)
    notify.setdefault('member_emails', {})
    notify.setdefault('categories', {
        'expiry': True,
        'meal_today': True,
        'shopping_add': True,
        'shared_cloud_add': True,
        'calendar_new_event': True,
    })

    supabase = config.setdefault('supabase', {})
    supabase.setdefault('enabled', False)
    supabase.setdefault('auth_enabled', False)
    supabase.setdefault('url', '')
    supabase.setdefault('anon_key', '')
    supabase.setdefault('service_role_key', '')
    supabase.setdefault('schema', 'public')
    supabase.setdefault('chat_enabled', True)
    supabase.setdefault('chat_table', 'homehub_chat_messages')

    supabase_auth_env_raw = (os.getenv('SUPABASE_AUTH_ENABLED') or '').strip()
    supabase_auth_env_set = bool(supabase_auth_env_raw)

    # Environment overrides for secure deployment flow.
    if (os.getenv('SUPABASE_ENABLED') or '').strip():
        supabase['enabled'] = _env_bool(
            'SUPABASE_ENABLED',
            bool(supabase.get('enabled', False)),
        )
    if supabase_auth_env_set:
        supabase['auth_enabled'] = _env_bool(
            'SUPABASE_AUTH_ENABLED',
            bool(supabase.get('auth_enabled', False)),
        )
    supabase['url'] = (os.getenv('SUPABASE_URL') or supabase.get('url') or '').strip()
    supabase['anon_key'] = (os.getenv('SUPABASE_ANON_KEY') or supabase.get('anon_key') or '').strip()
    supabase['service_role_key'] = (
        os.getenv('SUPABASE_SERVICE_ROLE_KEY')
        or supabase.get('service_role_key')
        or ''
    ).strip()
    supabase['schema'] = (os.getenv('SUPABASE_SCHEMA') or supabase.get('schema') or 'public').strip() or 'public'
    supabase['chat_enabled'] = _env_bool('SUPABASE_CHAT_ENABLED', bool(supabase.get('chat_enabled', True)))
    supabase['chat_table'] = (os.getenv('SUPABASE_CHAT_TABLE') or supabase.get('chat_table') or 'homehub_chat_messages').strip() or 'homehub_chat_messages'

    # Auto-enable when credentials are present, even if enabled flag is omitted.
    if (not bool(supabase.get('enabled', False))) and supabase['url'] and supabase['anon_key']:
        supabase['enabled'] = True

    # Default to Supabase auth when Supabase is configured.
    if (not bool(supabase.get('auth_enabled', False))) and supabase['url'] and supabase['anon_key'] and (not supabase_auth_env_set):
        supabase['auth_enabled'] = True

    # Home Assistant integration defaults
    home_assistant = config.setdefault('home_assistant', {})
    home_assistant.setdefault('url', DEFAULT_HOME_ASSISTANT_URL)
    home_assistant.setdefault('auto_login_url', '')
    # Menu visibility defaults - all visible by default
    menu = config.setdefault('menu_visibility', {})
    menu.setdefault('welcome', True)
    menu.setdefault('shared_notes', True)
    menu.setdefault('shared_cloud', True)
    menu.setdefault('shopping_list', True)
    menu.setdefault('chores', True)
    menu.setdefault('recipes', True)
    menu.setdefault('expiry_tracker', True)
    menu.setdefault('url_shortener', True)
    menu.setdefault('media_downloader', True)
    menu.setdefault('pdf_compressor', True)
    menu.setdefault('qr_generator', True)
    menu.setdefault('expense_tracker', True)
    menu.setdefault('shared_calendar', True)
    menu.setdefault('daily_agenda', True)
    menu.setdefault('meal_planner', True)
    menu.setdefault('notifications', True)
    menu.setdefault('photos', True)
    menu.setdefault('home_assistant', True)
    menu.setdefault('settings', True)
    return config
