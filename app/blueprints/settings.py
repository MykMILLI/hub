from flask import render_template, request, redirect, url_for, jsonify, current_app, flash, session
from ..models import (
    db,
    HubSettings,
    FamilyMember,
    ActivityLog,
    AppPermissionScheme,
    WorkspaceProject,
    WorkspaceSpace,
    PermissionGroup,
    PermissionGroupMember,
    ResourcePermission,
)
from ..blueprints import main_bp
from ..security import sanitize_text
from ..permissions import APP_MODULES, parse_allowed_members, sanitize_visibility
from ..services.notification_service import send_test_email
from datetime import datetime
import yaml
import os
import json


def _normalize_role(value: str | None) -> str:
    role = sanitize_text((value or 'standard').strip().lower())
    return 'admin' if role == 'admin' else 'standard'


def _is_config_admin_name(name: str | None) -> bool:
    admin_name = sanitize_text(current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator'))
    return sanitize_text(name or '') == admin_name


def _is_admin_name(name: str | None) -> bool:
    member_name = sanitize_text(name or '')
    if not member_name:
        return False
    if _is_config_admin_name(member_name):
        return True
    member = FamilyMember.query.filter_by(name=member_name).first()
    return bool(member and _normalize_role(member.role) == 'admin')


def _current_member_is_admin() -> bool:
    current_member = session.get('current_member')
    if not current_member:
        current_member = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    return _is_admin_name(current_member)


def _upsert_app_module_permission(module_key: str, visibility: str, allowed_members: list[str], updated_by: str):
    row = AppPermissionScheme.query.filter_by(resource_type='app_module', resource_key=module_key).first()
    if not row:
        row = AppPermissionScheme(
            resource_type='app_module',
            resource_key=module_key,
            resource_id=None,
            updated_by=updated_by,
        )
        db.session.add(row)
    row.visibility = sanitize_visibility(visibility)
    row.allowed_members_json = json.dumps(allowed_members)
    row.updated_by = updated_by


def _permission_payload(visibility: str, allowed_members_json: str) -> dict:
    return {
        'visibility': sanitize_visibility(visibility),
        'allowed_members': parse_allowed_members(allowed_members_json),
    }


APP_MODULE_LABELS = {
    'welcome': 'Welcome',
    'timeline': 'Timeline',
    'shared_notes': 'Projects & Spaces',
    'shared_cloud': 'Shared Cloud',
    'shopping_list': 'Shopping List',
    'chores': 'To-Do/Chores',
    'recipes': 'Recipe Book',
    'expiry_tracker': 'Expiry Tracker',
    'url_shortener': 'URL Shortener',
    'media_downloader': 'Media Downloader',
    'pdf_compressor': 'PDF Compressor',
    'qr_generator': 'QR Generator',
    'expense_tracker': 'Expense & Money Tracker',
    'shared_calendar': 'Family Calendar',
    'meal_planner': 'Meal Planner',
    'notifications': 'Messages, Polls & Notifications',
    'photos': 'Family Photos',
    'settings': 'Settings',
}


@main_bp.route('/settings', methods=['GET', 'POST'])
def settings():
    hub_settings = HubSettings.query.first()
    if not hub_settings:
        hub_settings = HubSettings(hub_name='HomeHub')
        db.session.add(hub_settings)
        db.session.commit()
    
    if request.method == 'POST':
        if not _current_member_is_admin():
            flash('Only admins can update hub settings.', 'error')
            return redirect(url_for('main.settings'))
        hub_name = sanitize_text(request.form.get('hub_name', 'HomeHub')).strip()
        if hub_name:
            hub_settings.hub_name = hub_name
            db.session.commit()
        return redirect(url_for('main.settings'))
    
    config = current_app.config['HOMEHUB_CONFIG']
    family_members = FamilyMember.query.order_by(FamilyMember.added_at).all()
    return render_template(
        'settings.html',
        config=config,
        hub_settings=hub_settings,
        family_members=family_members,
        current_is_admin=_current_member_is_admin(),
    )


@main_bp.route('/api/settings', methods=['GET'])
def api_get_settings():
    """Get all hub settings"""
    hub_settings = HubSettings.query.first()
    if not hub_settings:
        hub_settings = HubSettings(hub_name='HomeHub')
        db.session.add(hub_settings)
        db.session.commit()
    return jsonify({
        "hub_name": hub_settings.hub_name,
        "updated_at": hub_settings.updated_at.isoformat()
    })


@main_bp.route('/api/settings', methods=['POST'])
def api_update_settings():
    """Update hub settings"""
    if not _current_member_is_admin():
        return jsonify({"ok": False, "error": "Only admins can update settings"}), 403

    hub_settings = HubSettings.query.first()
    if not hub_settings:
        hub_settings = HubSettings()
        db.session.add(hub_settings)
    
    try:
        data = request.get_json(force=True) or {}
        if 'hub_name' in data:
            hub_settings.hub_name = sanitize_text(str(data['hub_name']).strip()) or 'HomeHub'
        
        db.session.commit()
        return jsonify({
            "ok": True,
            "hub_name": hub_settings.hub_name,
            "updated_at": hub_settings.updated_at.isoformat()
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# Family member management routes
@main_bp.route('/api/family-members', methods=['GET'])
def api_get_family_members():
    """Get all family members"""
    members = FamilyMember.query.order_by(FamilyMember.added_at).all()
    current_admin = _current_member_is_admin()
    return jsonify({
        "ok": True,
        "members": [
            {
                "id": m.id,
                "name": m.name,
                "pin": bool(m.pin),
                "email": m.email,
                "avatar": m.avatar,
                "role": _normalize_role(m.role),
                "is_admin": _normalize_role(m.role) == 'admin' or _is_config_admin_name(m.name),
            }
            for m in members
        ],
        "current_is_admin": current_admin,
    })


@main_bp.route('/api/family-members', methods=['POST'])
def api_add_family_member():
    """Add a new family member"""
    try:
        if not _current_member_is_admin():
            return jsonify({"ok": False, "error": "Only admins can add members"}), 403

        data = request.get_json(force=True) or {}
        name = sanitize_text(str(data.get('name', '')).strip())
        role = _normalize_role(str(data.get('role', 'standard')))
        email = sanitize_text(str(data.get('email', '')).strip())
        
        if not name:
            return jsonify({"ok": False, "error": "Family member name cannot be empty"}), 400
        
        # Check if member already exists
        existing = FamilyMember.query.filter_by(name=name).first()
        if existing:
            return jsonify({"ok": False, "error": "Family member already exists"}), 400
        
        if email and ('@' not in email or '.' not in email.split('@')[-1]):
            return jsonify({"ok": False, "error": "Invalid email address"}), 400

        member = FamilyMember(name=name, role=role, email=(email or None))
        db.session.add(member)
        db.session.commit()
        
        return jsonify({
            "ok": True,
            "member": {
                "id": member.id,
                "name": member.name,
                "email": member.email,
                "role": _normalize_role(member.role),
                "is_admin": _normalize_role(member.role) == 'admin' or _is_config_admin_name(member.name),
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/family-members/<int:member_id>', methods=['PUT'])
def api_update_family_member(member_id):
    """Update a family member's name and/or PIN"""
    try:
        if not _current_member_is_admin():
            return jsonify({"ok": False, "error": "Only admins can update members"}), 403

        member = FamilyMember.query.get(member_id)
        if not member:
            return jsonify({"ok": False, "error": "Family member not found"}), 404
        
        data = request.get_json(force=True) or {}
        name = sanitize_text(str(data.get('name', '')).strip()) if 'name' in data else member.name
        pin = str(data.get('pin', '')).strip() if 'pin' in data else member.pin
        role = _normalize_role(str(data.get('role', member.role or 'standard')))
        email = sanitize_text(str(data.get('email', '')).strip()) if 'email' in data else (member.email or '')
        
        if not name:
            return jsonify({"ok": False, "error": "Family member name cannot be empty"}), 400
        
        # Check if new name already exists (but not on the same member)
        if name != member.name:
            existing = FamilyMember.query.filter_by(name=name).first()
            if existing and existing.id != member_id:
                return jsonify({"ok": False, "error": "Family member name already exists"}), 400
        
        # Validate PIN if provided (must be 4 digits or empty)
        if pin and (not pin.isdigit() or len(pin) != 4):
            return jsonify({"ok": False, "error": "PIN must be exactly 4 digits"}), 400

        if email and ('@' not in email or '.' not in email.split('@')[-1]):
            return jsonify({"ok": False, "error": "Invalid email address"}), 400
        
        member.name = name
        member.pin = pin if pin else None
        member.email = email or None
        member.role = role
        db.session.commit()
        
        return jsonify({
            "ok": True,
            "member": {
                "id": member.id,
                "name": member.name,
                "pin": member.pin or "",
                "email": member.email,
                "role": _normalize_role(member.role),
                "is_admin": _normalize_role(member.role) == 'admin' or _is_config_admin_name(member.name),
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/family-members/<int:member_id>', methods=['DELETE'])
def api_delete_family_member(member_id):
    """Delete a family member"""
    try:
        if not _current_member_is_admin():
            return jsonify({"ok": False, "error": "Only admins can delete members"}), 403

        member = FamilyMember.query.get(member_id)
        if not member:
            return jsonify({"ok": False, "error": "Family member not found"}), 404

        if _is_config_admin_name(member.name):
            return jsonify({"ok": False, "error": "Cannot delete configured admin"}), 400

        admin_count = FamilyMember.query.filter_by(role='admin').count()
        if _normalize_role(member.role) == 'admin' and admin_count <= 1:
            return jsonify({"ok": False, "error": "Cannot delete the last admin member"}), 400
        
        member_name = member.name
        db.session.delete(member)
        db.session.commit()
        
        return jsonify({
            "ok": True,
            "message": f"Family member '{member_name}' deleted successfully"
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# Menu Visibility Management
@main_bp.route('/api/menu-visibility', methods=['GET'])
def api_get_menu_visibility():
    """Get current menu visibility settings"""
    config = current_app.config['HOMEHUB_CONFIG']
    menu_visibility = config.get('menu_visibility', {})
    return jsonify({
        "ok": True,
        "menu_visibility": menu_visibility
    })


@main_bp.route('/api/menu-visibility/<menu_item>', methods=['PUT'])
def api_toggle_menu_item(menu_item):
    """Toggle visibility of a menu item"""
    try:
        if not _current_member_is_admin():
            return jsonify({"ok": False, "error": "Only admins can change menu visibility"}), 403

        # Valid menu items that can be toggled
        valid_items = [
            'welcome', 'timeline', 'shared_notes', 'shared_cloud', 'shopping_list',
            'chores', 'recipes', 'expiry_tracker', 'url_shortener',
            'media_downloader', 'pdf_compressor', 'qr_generator',
            'expense_tracker', 'shared_calendar', 'meal_planner', 'notifications', 'photos', 'settings'
        ]
        
        if menu_item not in valid_items:
            return jsonify({"ok": False, "error": "Invalid menu item"}), 400
        
        data = request.get_json(force=True) or {}
        visible = data.get('visible', True)
        
        # Load current config
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        config_path = os.path.join(base_dir, 'config.yml')
        
        with open(config_path, 'r', encoding='utf-8') as f:
            file_config = yaml.safe_load(f) or {}
        
        # Update menu visibility
        menu_visibility = file_config.setdefault('menu_visibility', {})
        menu_visibility[menu_item] = visible
        
        # Write back to config file
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(file_config, f, default_flow_style=False, allow_unicode=True)
        
        # Reload config in the app
        from ..config import load_config
        updated_config = load_config()
        current_app.config['HOMEHUB_CONFIG'] = updated_config
        
        return jsonify({
            "ok": True,
            "message": f"Menu item '{menu_item}' is now {'visible' if visible else 'hidden'}",
            "menu_visibility": menu_visibility
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/permission-schemes', methods=['GET'])
def api_get_permission_schemes():
    member_rows = FamilyMember.query.order_by(FamilyMember.name.asc()).all()
    members = [sanitize_text((m.name or '')).strip() for m in member_rows if sanitize_text((m.name or '')).strip()]
    admin_name = sanitize_text(current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')).strip()
    if admin_name and admin_name not in members:
        members.append(admin_name)
    members = sorted(set(members), key=lambda x: x.lower())
    member_details = [{
        'id': row.id,
        'name': sanitize_text((row.name or '')).strip(),
        'role': _normalize_role(row.role),
        'is_admin': _normalize_role(row.role) == 'admin' or _is_config_admin_name(row.name),
    } for row in member_rows if sanitize_text((row.name or '')).strip()]
    if admin_name and not any((d.get('name') or '') == admin_name for d in member_details):
        member_details.append({
            'id': None,
            'name': admin_name,
            'role': 'admin',
            'is_admin': True,
        })
    member_details = sorted(member_details, key=lambda d: sanitize_text(d.get('name') or '').lower())
    groups = PermissionGroup.query.order_by(PermissionGroup.name.asc()).all()
    group_members = PermissionGroupMember.query.all()
    members_by_group = {}
    for gm in group_members:
        members_by_group.setdefault(gm.group_id, []).append(gm.member_name)

    grants = ResourcePermission.query.order_by(ResourcePermission.resource_type.asc(), ResourcePermission.updated_at.desc()).all()
    projects = WorkspaceProject.query.order_by(WorkspaceProject.updated_at.desc()).all()
    spaces = WorkspaceSpace.query.order_by(WorkspaceSpace.updated_at.desc()).all()

    return jsonify({
        'ok': True,
        'members': members,
        'member_details': member_details,
        'groups': [{
            'id': g.id,
            'name': g.name,
            'description': g.description or '',
            'members': sorted(set(members_by_group.get(g.id, []))),
        } for g in groups],
        'resources': {
            'app_modules': [{
                'resource_type': 'app_module',
                'resource_key': key,
                'label': APP_MODULE_LABELS.get(key, key),
            } for key in APP_MODULES],
            'projects': [{
                'resource_type': 'project',
                'resource_id': p.id,
                'resource_key': p.project_key,
                'label': f"{p.name} ({p.project_key})",
                'archived': bool(p.archived),
            } for p in projects],
            'spaces': [{
                'resource_type': 'space',
                'resource_id': s.id,
                'resource_key': s.space_key,
                'label': f"{s.name} ({s.space_key})",
                'archived': bool(s.archived),
            } for s in spaces],
        },
        'grants': [{
            'id': g.id,
            'resource_type': g.resource_type,
            'resource_key': g.resource_key,
            'resource_id': g.resource_id,
            'subject_type': g.subject_type,
            'subject_value': g.subject_value,
            'can_read': bool(g.can_read),
            'can_write': bool(g.can_write),
            'can_admin': bool(g.can_admin),
        } for g in grants],
        'projects': [{
            'id': p.id,
            'name': p.name,
            'key': p.project_key,
            'archived': bool(p.archived),
            **_permission_payload(p.visibility or 'family', p.allowed_members_json),
        } for p in projects],
        'spaces': [{
            'id': s.id,
            'name': s.name,
            'key': s.space_key,
            'archived': bool(s.archived),
            **_permission_payload(s.visibility or 'family', s.allowed_members_json),
        } for s in spaces],
    })


@main_bp.route('/api/permission-schemes/app/<module_key>', methods=['PUT'])
def api_update_permission_scheme_app(module_key):
    if not _current_member_is_admin():
        return jsonify({'ok': False, 'error': 'Only admins can manage permission schemes'}), 403
    clean_module = sanitize_text(module_key or '').strip()
    if clean_module not in APP_MODULES:
        return jsonify({'ok': False, 'error': 'Invalid module key'}), 400

    data = request.get_json(force=True) or {}
    visibility = sanitize_visibility(data.get('visibility') or 'family')
    allowed_members = parse_allowed_members(data.get('allowed_members') or [])
    current_member = sanitize_text(session.get('current_member') or current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator'))

    _upsert_app_module_permission(clean_module, visibility, allowed_members, current_member)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/permission-groups', methods=['POST'])
def api_create_permission_group():
    if not _current_member_is_admin():
        return jsonify({'ok': False, 'error': 'Only admins can manage permission groups'}), 403
    data = request.get_json(force=True) or {}
    name = sanitize_text(data.get('name') or '').strip()
    description = sanitize_text(data.get('description') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Group name is required'}), 400
    existing = PermissionGroup.query.filter_by(name=name).first()
    if existing:
        return jsonify({'ok': False, 'error': 'Group already exists'}), 400
    creator = sanitize_text(session.get('current_member') or current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator'))
    row = PermissionGroup(name=name, description=description, created_by=creator)
    db.session.add(row)
    db.session.commit()
    return jsonify({'ok': True, 'id': row.id})


@main_bp.route('/api/permission-groups/<int:group_id>', methods=['PUT'])
def api_update_permission_group(group_id):
    if not _current_member_is_admin():
        return jsonify({'ok': False, 'error': 'Only admins can manage permission groups'}), 403
    row = PermissionGroup.query.get_or_404(group_id)
    data = request.get_json(force=True) or {}
    name = sanitize_text(data.get('name') or row.name).strip()
    description = sanitize_text(data.get('description') or row.description or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Group name is required'}), 400
    existing = PermissionGroup.query.filter_by(name=name).first()
    if existing and existing.id != row.id:
        return jsonify({'ok': False, 'error': 'Group name already exists'}), 400
    row.name = name
    row.description = description
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/permission-groups/<int:group_id>', methods=['DELETE'])
def api_delete_permission_group(group_id):
    if not _current_member_is_admin():
        return jsonify({'ok': False, 'error': 'Only admins can manage permission groups'}), 403
    row = PermissionGroup.query.get_or_404(group_id)
    PermissionGroupMember.query.filter_by(group_id=row.id).delete()
    ResourcePermission.query.filter_by(subject_type='group', subject_value=row.name).delete()
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/permission-groups/<int:group_id>/members', methods=['PUT'])
def api_set_permission_group_members(group_id):
    if not _current_member_is_admin():
        return jsonify({'ok': False, 'error': 'Only admins can manage permission groups'}), 403
    row = PermissionGroup.query.get_or_404(group_id)
    data = request.get_json(force=True) or {}
    members = parse_allowed_members(data.get('members') or [])
    actor = sanitize_text(session.get('current_member') or current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator'))
    PermissionGroupMember.query.filter_by(group_id=row.id).delete()
    for member_name in members:
        db.session.add(PermissionGroupMember(group_id=row.id, member_name=member_name, added_by=actor))
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/permission-grants', methods=['PUT'])
def api_upsert_permission_grant():
    if not _current_member_is_admin():
        return jsonify({'ok': False, 'error': 'Only admins can manage permission grants'}), 403
    data = request.get_json(force=True) or {}
    resource_type = sanitize_text(data.get('resource_type') or '').strip()
    resource_key = sanitize_text(data.get('resource_key') or '').strip() or None
    resource_id_raw = data.get('resource_id')
    resource_id = None
    if resource_id_raw not in (None, '', 'null'):
        try:
            resource_id = int(resource_id_raw)
        except Exception:
            return jsonify({'ok': False, 'error': 'Invalid resource_id'}), 400
    subject_type = sanitize_text(data.get('subject_type') or '').strip().lower()
    subject_value = sanitize_text(data.get('subject_value') or '').strip()
    can_read = bool(data.get('can_read'))
    can_write = bool(data.get('can_write'))
    can_admin = bool(data.get('can_admin'))

    if resource_type not in {'app_module', 'project', 'space'}:
        return jsonify({'ok': False, 'error': 'Invalid resource_type'}), 400
    if subject_type not in {'member', 'group'}:
        return jsonify({'ok': False, 'error': 'Invalid subject_type'}), 400
    if not subject_value:
        return jsonify({'ok': False, 'error': 'subject_value is required'}), 400

    actor = sanitize_text(session.get('current_member') or current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator'))
    row = ResourcePermission.query.filter_by(
        resource_type=resource_type,
        resource_key=resource_key,
        resource_id=resource_id,
        subject_type=subject_type,
        subject_value=subject_value,
    ).first()
    if not row:
        row = ResourcePermission(
            resource_type=resource_type,
            resource_key=resource_key,
            resource_id=resource_id,
            subject_type=subject_type,
            subject_value=subject_value,
            updated_by=actor,
        )
        db.session.add(row)

    row.can_read = can_read or can_write or can_admin
    row.can_write = can_write or can_admin
    row.can_admin = can_admin
    row.updated_by = actor
    db.session.commit()
    return jsonify({'ok': True, 'id': row.id})


@main_bp.route('/api/permission-grants/<int:grant_id>', methods=['DELETE'])
def api_delete_permission_grant(grant_id):
    if not _current_member_is_admin():
        return jsonify({'ok': False, 'error': 'Only admins can manage permission grants'}), 403
    row = ResourcePermission.query.get_or_404(grant_id)
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/permission-schemes/project/<int:project_id>', methods=['PUT'])
def api_update_permission_scheme_project(project_id):
    if not _current_member_is_admin():
        return jsonify({'ok': False, 'error': 'Only admins can manage permission schemes'}), 403
    project = WorkspaceProject.query.get_or_404(project_id)
    data = request.get_json(force=True) or {}
    project.visibility = sanitize_visibility(data.get('visibility') or project.visibility or 'family')
    project.allowed_members_json = json.dumps(parse_allowed_members(data.get('allowed_members') or []))
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/permission-schemes/space/<int:space_id>', methods=['PUT'])
def api_update_permission_scheme_space(space_id):
    if not _current_member_is_admin():
        return jsonify({'ok': False, 'error': 'Only admins can manage permission schemes'}), 403
    space = WorkspaceSpace.query.get_or_404(space_id)
    data = request.get_json(force=True) or {}
    space.visibility = sanitize_visibility(data.get('visibility') or space.visibility or 'family')
    space.allowed_members_json = json.dumps(parse_allowed_members(data.get('allowed_members') or []))
    db.session.commit()
    return jsonify({'ok': True})

# Member login/PIN verification
@main_bp.route('/api/member-login', methods=['POST'])
def api_member_login():
    """Verify PIN and set member in session"""
    try:
        from flask import session
        data = request.get_json(force=True) or {}
        name = sanitize_text(str(data.get('name', '')).strip())
        pin = str(data.get('pin', '')).strip()
        
        if not name:
            return jsonify({"ok": False, "error": "Member name required"}), 400
        
        member = FamilyMember.query.filter_by(name=name).first()
        if not member:
            # Allow config admin identity even when not represented in family_member table.
            if _is_config_admin_name(name):
                session['current_member'] = name
                session.pop('shared_guest_mode', None)
                session.pop('shared_calendar_token', None)
                return jsonify({
                    "ok": True,
                    "name": name,
                    "message": f"Welcome, {name}",
                    "is_admin": True
                })
            return jsonify({"ok": False, "error": "Member not found"}), 404
        
        # If no PIN is set, allow login without PIN
        if not member.pin:
            session['current_member'] = name
            session.pop('shared_guest_mode', None)
            session.pop('shared_calendar_token', None)
            return jsonify({
                "ok": True,
                "name": name,
                "message": f"Welcome, {name}",
                "is_admin": _is_admin_name(name)
            })
        
        # PIN verification
        if pin != member.pin:
            return jsonify({"ok": False, "error": "Invalid PIN"}), 403
        
        session['current_member'] = name
        session.pop('shared_guest_mode', None)
        session.pop('shared_calendar_token', None)
        return jsonify({
            "ok": True,
            "name": name,
            "message": f"Welcome, {name}",
            "is_admin": _is_admin_name(name)
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/member-logout', methods=['POST'])
def api_member_logout():
    """Clear member from session"""
    try:
        from flask import session
        session.pop('current_member', None)
        session.pop('shared_guest_mode', None)
        session.pop('shared_calendar_token', None)
        return jsonify({"ok": True, "message": "Logged out successfully"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/current-member', methods=['GET'])
def api_current_member():
    """Get currently logged-in member from session"""
    current = session.get('current_member', None)
    return jsonify({
        "ok": True,
        "member": current,
        "is_admin": _is_admin_name(current),
    })


@main_bp.route('/api/member/set-pin', methods=['POST'])
def api_member_set_pin():
    """Member sets PIN for the first time (no verification needed)"""
    try:
        from flask import session
        data = request.get_json(force=True) or {}
        pin = str(data.get('pin', '')).strip()
        email = sanitize_text(str(data.get('email', '')).strip())
        
        # Get the member name from the request (for first-time setup)
        # or from session (for self-service PIN changes)
        member_name = data.get('member_name') or session.get('current_member')
        
        if not member_name:
            return jsonify({"ok": False, "error": "No member specified"}), 400
        
        # Validate PIN: must be exactly 4 digits
        if not pin or not pin.isdigit() or len(pin) != 4:
            return jsonify({"ok": False, "error": "PIN must be exactly 4 digits"}), 400
        
        member = FamilyMember.query.filter_by(name=member_name).first()
        if not member:
            return jsonify({"ok": False, "error": "Member not found"}), 404
        
        # Only allow setting PIN if no PIN is currently set
        if member.pin:
            return jsonify({"ok": False, "error": "PIN already set. Use change-pin to update."}), 400

        # Email is required for first-time setup if not already present
        current_email = sanitize_text((member.email or '')).strip() if hasattr(member, 'email') else ''
        if not current_email and not email:
            return jsonify({"ok": False, "error": "Email is required for first-time setup"}), 400

        chosen_email = email or current_email
        if chosen_email and ('@' not in chosen_email or '.' not in chosen_email.split('@')[-1]):
            return jsonify({"ok": False, "error": "Please provide a valid email address"}), 400
        
        member.pin = pin
        if hasattr(member, 'email') and chosen_email:
            member.email = chosen_email
        db.session.commit()
        
        return jsonify({
            "ok": True,
            "message": f"PIN set successfully for {member_name}",
            "member": member_name
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/member/change-pin', methods=['POST'])
def api_member_change_pin():
    """Member changes their own PIN (requires current PIN verification)"""
    try:
        from flask import session
        current = session.get('current_member')
        if not current:
            return jsonify({"ok": False, "error": "Not logged in"}), 401
        
        data = request.get_json(force=True) or {}
        current_pin = str(data.get('current_pin', '')).strip()
        new_pin = str(data.get('new_pin', '')).strip()
        
        member = FamilyMember.query.filter_by(name=current).first()
        if not member:
            return jsonify({"ok": False, "error": "Member not found"}), 404
        
        # Verify current PIN
        if not member.pin or current_pin != member.pin:
            return jsonify({"ok": False, "error": "Current PIN is incorrect"}), 403
        
        # Validate new PIN: must be exactly 4 digits
        if not new_pin or not new_pin.isdigit() or len(new_pin) != 4:
            return jsonify({"ok": False, "error": "New PIN must be exactly 4 digits"}), 400
        
        # Prevent using same PIN
        if new_pin == current_pin:
            return jsonify({"ok": False, "error": "New PIN must be different from current PIN"}), 400
        
        member.pin = new_pin
        db.session.commit()
        
        return jsonify({
            "ok": True,
            "message": "PIN changed successfully"
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/member/<int:member_id>/reset-pin', methods=['POST'])
def api_reset_member_pin(member_id):
    """Admin resets a member's forgotten PIN (clears PIN, member sets new on next login)"""
    try:
        # Only admin can reset/remove PINs
        if not _current_member_is_admin():
            return jsonify({"ok": False, "error": "Only administrator can reset PINs"}), 403
        
        member = FamilyMember.query.get(member_id)
        if not member:
            return jsonify({"ok": False, "error": "Member not found"}), 404
        
        member_name = member.name
        if not member.pin:
            return jsonify({
                "ok": True,
                "message": f"PIN is already cleared for {member_name}.",
                "member_name": member_name
            })

        member.pin = None  # Clear PIN
        db.session.commit()
        
        return jsonify({
            "ok": True,
            "message": f"PIN reset for {member_name}. They can set a new PIN on next login.",
            "member_name": member_name
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/member/<int:member_id>/remove-pin', methods=['POST'])
def api_remove_member_pin(member_id):
    """Admin removes a member PIN (alias of reset-pin)."""
    return api_reset_member_pin(member_id)


@main_bp.route('/api/member/set-status', methods=['POST'])
def api_set_member_status():
    """Set current member's status (home, away, at_work, traveling)"""
    try:
        from flask import session
        data = request.get_json(force=True) or {}
        status = str(data.get('status', 'home')).strip().lower()
        member_name = data.get('member_name')
        
        # Allow any custom status, not just predefined ones
        # Sanitize to prevent injection
        if not status or len(status) > 100:
            return jsonify({"ok": False, "error": "Status must be 1-100 characters"}), 400
        
        # Remove special characters but allow spaces
        status = sanitize_text(status)
        
        if not member_name:
            return jsonify({"ok": False, "error": "Member name required"}), 400
        
        admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
        
        # Admin status doesn't need to be tracked in DB (or could be, but not required)
        if member_name != admin_name:
            member = FamilyMember.query.filter_by(name=member_name).first()
            if not member:
                return jsonify({"ok": False, "error": "Member not found"}), 404
            
            member.status = status
            db.session.commit()
        
        # Also store in session for current user
        session['member_status'] = status
        
        return jsonify({
            "ok": True,
            "status": status,
            "message": f"Status updated to {status}"
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/member/<int:member_id>/profile', methods=['GET'])
def api_get_member_profile(member_id):
    """Get member profile information"""
    member = FamilyMember.query.get_or_404(member_id)
    return jsonify({
        "ok": True,
        "id": member.id,
        "name": member.name,
        "email": member.email,
        "birthday": member.birthday.isoformat() if member.birthday else None,
        "favorite_color": member.favorite_color,
        "favorite_song": member.favorite_song,
        "amazon_wishlist_url": member.amazon_wishlist_url,
        "status": member.status or 'home',
        "avatar": member.avatar,
        "status_updated_at": member.status_updated_at.isoformat() if member.status_updated_at else None
    })


@main_bp.route('/api/member/<int:member_id>/profile', methods=['POST'])
def api_update_member_profile(member_id):
    """Update member profile (avatar, status, etc.)"""
    try:
        member = FamilyMember.query.get_or_404(member_id)
        data = request.get_json()
        
        email_changed = False

        # Update avatar if provided (base64 encoded image)
        if 'avatar' in data:
            avatar = data['avatar']
            if avatar:
                # Validate base64 data URL format
                if not avatar.startswith('data:image/'):
                    return jsonify({"ok": False, "error": "Invalid image format"}), 400
                # Limit avatar size to 500KB
                if len(avatar) > 500000:
                    return jsonify({"ok": False, "error": "Image too large (max 500KB)"}), 400
            member.avatar = avatar

        # Update email if provided
        if 'email' in data:
            email = sanitize_text(str(data.get('email') or '').strip())
            if email and ('@' not in email or '.' not in email.split('@')[-1]):
                return jsonify({"ok": False, "error": "Invalid email format"}), 400
            old_email = sanitize_text(member.email or '').strip()
            new_email = email or None
            email_changed = (old_email != sanitize_text(new_email or '').strip())
            member.email = new_email
        
        # Update status if provided
        if 'status' in data:
            status = sanitize_text(data['status']).strip()
            if status:
                if len(status) > 100:
                    return jsonify({"ok": False, "error": "Status too long (max 100 chars)"}), 400
                member.status = status
                member.status_updated_at = db.func.now()

        if 'birthday' in data:
            birthday_raw = sanitize_text(str(data.get('birthday') or '').strip())
            if birthday_raw:
                try:
                    member.birthday = datetime.strptime(birthday_raw, '%Y-%m-%d').date()
                except Exception:
                    return jsonify({"ok": False, "error": "Birthday must be in YYYY-MM-DD format"}), 400
            else:
                member.birthday = None

        if 'favorite_color' in data:
            member.favorite_color = sanitize_text(str(data.get('favorite_color') or '').strip()) or None

        if 'favorite_song' in data:
            member.favorite_song = sanitize_text(str(data.get('favorite_song') or '').strip()) or None

        if 'amazon_wishlist_url' in data:
            wishlist = sanitize_text(str(data.get('amazon_wishlist_url') or '').strip())
            if wishlist and not (wishlist.startswith('http://') or wishlist.startswith('https://')):
                wishlist = 'https://' + wishlist
            member.amazon_wishlist_url = wishlist or None
        
        db.session.commit()

        if email_changed and member.email:
            ok, msg = send_test_email(member.email, member.name)
            if not ok:
                return jsonify({"ok": False, "error": msg}), 400
        
        return jsonify({
            "ok": True,
            "id": member.id,
            "name": member.name,
            "email": member.email,
            "birthday": member.birthday.isoformat() if member.birthday else None,
            "favorite_color": member.favorite_color,
            "favorite_song": member.favorite_song,
            "amazon_wishlist_url": member.amazon_wishlist_url,
            "status": member.status or 'home',
            "avatar": member.avatar,
            "message": "Profile updated" + (" and test email sent" if email_changed and member.email else "")
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/member/<int:member_id>/profile/test-email', methods=['POST'])
def api_member_profile_test_email(member_id):
    try:
        member = FamilyMember.query.get_or_404(member_id)
        if not member.email:
            return jsonify({"ok": False, "error": "No email set for this member profile"}), 400
        ok, msg = send_test_email(member.email, member.name)
        if not ok:
            return jsonify({"ok": False, "error": msg}), 400
        return jsonify({"ok": True, "message": msg})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/member/<int:member_id>/public-profile', methods=['GET'])
def api_get_member_public_profile(member_id):
    member = FamilyMember.query.get_or_404(member_id)

    actions = []
    try:
        rows = (
            ActivityLog.query
            .filter(ActivityLog.actor == member.name)
            .order_by(ActivityLog.timestamp.desc(), ActivityLog.id.desc())
            .limit(8)
            .all()
        )
        for row in rows:
            actions.append({
                'summary': row.summary or row.title or row.action or 'Activity',
                'action': row.action or '',
                'timestamp': row.timestamp.isoformat() if row.timestamp else None,
            })
    except Exception:
        actions = []

    return jsonify({
        'ok': True,
        'id': member.id,
        'name': member.name,
        'avatar': member.avatar,
        'status': member.status or 'home',
        'birthday': member.birthday.isoformat() if member.birthday else None,
        'favorite_color': member.favorite_color,
        'favorite_song': member.favorite_song,
        'amazon_wishlist_url': member.amazon_wishlist_url,
        'recent_actions': actions,
    })
