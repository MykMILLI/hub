from flask import render_template, request, redirect, url_for, jsonify, current_app, flash
from ..models import db, HubSettings, FamilyMember
from ..blueprints import main_bp
from ..security import sanitize_text
import yaml
import os


@main_bp.route('/settings', methods=['GET', 'POST'])
def settings():
    hub_settings = HubSettings.query.first()
    if not hub_settings:
        hub_settings = HubSettings(hub_name='HomeHub')
        db.session.add(hub_settings)
        db.session.commit()
    
    if request.method == 'POST':
        hub_name = sanitize_text(request.form.get('hub_name', 'HomeHub')).strip()
        if hub_name:
            hub_settings.hub_name = hub_name
            db.session.commit()
        return redirect(url_for('main.settings'))
    
    config = current_app.config['HOMEHUB_CONFIG']
    family_members = FamilyMember.query.order_by(FamilyMember.added_at).all()
    return render_template('settings.html', config=config, hub_settings=hub_settings, family_members=family_members)


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
    return jsonify({
        "ok": True,
        "members": [{"id": m.id, "name": m.name, "pin": bool(m.pin)} for m in members]
    })


@main_bp.route('/api/family-members', methods=['POST'])
def api_add_family_member():
    """Add a new family member"""
    try:
        data = request.get_json(force=True) or {}
        name = sanitize_text(str(data.get('name', '')).strip())
        
        if not name:
            return jsonify({"ok": False, "error": "Family member name cannot be empty"}), 400
        
        # Check if member already exists
        existing = FamilyMember.query.filter_by(name=name).first()
        if existing:
            return jsonify({"ok": False, "error": "Family member already exists"}), 400
        
        member = FamilyMember(name=name)
        db.session.add(member)
        db.session.commit()
        
        return jsonify({
            "ok": True,
            "member": {"id": member.id, "name": member.name}
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/family-members/<int:member_id>', methods=['PUT'])
def api_update_family_member(member_id):
    """Update a family member's name and/or PIN"""
    try:
        member = FamilyMember.query.get(member_id)
        if not member:
            return jsonify({"ok": False, "error": "Family member not found"}), 404
        
        data = request.get_json(force=True) or {}
        name = sanitize_text(str(data.get('name', '')).strip()) if 'name' in data else member.name
        pin = str(data.get('pin', '')).strip() if 'pin' in data else member.pin
        
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
        
        member.name = name
        member.pin = pin if pin else None
        db.session.commit()
        
        return jsonify({
            "ok": True,
            "member": {"id": member.id, "name": member.name, "pin": member.pin or ""}
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/family-members/<int:member_id>', methods=['DELETE'])
def api_delete_family_member(member_id):
    """Delete a family member"""
    try:
        member = FamilyMember.query.get(member_id)
        if not member:
            return jsonify({"ok": False, "error": "Family member not found"}), 404
        
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
        # Valid menu items that can be toggled
        valid_items = [
            'welcome', 'shared_notes', 'shared_cloud', 'shopping_list',
            'chores', 'recipes', 'expiry_tracker', 'url_shortener',
            'media_downloader', 'pdf_compressor', 'qr_generator',
            'expense_tracker', 'settings'
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
        
        admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
        
        # Admin (can be identified by admin_name) bypasses PIN
        if name == admin_name:
            session['current_member'] = name
            return jsonify({
                "ok": True,
                "name": name,
                "message": f"Welcome, {name}",
                "is_admin": True
            })
        
        # Regular family member requires PIN verification
        member = FamilyMember.query.filter_by(name=name).first()
        if not member:
            return jsonify({"ok": False, "error": "Member not found"}), 404
        
        # If no PIN is set, allow login without PIN
        if not member.pin:
            session['current_member'] = name
            return jsonify({
                "ok": True,
                "name": name,
                "message": f"Welcome, {name}",
                "is_admin": False
            })
        
        # PIN verification
        if pin != member.pin:
            return jsonify({"ok": False, "error": "Invalid PIN"}), 403
        
        session['current_member'] = name
        return jsonify({
            "ok": True,
            "name": name,
            "message": f"Welcome, {name}",
            "is_admin": False
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/member-logout', methods=['POST'])
def api_member_logout():
    """Clear member from session"""
    try:
        from flask import session
        session.pop('current_member', None)
        return jsonify({"ok": True, "message": "Logged out successfully"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/current-member', methods=['GET'])
def api_current_member():
    """Get currently logged-in member from session"""
    from flask import session
    current = session.get('current_member', None)
    return jsonify({
        "ok": True,
        "member": current
    })


@main_bp.route('/api/member/set-pin', methods=['POST'])
def api_member_set_pin():
    """Member sets PIN for the first time (no verification needed)"""
    try:
        from flask import session
        data = request.get_json(force=True) or {}
        pin = str(data.get('pin', '')).strip()
        
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
        
        member.pin = pin
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
        from flask import session
        admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
        current = session.get('current_member', '')
        
        # Only admin can reset PINs
        if current != admin_name:
            return jsonify({"ok": False, "error": "Only administrator can reset PINs"}), 403
        
        member = FamilyMember.query.get(member_id)
        if not member:
            return jsonify({"ok": False, "error": "Member not found"}), 404
        
        # Prevent resetting admin's PIN
        if member.name == admin_name:
            return jsonify({"ok": False, "error": "Cannot reset admin PIN"}), 400
        
        member_name = member.name
        member.pin = None  # Clear PIN
        db.session.commit()
        
        return jsonify({
            "ok": True,
            "message": f"PIN reset for {member_name}. They can set a new PIN on next login.",
            "member_name": member_name
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


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
        
        # Update status if provided
        if 'status' in data:
            status = sanitize_text(data['status']).strip()
            if status:
                if len(status) > 100:
                    return jsonify({"ok": False, "error": "Status too long (max 100 chars)"}), 400
                member.status = status
                member.status_updated_at = db.func.now()
        
        db.session.commit()
        
        return jsonify({
            "ok": True,
            "id": member.id,
            "name": member.name,
            "status": member.status or 'home',
            "avatar": member.avatar,
            "message": "Profile updated"
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
