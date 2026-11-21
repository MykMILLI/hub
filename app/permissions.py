import json
from flask import current_app, session

from .models import AppPermissionScheme, FamilyMember, PermissionGroup, PermissionGroupMember, ResourcePermission
from .security import sanitize_text


ALLOWED_VISIBILITY = {'family', 'restricted', 'private'}
APP_MODULES = [
    'welcome',
    'timeline',
    'shared_notes',
    'shared_cloud',
    'shopping_list',
    'chores',
    'reward_system',
    'recipes',
    'expiry_tracker',
    'url_shortener',
    'media_downloader',
    'pdf_compressor',
    'qr_generator',
    'expense_tracker',
    'shared_calendar',
    'daily_agenda',
    'meal_planner',
    'notifications',
    'photos',
    'home_assistant',
    'cameras',
    'settings',
]

# Modules that remain in the codebase but are intentionally turned off.
# Mirrors `disabled_prefixes` in app/__init__.py and the commented-out sidebar links.
DISABLED_APP_MODULES = {
    'expense_tracker',
    'photos',
    'home_assistant',
}

# Ordered navigation spec shared by the web sidebar and native clients.
# `menu_default`/`feature_default` mirror the `.get(key, default)` calls in
# templates/base.html so native clients resolve visibility identically.
# `feature_key` is None when an entry is not gated by feature_toggles.
APP_MODULE_NAV = [
    {'id': 'welcome', 'key': 'welcome', 'label': 'Welcome', 'path': '/', 'icon': 'home',
     'menu_default': True, 'feature_key': None, 'feature_default': True},
    {'id': 'timeline', 'key': 'timeline', 'label': 'Timeline', 'path': '/timeline', 'icon': 'timeline',
     'menu_default': True, 'feature_key': None, 'feature_default': True},
    {'id': 'messages', 'key': 'notifications', 'label': 'Messages', 'path': '/messages', 'icon': 'chat',
     'menu_default': True, 'feature_key': 'notifications', 'feature_default': True},
    {'id': 'polls', 'key': 'notifications', 'label': 'Polls', 'path': '/polls', 'icon': 'poll',
     'menu_default': True, 'feature_key': 'notifications', 'feature_default': True},
    {'id': 'shared_notes', 'key': 'shared_notes', 'label': 'Projects & Spaces', 'path': '/notes', 'icon': 'projects',
     'menu_default': True, 'feature_key': 'notes', 'feature_default': False, 'skip_menu_check': True},
    {'id': 'shared_cloud', 'key': 'shared_cloud', 'label': 'Shared Cloud', 'path': '/upload', 'icon': 'cloud',
     'menu_default': True, 'feature_key': 'shared_cloud', 'feature_default': False},
    {'id': 'shopping_list', 'key': 'shopping_list', 'label': 'Shopping List', 'path': '/shopping', 'icon': 'cart',
     'menu_default': True, 'feature_key': 'shopping_list', 'feature_default': False},
    {'id': 'chores', 'key': 'chores', 'label': 'To-Do/Chores', 'path': '/chores', 'icon': 'checklist',
     'menu_default': True, 'feature_key': 'chores', 'feature_default': False},
    {'id': 'reward_system', 'key': 'reward_system', 'label': 'Gold Star Club', 'path': '/gold-stars', 'icon': 'star',
     'menu_default': True, 'feature_key': 'reward_system', 'feature_default': True},
    {'id': 'recipes', 'key': 'recipes', 'label': 'Recipe Book', 'path': '/recipes', 'icon': 'recipes',
     'menu_default': True, 'feature_key': 'recipes', 'feature_default': False, 'skip_menu_check': True},
    {'id': 'expiry_tracker', 'key': 'expiry_tracker', 'label': 'Expiry Tracker', 'path': '/expiry', 'icon': 'hourglass',
     'menu_default': True, 'feature_key': 'expiry_tracker', 'feature_default': False},
    {'id': 'url_shortener', 'key': 'url_shortener', 'label': 'URL Shortener', 'path': '/shorten', 'icon': 'link',
     'menu_default': True, 'feature_key': 'url_shortener', 'feature_default': False},
    {'id': 'media_downloader', 'key': 'media_downloader', 'label': 'Media Downloader', 'path': '/media', 'icon': 'download',
     'menu_default': True, 'feature_key': 'media_downloader', 'feature_default': False},
    {'id': 'pdf_compressor', 'key': 'pdf_compressor', 'label': 'PDF Compressor', 'path': '/pdfs', 'icon': 'pdf',
     'menu_default': True, 'feature_key': 'pdf_compressor', 'feature_default': False},
    {'id': 'qr_generator', 'key': 'qr_generator', 'label': 'QR Generator', 'path': '/qr', 'icon': 'qr',
     'menu_default': True, 'feature_key': 'qr_generator', 'feature_default': False},
    {'id': 'shared_calendar', 'key': 'shared_calendar', 'label': 'Family Calendar', 'path': '/calendar', 'icon': 'calendar',
     'menu_default': True, 'feature_key': 'shared_calendar', 'feature_default': True},
    {'id': 'daily_agenda', 'key': 'daily_agenda', 'label': 'Daily Agenda', 'path': '/daily-agenda', 'icon': 'route',
     'menu_default': True, 'feature_key': 'daily_agenda', 'feature_default': True},
    {'id': 'meal_planner', 'key': 'meal_planner', 'label': 'Meal Planner', 'path': '/meal-planner', 'icon': 'meal',
     'menu_default': True, 'feature_key': 'meal_planner', 'feature_default': True},
    {'id': 'notifications', 'key': 'notifications', 'label': 'Notifications', 'path': '/notifications', 'icon': 'bell',
     'menu_default': True, 'feature_key': 'notifications', 'feature_default': True},
    {'id': 'cameras', 'key': 'cameras', 'label': 'Cameras', 'path': '/cameras', 'icon': 'camera',
     'menu_default': True, 'feature_key': 'cameras', 'feature_default': True},
    {'id': 'settings', 'key': 'settings', 'label': 'Settings', 'path': '/settings', 'icon': 'settings',
     'menu_default': True, 'feature_key': None, 'feature_default': True},
]


