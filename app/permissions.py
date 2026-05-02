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
    'recipes',
    'expiry_tracker',
    'url_shortener',
    'media_downloader',
    'pdf_compressor',
    'qr_generator',
    'expense_tracker',
    'shared_calendar',
    'meal_planner',
    'notifications',
    'photos',
    'settings',
]


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
