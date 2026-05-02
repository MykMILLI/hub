import yaml
import os
import hashlib

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.yml')

def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f'config.yml not found at {CONFIG_PATH}.')
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}
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
    config['feature_toggles'].setdefault('meal_planner', True)
    config['feature_toggles'].setdefault('notifications', True)
    config['feature_toggles'].setdefault('photos', True)
    # Reminders defaults & calendar start day (supports sunday..saturday or 0-6)
    rem = config.setdefault('reminders', {})
    # Do not overwrite existing user value
    if 'calendar_start_day' not in rem or rem.get('calendar_start_day') in (None, ''):
        rem.setdefault('calendar_start_day', 'sunday')  # default Sunday to align with expense tracker
    # Admin name default
    config.setdefault('admin_name', 'Administrator')
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
    menu.setdefault('meal_planner', True)
    menu.setdefault('notifications', True)
    menu.setdefault('photos', True)
    menu.setdefault('settings', True)
    return config