def _config_mapping(config, name: str) -> dict:
    section = config.get(name) if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def is_app_module_enabled(config, entry: dict) -> bool:
    """Resolve a nav entry against the hard-disable set and config toggles."""
    if entry['key'] in DISABLED_APP_MODULES:
        return False
    if not entry.get('skip_menu_check'):
        menu = _config_mapping(config, 'menu_visibility')
        if not menu.get(entry['key'], entry['menu_default']):
            return False
    feature_key = entry.get('feature_key')
    if feature_key:
        features = _config_mapping(config, 'feature_toggles')
        if not features.get(feature_key, entry['feature_default']):
            return False
    return True


def get_visible_app_modules(config=None, member: str | None = None) -> list[dict]:
    """Modules the given member can actually open, in web sidebar order."""
    if config is None:
        config = current_app.config.get('HOMEHUB_CONFIG') or {}
    visible = []
    for entry in APP_MODULE_NAV:
        if not is_app_module_enabled(config, entry):
            continue
        if not can_access_app_module(entry['key'], member):
            continue
        visible.append({
            'id': entry['id'],
            'key': entry['key'],
            'label': entry['label'],
            'path': entry['path'],
            'icon': entry['icon'],
        })
    return visible


def parse_allowed_members(raw_value) -> list[str]:
    if not raw_value:
        return []
    if isinstance(raw_value, list):
        values = raw_value
    else:
        text = str(raw_value or '').strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            values = parsed if isinstance(parsed, list) else text.split(',')
        except Exception:
            values = text.split(',')
    cleaned = [sanitize_text(str(v)).strip() for v in values]
    return sorted({v for v in cleaned if v})


def sanitize_visibility(value: str) -> str:
    clean = sanitize_text(value or '').strip().lower()
    return clean if clean in ALLOWED_VISIBILITY else 'family'


