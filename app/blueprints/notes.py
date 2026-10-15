import json
import os
import secrets
from datetime import datetime

from flask import current_app, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from .. import db
from ..blueprints import main_bp
from ..models import (
    FamilyMember,
    Note,
    ProjectCard,
    ProjectColumn,
    SpacePage,
    WorkspaceProject,
    WorkspaceSpace,
    WorkspaceTemplate,
)
from ..permissions import has_resource_permission
from ..security import sanitize_html, sanitize_text


ALLOWED_VISIBILITY = {'family', 'restricted', 'private'}
ALLOWED_PRIORITIES = {'low', 'normal', 'high', 'urgent'}
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')


def _current_member_name() -> str:
    member = sanitize_text(session.get('current_member') or '').strip()
    if member:
        return member
    return sanitize_text(current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator'))


def _is_admin(user: str) -> bool:
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    return sanitize_text(user or '') in admin_aliases


def _parse_allowed_members(raw_value) -> list[str]:
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


def _sanitize_visibility(value: str) -> str:
    clean = sanitize_text(value or '').strip().lower()
    return clean if clean in ALLOWED_VISIBILITY else 'family'


def _entity_visible_to_member(visibility: str, owner: str, allowed_members_json: str, member: str) -> bool:
    if _is_admin(member):
        return True
    if member == owner:
        return True
    visible = _sanitize_visibility(visibility)
    if visible == 'family':
        return True
    if visible == 'private':
        return False
    allowed = _parse_allowed_members(allowed_members_json)
    return member in allowed


def _entity_editable_by_member(owner: str, member: str) -> bool:
    return _is_admin(member) or owner == member


def _can_read_project(project: WorkspaceProject, member: str) -> bool:
    if has_resource_permission('project', resource_key=project.project_key, resource_id=project.id, member=member, action='read'):
        return True
    return _entity_visible_to_member(project.visibility, project.created_by, project.allowed_members_json, member)


def _can_write_project(project: WorkspaceProject, member: str) -> bool:
    if has_resource_permission('project', resource_key=project.project_key, resource_id=project.id, member=member, action='write'):
        return True
    return _entity_editable_by_member(project.created_by, member)


def _can_admin_project(project: WorkspaceProject, member: str) -> bool:
    if has_resource_permission('project', resource_key=project.project_key, resource_id=project.id, member=member, action='admin'):
        return True
    return _entity_editable_by_member(project.created_by, member)


def _can_read_space(space: WorkspaceSpace, member: str) -> bool:
    if has_resource_permission('space', resource_key=space.space_key, resource_id=space.id, member=member, action='read'):
        return True
    return _entity_visible_to_member(space.visibility, space.created_by, space.allowed_members_json, member)


def _can_write_space(space: WorkspaceSpace, member: str) -> bool:
    if has_resource_permission('space', resource_key=space.space_key, resource_id=space.id, member=member, action='write'):
        return True
    return _entity_editable_by_member(space.created_by, member)


def _can_admin_space(space: WorkspaceSpace, member: str) -> bool:
    if has_resource_permission('space', resource_key=space.space_key, resource_id=space.id, member=member, action='admin'):
        return True
    return _entity_editable_by_member(space.created_by, member)


def _normalize_key(raw_key: str, fallback_name: str) -> str:
    source = sanitize_text(raw_key or '').upper().strip()
    if not source:
        source = ''.join(ch for ch in sanitize_text(fallback_name or '').upper() if ch.isalnum())[:6]
    cleaned = ''.join(ch for ch in source if ch.isalnum())[:10]
    if not cleaned:
        cleaned = 'PRJ'
    return cleaned


def _next_unique_key(base_key: str, table: str, key_field: str) -> str:
    candidate = base_key
    for i in range(1, 1000):
        exists = db.session.execute(
            db.text(f"SELECT 1 FROM {table} WHERE {key_field} = :key LIMIT 1"),
            {'key': candidate},
        ).first()
        if not exists:
            return candidate
        candidate = f"{base_key}{i}"
    return f"{base_key}{secrets.randbelow(9999)}"


def _safe_json_load(value: str, default):
    try:
        return json.loads(value or '')
    except Exception:
        return default


def _card_to_dict(card: ProjectCard) -> dict:
    return {
        'id': card.id,
        'project_id': card.project_id,
        'column_id': card.column_id,
        'title': card.title,
        'description': card.description or '',
        'priority': card.priority or 'normal',
        'start_date': card.start_date.strftime('%Y-%m-%d') if card.start_date else '',
        'due_date': card.due_date.strftime('%Y-%m-%d') if card.due_date else '',
        'position': int(card.position or 0),
        'creator': card.creator,
    }


def _project_to_dict(project: WorkspaceProject) -> dict:
    return {
        'id': project.id,
        'name': project.name,
        'key': project.project_key,
        'description': project.description or '',
        'visibility': project.visibility or 'family',
        'allowed_members': _parse_allowed_members(project.allowed_members_json),
        'created_by': project.created_by,
        'linked_space_id': project.linked_space_id,
        'archived': bool(project.archived),
        'share_url': f"/shared/project/{project.share_token}",
        'detail_url': f"/projects/{project.project_key}",
    }


def _space_to_dict(space: WorkspaceSpace) -> dict:
    return {
        'id': space.id,
        'name': space.name,
        'key': space.space_key,
        'description': space.description or '',
        'visibility': space.visibility or 'family',
        'allowed_members': _parse_allowed_members(space.allowed_members_json),
        'created_by': space.created_by,
        'linked_project_id': space.linked_project_id,
        'archived': bool(space.archived),
        'share_url': f"/shared/space/{space.share_token}",
        'detail_url': f"/spaces/{space.space_key}",
    }


def _member_names() -> list[str]:
    try:
        rows = FamilyMember.query.order_by(FamilyMember.name.asc()).all()
        if rows:
            return [sanitize_text((r.name or '')).strip() for r in rows if (r.name or '').strip()]
    except Exception:
        pass
    config_members = current_app.config['HOMEHUB_CONFIG'].get('family_members') or []
    if isinstance(config_members, list):
        return [sanitize_text(str(m)).strip() for m in config_members if sanitize_text(str(m)).strip()]
    return []


def _render_notes_page(selected_project_key: str = '', selected_space_key: str = '', page_mode: str = 'workspace'):
    member = _current_member_name()
    selected_project_key = sanitize_text(selected_project_key or '').strip().upper()
    selected_space_key = sanitize_text(selected_space_key or '').strip().upper()

    all_templates = WorkspaceTemplate.query.filter_by(is_public=True).order_by(WorkspaceTemplate.created_at.desc()).all()
    all_projects = WorkspaceProject.query.order_by(WorkspaceProject.updated_at.desc()).all()
    all_spaces = WorkspaceSpace.query.order_by(WorkspaceSpace.updated_at.desc()).all()

    projects = [
        p for p in all_projects
        if _can_read_project(p, member)
    ]
    spaces = [
        s for s in all_spaces
        if _can_read_space(s, member)
    ]

    project_ids = [p.id for p in projects]
    space_ids = [s.id for s in spaces]

    columns = []
    cards = []
    pages = []
    if project_ids:
        columns = ProjectColumn.query.filter(ProjectColumn.project_id.in_(project_ids)).order_by(ProjectColumn.position.asc(), ProjectColumn.id.asc()).all()
        cards = ProjectCard.query.filter(ProjectCard.project_id.in_(project_ids)).order_by(ProjectCard.position.asc(), ProjectCard.id.asc()).all()
    if space_ids:
        pages = SpacePage.query.filter(SpacePage.space_id.in_(space_ids)).order_by(SpacePage.updated_at.desc()).all()

    columns_by_project = {}
    for col in columns:
        columns_by_project.setdefault(col.project_id, []).append({
            'id': col.id,
            'name': col.name,
            'position': col.position,
        })

    cards_by_project = {}
    for card in cards:
        cards_by_project.setdefault(card.project_id, []).append(_card_to_dict(card))

    pages_by_space = {}
    for page in pages:
        pages_by_space.setdefault(page.space_id, []).append({
            'id': page.id,
            'title': page.title,
            'content': page.content or '',
            'media': _safe_json_load(page.media_json, []),
            'created_by': page.created_by,
            'updated_by': page.updated_by or page.created_by,
            'updated_at': (page.updated_at or page.created_at).strftime('%Y-%m-%d %H:%M'),
        })

    config = current_app.config['HOMEHUB_CONFIG']
    return render_template(
        'notes.html',
        config=config,
        current_member_name=member,
        members=_member_names(),
        templates=[{
            'id': t.id,
            'name': t.name,
            'template_type': t.template_type,
            'description': t.description or '',
            'config': _safe_json_load(t.config_json, {}),
            'created_by': t.created_by,
        } for t in all_templates],
        projects=[_project_to_dict(p) for p in projects],
        spaces=[_space_to_dict(s) for s in spaces],
        columns_by_project=columns_by_project,
        cards_by_project=cards_by_project,
        pages_by_space=pages_by_space,
        selected_project_key=selected_project_key,
        selected_space_key=selected_space_key,
        page_mode=page_mode,
    )


@main_bp.route('/notes', methods=['GET'])
def notes():
    selected_project_key = sanitize_text(request.args.get('project_key') or '').strip().upper()
    selected_space_key = sanitize_text(request.args.get('space_key') or '').strip().upper()
    return _render_notes_page(
        selected_project_key=selected_project_key,
        selected_space_key=selected_space_key,
        page_mode='workspace',
    )


@main_bp.route('/api/workspaces/overview', methods=['GET'])
def api_workspaces_overview():
    member = _current_member_name()

    all_templates = WorkspaceTemplate.query.filter_by(is_public=True).order_by(WorkspaceTemplate.created_at.desc()).all()
    all_projects = WorkspaceProject.query.order_by(WorkspaceProject.updated_at.desc()).all()
    all_spaces = WorkspaceSpace.query.order_by(WorkspaceSpace.updated_at.desc()).all()

    projects = [p for p in all_projects if _can_read_project(p, member)]
    spaces = [s for s in all_spaces if _can_read_space(s, member)]

    project_ids = [p.id for p in projects]
    space_ids = [s.id for s in spaces]

    columns = []
    cards = []
    pages = []
    if project_ids:
        columns = ProjectColumn.query.filter(ProjectColumn.project_id.in_(project_ids)).order_by(ProjectColumn.position.asc(), ProjectColumn.id.asc()).all()
        cards = ProjectCard.query.filter(ProjectCard.project_id.in_(project_ids)).order_by(ProjectCard.position.asc(), ProjectCard.id.asc()).all()
    if space_ids:
        pages = SpacePage.query.filter(SpacePage.space_id.in_(space_ids)).order_by(SpacePage.updated_at.desc()).all()

    columns_by_project = {}
    for col in columns:
        columns_by_project.setdefault(col.project_id, []).append({
            'id': col.id,
            'name': col.name,
            'position': col.position,
        })

    cards_by_project = {}
    for card in cards:
        cards_by_project.setdefault(card.project_id, []).append(_card_to_dict(card))

    pages_by_space = {}
    for page in pages:
        pages_by_space.setdefault(page.space_id, []).append({
            'id': page.id,
            'title': page.title,
            'content': page.content or '',
            'media': _safe_json_load(page.media_json, []),
            'created_by': page.created_by,
            'updated_by': page.updated_by or page.created_by,
            'updated_at': (page.updated_at or page.created_at).strftime('%Y-%m-%d %H:%M'),
        })

    return jsonify({
        'ok': True,
        'current_member': member,
        'members': _member_names(),
        'templates': [{
            'id': t.id,
            'name': t.name,
            'template_type': t.template_type,
            'description': t.description or '',
            'config': _safe_json_load(t.config_json, {}),
            'created_by': t.created_by,
        } for t in all_templates],
        'projects': [_project_to_dict(p) for p in projects],
        'spaces': [_space_to_dict(s) for s in spaces],
        'columns_by_project': columns_by_project,
        'cards_by_project': cards_by_project,
        'pages_by_space': pages_by_space,
    })


@main_bp.route('/projects/<project_key>', methods=['GET'])
def project_by_key(project_key: str):
    clean_key = sanitize_text(project_key or '').strip().upper()
    return _render_notes_page(selected_project_key=clean_key, page_mode='project-detail')


@main_bp.route('/spaces/<space_key>', methods=['GET'])
def space_by_key(space_key: str):
    clean_key = sanitize_text(space_key or '').strip().upper()
    return _render_notes_page(selected_space_key=clean_key, page_mode='space-detail')


@main_bp.route('/api/workspaces/templates', methods=['POST'])
def api_create_template():
    data = request.get_json(silent=True) or {}
    member = _current_member_name()
    name = sanitize_text(data.get('name') or '').strip()
    template_type = sanitize_text(data.get('template_type') or '').strip().lower()
    description = sanitize_text(data.get('description') or '').strip()
    config_payload = data.get('config') if isinstance(data.get('config'), dict) else {}
    if not name:
        return jsonify({'ok': False, 'error': 'Template name is required'}), 400
    if template_type not in {'project', 'space'}:
        return jsonify({'ok': False, 'error': 'template_type must be project or space'}), 400

    template = WorkspaceTemplate(
        name=name,
        template_type=template_type,
        description=description,
        config_json=json.dumps(config_payload),
        created_by=member,
        is_public=True,
    )
    db.session.add(template)
    db.session.commit()
    return jsonify({'ok': True, 'template_id': template.id})


@main_bp.route('/api/workspaces/templates/<int:template_id>', methods=['PUT'])
def api_update_template(template_id: int):
    template = WorkspaceTemplate.query.get_or_404(template_id)
    member = _current_member_name()
    if not (_is_admin(member) or template.created_by == member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    data = request.get_json(silent=True) or {}
    name = sanitize_text(data.get('name') or template.name).strip()
    description = sanitize_text(data.get('description') or template.description or '').strip()
    cfg = data.get('config')
    if not name:
        return jsonify({'ok': False, 'error': 'Template name is required'}), 400

    template.name = name
    template.description = description
    if isinstance(cfg, dict):
        template.config_json = json.dumps(cfg)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/workspaces/templates/<int:template_id>', methods=['DELETE'])
def api_delete_template(template_id: int):
    template = WorkspaceTemplate.query.get_or_404(template_id)
    member = _current_member_name()
    if not (_is_admin(member) or template.created_by == member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    db.session.delete(template)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/workspaces/wizard/create', methods=['POST'])
def api_wizard_create_workspace():
    data = request.get_json(silent=True) or {}
    member = _current_member_name()

    entity_type = sanitize_text(data.get('entity_type') or '').strip().lower()
    if entity_type not in {'project', 'space'}:
        return jsonify({'ok': False, 'error': 'entity_type must be project or space'}), 400

    name = sanitize_text(data.get('name') or '').strip()
    description = sanitize_html(data.get('description') or '')
    key_in = sanitize_text(data.get('key') or '')
    visibility = _sanitize_visibility(data.get('visibility') or 'family')
    allowed_members = _parse_allowed_members(data.get('allowed_members') or [])
    if member in allowed_members:
        allowed_members = [m for m in allowed_members if m != member]
    if visibility == 'private':
        allowed_members = []

    template_id = data.get('template_id')
    template = None
    if template_id:
        try:
            template = WorkspaceTemplate.query.get(int(template_id))
        except Exception:
            template = None

    if not name:
        return jsonify({'ok': False, 'error': 'Name is required'}), 400

    if entity_type == 'project':
        base_key = _normalize_key(key_in, name)
        project_key = _next_unique_key(base_key, 'workspace_project', 'project_key')
        share_token = secrets.token_urlsafe(24)
        linked_space_id = data.get('linked_space_id')
        try:
            linked_space_id = int(linked_space_id) if linked_space_id else None
        except Exception:
            linked_space_id = None

        project = WorkspaceProject(
            name=name,
            project_key=project_key,
            description=description,
            visibility=visibility,
            allowed_members_json=json.dumps(allowed_members),
            created_by=member,
            template_id=template.id if template else None,
            linked_space_id=linked_space_id,
            share_token=share_token,
        )
        db.session.add(project)
        db.session.flush()
        if not linked_space_id:
            space_key = _next_unique_key(_normalize_key('', f'{name} Wiki'), 'workspace_space', 'space_key')
            linked_space = WorkspaceSpace(
                name=f'{name} Wiki',
                space_key=space_key,
                description=f'Project wiki for {name}',
                visibility=visibility,
                allowed_members_json=json.dumps(allowed_members),
                created_by=member,
                share_token=secrets.token_urlsafe(24),
                linked_project_id=project.id,
            )
            db.session.add(linked_space)
            db.session.flush()
            db.session.add(SpacePage(
                space_id=linked_space.id,
                title='Project overview',
                content=f'<h2>{sanitize_text(name)}</h2><p>Project documentation and decisions.</p>',
                media_json='[]',
                created_by=member,
                updated_by=member,
            ))
            project.linked_space_id = linked_space.id

        template_config = _safe_json_load(template.config_json, {}) if template and template.template_type == 'project' else {}
        incoming_columns = data.get('columns')
        if not isinstance(incoming_columns, list) or not incoming_columns:
            incoming_columns = template_config.get('columns') if isinstance(template_config.get('columns'), list) else []
        clean_columns = [sanitize_text(str(c)).strip() for c in incoming_columns if sanitize_text(str(c)).strip()]

        created_columns = []
        for index, col_name in enumerate(clean_columns):
            col = ProjectColumn(project_id=project.id, name=col_name, position=index)
            db.session.add(col)
            db.session.flush()
            created_columns.append(col)

        initial_cards = data.get('cards')
        if not isinstance(initial_cards, list):
            initial_cards = template_config.get('cards') if isinstance(template_config.get('cards'), list) else []

        for card_index, raw_card in enumerate(initial_cards):
            if not isinstance(raw_card, dict):
                continue
            title = sanitize_text(raw_card.get('title') or '').strip()
            if not title:
                continue
            card_column_name = sanitize_text(raw_card.get('column') or '').strip().lower()
            target_col = None
            for c in created_columns:
                if c.name.lower() == card_column_name:
                    target_col = c
                    break
            if not target_col and created_columns:
                target_col = created_columns[0]
            if not target_col:
                continue
            priority = sanitize_text(raw_card.get('priority') or 'normal').strip().lower()
            if priority not in ALLOWED_PRIORITIES:
                priority = 'normal'
            due_date = None
            due_raw = sanitize_text(raw_card.get('due_date') or '').strip()
            if due_raw:
                try:
                    due_date = datetime.strptime(due_raw, '%Y-%m-%d').date()
                except Exception:
                    due_date = None
            start_date = None
            start_raw = sanitize_text(raw_card.get('start_date') or '').strip()
            if start_raw:
                try:
                    start_date = datetime.strptime(start_raw, '%Y-%m-%d').date()
                except Exception:
                    start_date = None
            if not start_date:
                start_date = due_date
            if start_date and due_date and start_date > due_date:
                start_date = due_date
            db.session.add(ProjectCard(
                project_id=project.id,
                column_id=target_col.id,
                title=title,
                description=sanitize_html(raw_card.get('description') or ''),
                priority=priority,
                start_date=start_date,
                due_date=due_date,
                position=card_index,
                creator=member,
            ))

        db.session.commit()
        return jsonify({'ok': True, 'entity_type': 'project', 'id': project.id, 'key': project.project_key})

    base_key = _normalize_key(key_in, name)
    space_key = _next_unique_key(base_key, 'workspace_space', 'space_key')
    share_token = secrets.token_urlsafe(24)
    linked_project_id = data.get('linked_project_id')
    try:
        linked_project_id = int(linked_project_id) if linked_project_id else None
    except Exception:
        linked_project_id = None

    space = WorkspaceSpace(
        name=name,
        space_key=space_key,
        description=description,
        visibility=visibility,
        allowed_members_json=json.dumps(allowed_members),
        created_by=member,
        template_id=template.id if template else None,
        linked_project_id=linked_project_id,
        share_token=share_token,
    )
    db.session.add(space)
    db.session.flush()

    template_config = _safe_json_load(template.config_json, {}) if template and template.template_type == 'space' else {}
    incoming_pages = data.get('pages')
    if not isinstance(incoming_pages, list):
        incoming_pages = template_config.get('pages') if isinstance(template_config.get('pages'), list) else []
    for page in incoming_pages:
        if not isinstance(page, dict):
            continue
        title = sanitize_text(page.get('title') or '').strip()
        if not title:
            continue
        db.session.add(SpacePage(
            space_id=space.id,
            title=title,
            content=sanitize_html(page.get('content') or ''),
            media_json='[]',
            created_by=member,
            updated_by=member,
        ))

    db.session.commit()
    return jsonify({'ok': True, 'entity_type': 'space', 'id': space.id, 'key': space.space_key})


@main_bp.route('/api/workspaces/projects/<int:project_id>/columns', methods=['POST'])
def api_add_project_column(project_id: int):
    project = WorkspaceProject.query.get_or_404(project_id)
    member = _current_member_name()
    if not _can_write_project(project, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    data = request.get_json(silent=True) or {}
    name = sanitize_text(data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Column name is required'}), 400
    max_pos = db.session.query(db.func.max(ProjectColumn.position)).filter_by(project_id=project.id).scalar()
    next_pos = int(max_pos or -1) + 1
    col = ProjectColumn(project_id=project.id, name=name, position=next_pos)
    db.session.add(col)
    db.session.commit()
    return jsonify({'ok': True, 'column_id': col.id})


@main_bp.route('/api/workspaces/projects/<int:project_id>', methods=['PUT'])
def api_update_project(project_id: int):
    project = WorkspaceProject.query.get_or_404(project_id)
    member = _current_member_name()
    if not _can_admin_project(project, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    data = request.get_json(silent=True) or {}
    name = sanitize_text(data.get('name') or project.name).strip()
    description = sanitize_html(data.get('description') if 'description' in data else (project.description or ''))
    visibility = _sanitize_visibility(data.get('visibility') if 'visibility' in data else (project.visibility or 'family'))
    allowed_members = _parse_allowed_members(data.get('allowed_members') if 'allowed_members' in data else project.allowed_members_json)
    archived = bool(data.get('archived', project.archived))
    linked_space_id = data.get('linked_space_id', project.linked_space_id)
    try:
        linked_space_id = int(linked_space_id) if linked_space_id else None
    except Exception:
        linked_space_id = None

    if not name:
        return jsonify({'ok': False, 'error': 'Project name is required'}), 400
    if visibility == 'private':
        allowed_members = []

    project.name = name
    project.description = description
    project.visibility = visibility
    project.allowed_members_json = json.dumps(allowed_members)
    project.archived = archived
    project.linked_space_id = linked_space_id
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/workspaces/projects/<int:project_id>', methods=['DELETE'])
def api_delete_project(project_id: int):
    project = WorkspaceProject.query.get_or_404(project_id)
    member = _current_member_name()
    if not _can_admin_project(project, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    ProjectCard.query.filter_by(project_id=project.id).delete()
    ProjectColumn.query.filter_by(project_id=project.id).delete()
    db.session.delete(project)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/workspaces/projects/<int:project_id>/cards', methods=['POST'])
def api_create_project_card(project_id: int):
    project = WorkspaceProject.query.get_or_404(project_id)
    member = _current_member_name()
    if not _can_write_project(project, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    data = request.get_json(silent=True) or {}
    title = sanitize_text(data.get('title') or '').strip()
    if not title:
        return jsonify({'ok': False, 'error': 'Card title is required'}), 400
    try:
        column_id = int(data.get('column_id'))
    except Exception:
        return jsonify({'ok': False, 'error': 'Valid column_id is required'}), 400

    col = ProjectColumn.query.filter_by(id=column_id, project_id=project.id).first()
    if not col:
        return jsonify({'ok': False, 'error': 'Column not found'}), 404

    priority = sanitize_text(data.get('priority') or 'normal').strip().lower()
    if priority not in ALLOWED_PRIORITIES:
        priority = 'normal'

    due_date = None
    due_raw = sanitize_text(data.get('due_date') or '').strip()
    if due_raw:
        try:
            due_date = datetime.strptime(due_raw, '%Y-%m-%d').date()
        except Exception:
            due_date = None
    start_date = None
    start_raw = sanitize_text(data.get('start_date') or '').strip()
    if start_raw:
        try:
            start_date = datetime.strptime(start_raw, '%Y-%m-%d').date()
        except Exception:
            start_date = None
    if not start_date:
        start_date = due_date
    if start_date and due_date and start_date > due_date:
        start_date = due_date

    max_pos = db.session.query(db.func.max(ProjectCard.position)).filter_by(project_id=project.id, column_id=col.id).scalar()
    next_pos = int(max_pos or -1) + 1

    card = ProjectCard(
        project_id=project.id,
        column_id=col.id,
        title=title,
        description=sanitize_html(data.get('description') or ''),
        priority=priority,
        start_date=start_date,
        due_date=due_date,
        position=next_pos,
        creator=member,
    )
    db.session.add(card)
    db.session.commit()
    return jsonify({'ok': True, 'card': _card_to_dict(card)})


@main_bp.route('/api/workspaces/cards/<int:card_id>/move', methods=['POST'])
def api_move_card(card_id: int):
    card = ProjectCard.query.get_or_404(card_id)
    project = WorkspaceProject.query.get_or_404(card.project_id)
    member = _current_member_name()
    if not _can_write_project(project, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    data = request.get_json(silent=True) or {}
    try:
        target_column_id = int(data.get('column_id'))
    except Exception:
        return jsonify({'ok': False, 'error': 'Valid column_id is required'}), 400
    target_column = ProjectColumn.query.filter_by(id=target_column_id, project_id=project.id).first()
    if not target_column:
        return jsonify({'ok': False, 'error': 'Target column not found'}), 404

    target_position = data.get('position')
    try:
        target_position = int(target_position)
    except Exception:
        target_position = None

    card.column_id = target_column.id
    if target_position is None:
        max_pos = db.session.query(db.func.max(ProjectCard.position)).filter_by(project_id=project.id, column_id=target_column.id).scalar()
        card.position = int(max_pos or -1) + 1
    else:
        card.position = max(0, target_position)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/workspaces/cards/<int:card_id>', methods=['PUT'])
def api_update_card(card_id: int):
    card = ProjectCard.query.get_or_404(card_id)
    project = WorkspaceProject.query.get_or_404(card.project_id)
    member = _current_member_name()
    if not _can_write_project(project, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    data = request.get_json(silent=True) or {}

    if 'title' in data:
        title = sanitize_text(data.get('title') or '').strip()
        if not title:
            return jsonify({'ok': False, 'error': 'Card title is required'}), 400
        card.title = title

    if 'description' in data:
        card.description = sanitize_html(data.get('description') or '')

    if 'priority' in data:
        priority = sanitize_text(data.get('priority') or 'normal').strip().lower()
        if priority not in ALLOWED_PRIORITIES:
            priority = 'normal'
        card.priority = priority

    if 'start_date' in data:
        start_raw = sanitize_text(data.get('start_date') or '').strip()
        if start_raw:
            try:
                card.start_date = datetime.strptime(start_raw, '%Y-%m-%d').date()
            except Exception:
                return jsonify({'ok': False, 'error': 'Invalid start_date format. Use YYYY-MM-DD'}), 400
        else:
            card.start_date = None

    if 'due_date' in data:
        due_raw = sanitize_text(data.get('due_date') or '').strip()
        if due_raw:
            try:
                card.due_date = datetime.strptime(due_raw, '%Y-%m-%d').date()
            except Exception:
                return jsonify({'ok': False, 'error': 'Invalid due_date format. Use YYYY-MM-DD'}), 400
        else:
            card.due_date = None

    if 'column_id' in data:
        try:
            target_column_id = int(data.get('column_id'))
        except Exception:
            return jsonify({'ok': False, 'error': 'Valid column_id is required'}), 400
        target_column = ProjectColumn.query.filter_by(id=target_column_id, project_id=project.id).first()
        if not target_column:
            return jsonify({'ok': False, 'error': 'Target column not found'}), 404
        if card.column_id != target_column.id:
            card.column_id = target_column.id
            max_pos = db.session.query(db.func.max(ProjectCard.position)).filter_by(project_id=project.id, column_id=target_column.id).scalar()
            card.position = int(max_pos or -1) + 1

    if card.start_date and card.due_date and card.start_date > card.due_date:
        return jsonify({'ok': False, 'error': 'start_date cannot be after due_date'}), 400

    db.session.commit()
    return jsonify({'ok': True, 'card': _card_to_dict(card)})


@main_bp.route('/api/workspaces/cards/<int:card_id>/delete', methods=['POST'])
def api_delete_card(card_id: int):
    card = ProjectCard.query.get_or_404(card_id)
    project = WorkspaceProject.query.get_or_404(card.project_id)
    member = _current_member_name()
    if not (_can_write_project(project, member) or _entity_editable_by_member(card.creator, member)):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    db.session.delete(card)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/workspaces/spaces/<int:space_id>/pages', methods=['POST'])
def api_create_space_page(space_id: int):
    space = WorkspaceSpace.query.get_or_404(space_id)
    member = _current_member_name()
    if not _can_write_space(space, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    title = sanitize_text(request.form.get('title') or '').strip()
    content = sanitize_html(request.form.get('content') or '')
    if not title:
        return jsonify({'ok': False, 'error': 'Page title is required'}), 400

    media_items = []
    files = request.files.getlist('media')
    for file in files:
        if not file or not getattr(file, 'filename', ''):
            continue
        safe_name = secure_filename(file.filename)
        unique_name = f"space_{space.id}_{int(datetime.utcnow().timestamp())}_{safe_name}"
        file_path = os.path.join(UPLOAD_FOLDER, unique_name)
        file.save(file_path)
        media_items.append({'filename': unique_name, 'url': f"/uploads/preview/{unique_name}"})

    page = SpacePage(
        space_id=space.id,
        title=title,
        content=content,
        media_json=json.dumps(media_items),
        created_by=member,
        updated_by=member,
    )
    db.session.add(page)
    db.session.commit()
    return jsonify({'ok': True, 'page_id': page.id})


@main_bp.route('/api/workspaces/spaces/<int:space_id>', methods=['PUT'])
def api_update_space(space_id: int):
    space = WorkspaceSpace.query.get_or_404(space_id)
    member = _current_member_name()
    if not _can_admin_space(space, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    data = request.get_json(silent=True) or {}
    name = sanitize_text(data.get('name') or space.name).strip()
    description = sanitize_html(data.get('description') if 'description' in data else (space.description or ''))
    visibility = _sanitize_visibility(data.get('visibility') if 'visibility' in data else (space.visibility or 'family'))
    allowed_members = _parse_allowed_members(data.get('allowed_members') if 'allowed_members' in data else space.allowed_members_json)
    archived = bool(data.get('archived', space.archived))
    linked_project_id = data.get('linked_project_id', space.linked_project_id)
    try:
        linked_project_id = int(linked_project_id) if linked_project_id else None
    except Exception:
        linked_project_id = None

    if not name:
        return jsonify({'ok': False, 'error': 'Space name is required'}), 400
    if visibility == 'private':
        allowed_members = []

    space.name = name
    space.description = description
    space.visibility = visibility
    space.allowed_members_json = json.dumps(allowed_members)
    space.archived = archived
    space.linked_project_id = linked_project_id
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/workspaces/spaces/<int:space_id>', methods=['DELETE'])
def api_delete_space(space_id: int):
    space = WorkspaceSpace.query.get_or_404(space_id)
    member = _current_member_name()
    if not _can_admin_space(space, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    SpacePage.query.filter_by(space_id=space.id).delete()
    db.session.delete(space)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/workspaces/pages/<int:page_id>/delete', methods=['POST'])
def api_delete_space_page(page_id: int):
    page = SpacePage.query.get_or_404(page_id)
    space = WorkspaceSpace.query.get_or_404(page.space_id)
    member = _current_member_name()
    if not (_can_write_space(space, member) or _entity_editable_by_member(page.created_by, member)):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    db.session.delete(page)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/shared/project/<token>', methods=['GET'])
def shared_project(token: str):
    project = WorkspaceProject.query.filter_by(share_token=sanitize_text(token or '').strip()).first_or_404()
    if bool(project.archived):
        return render_template('shared_project.html', project=_project_to_dict(project), columns=[], cards=[])
    columns = ProjectColumn.query.filter_by(project_id=project.id).order_by(ProjectColumn.position.asc(), ProjectColumn.id.asc()).all()
    cards = ProjectCard.query.filter_by(project_id=project.id).order_by(ProjectCard.position.asc(), ProjectCard.id.asc()).all()
    columns_by_id = {c.id: {'id': c.id, 'name': c.name} for c in columns}
    return render_template(
        'shared_project.html',
        project=_project_to_dict(project),
        columns=[{'id': c.id, 'name': c.name} for c in columns],
        cards=[{**_card_to_dict(card), 'column_name': columns_by_id.get(card.column_id, {}).get('name', '')} for card in cards],
    )


@main_bp.route('/shared/space/<token>', methods=['GET'])
def shared_space(token: str):
    space = WorkspaceSpace.query.filter_by(share_token=sanitize_text(token or '').strip()).first_or_404()
    if bool(space.archived):
        return render_template('shared_space.html', space=_space_to_dict(space), pages=[])
    pages = SpacePage.query.filter_by(space_id=space.id).order_by(SpacePage.updated_at.desc()).all()
    return render_template(
        'shared_space.html',
        space=_space_to_dict(space),
        pages=[{
            'id': p.id,
            'title': p.title,
            'content': p.content,
            'media': _safe_json_load(p.media_json, []),
            'updated_by': p.updated_by or p.created_by,
            'updated_at': (p.updated_at or p.created_at).strftime('%Y-%m-%d %H:%M'),
        } for p in pages],
    )


@main_bp.route('/notes', methods=['POST'])
def notes_post_legacy_redirect():
    content = sanitize_html(request.form.get('content') or '')
    creator = sanitize_text(request.form.get('creator') or '').strip() or _current_member_name()
    title = sanitize_text(request.form.get('title') or '').strip()

    if content:
        note = Note(
            title=title,
            content=content,
            creator=creator,
            note_type='note',
            visibility='family',
            allowed_members_json='[]',
            require_pin=False,
            updated_by=creator,
        )
        db.session.add(note)
        db.session.commit()

    return redirect(url_for('main.notes'))