def is_admin_name(name: str | None) -> bool:
    member_name = sanitize_text(name or '').strip()
    if not member_name:
        return False

    current_member = sanitize_text(session.get('current_member') or '').strip()
    supabase_role = sanitize_text(session.get('supabase_role') or '').strip().lower()
    if supabase_role == 'admin' and current_member and member_name == current_member:
        return True

    admin_name = sanitize_text(current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator'))
    if member_name in {admin_name, 'Administrator', 'admin'}:
        return True
    row = FamilyMember.query.filter_by(name=member_name).first()
    role = sanitize_text(getattr(row, 'role', 'standard') or 'standard').lower()
    return role == 'admin'


def current_member_name() -> str:
    member = sanitize_text(session.get('current_member') or '').strip()
    if member:
        return member
    return ''


def has_visibility_access(visibility: str, owner: str, allowed_members_json: str, member: str | None = None) -> bool:
    member_name = sanitize_text(member or current_member_name()).strip()
    if is_admin_name(member_name):
        return True
    if sanitize_text(owner or '').strip() == member_name:
        return True
    clean_visibility = sanitize_visibility(visibility)
    if clean_visibility == 'family':
        return True
    if clean_visibility == 'private':
        return False
    allowed = parse_allowed_members(allowed_members_json)
    return member_name in allowed


def can_access_app_module(module_key: str, member: str | None = None) -> bool:
    key = sanitize_text(module_key or '').strip()
    if not key:
        return True
    member_name = sanitize_text(member or current_member_name()).strip()
    if not member_name:
        return key == 'welcome'
    if is_admin_name(member_name):
        return True

    if has_resource_permission('app_module', resource_key=key, member=member_name, action='read'):
        return True

    row = AppPermissionScheme.query.filter_by(resource_type='app_module', resource_key=key).first()
    if not row:
        if key == 'reward_system':
            return True
        return False
    return has_visibility_access(row.visibility, row.updated_by, row.allowed_members_json, member_name)


def get_app_module_access_map(member: str | None = None) -> dict:
    member_name = sanitize_text(member or current_member_name()).strip()
    result = {}
    for module_key in APP_MODULES:
        result[module_key] = can_access_app_module(module_key, member_name)
    return result


def get_member_groups(member: str | None = None) -> set[str]:
    member_name = sanitize_text(member or current_member_name()).strip()
    if not member_name:
        return set()
    rows = PermissionGroupMember.query.filter_by(member_name=member_name).all()
    group_ids = [r.group_id for r in rows]
    if not group_ids:
        return set()
    groups = PermissionGroup.query.filter(PermissionGroup.id.in_(group_ids)).all()
    return {sanitize_text(g.name or '').strip() for g in groups if sanitize_text(g.name or '').strip()}


def _resource_permission_rows(resource_type: str, resource_key: str | None = None, resource_id: int | None = None) -> list[ResourcePermission]:
    query = ResourcePermission.query.filter_by(resource_type=sanitize_text(resource_type or '').strip())
    if resource_key is not None:
        query = query.filter_by(resource_key=sanitize_text(resource_key or '').strip())
    if resource_id is not None:
        query = query.filter_by(resource_id=int(resource_id))
    return query.all()


def has_resource_permission(resource_type: str, resource_key: str | None = None, resource_id: int | None = None, member: str | None = None, action: str = 'read') -> bool:
    member_name = sanitize_text(member or current_member_name()).strip()
    if is_admin_name(member_name):
        return True
    clean_action = sanitize_text(action or 'read').strip().lower()

    rows = _resource_permission_rows(resource_type=resource_type, resource_key=resource_key, resource_id=resource_id)
    if not rows:
        return False

    groups = get_member_groups(member_name)
    relevant = []
    for row in rows:
        subject_type = sanitize_text(row.subject_type or '').strip().lower()
        subject_value = sanitize_text(row.subject_value or '').strip()
        if subject_type == 'member' and subject_value == member_name:
            relevant.append(row)
        elif subject_type == 'group' and subject_value in groups:
            relevant.append(row)

    if not relevant:
        return False

    for row in relevant:
        can_read = bool(row.can_read or row.can_write or row.can_admin)
        can_write = bool(row.can_write or row.can_admin)
        can_admin = bool(row.can_admin)
        if clean_action == 'admin' and can_admin:
            return True
        if clean_action == 'write' and can_write:
            return True
        if clean_action == 'read' and can_read:
            return True
    return False
