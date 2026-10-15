from datetime import date, datetime
import json
import re

import bleach
from flask import render_template, request, redirect, url_for, current_app, jsonify

from ..models import ActivityLog, db, Chore, FamilyMember
from ..blueprints import main_bp
from ..security import sanitize_text
from ..services.notification_service import publish_notification


_DEFAULT_STATUS_OPTIONS = ['todo', 'in_progress', 'completed']


def _normalize_status_token(value) -> str:
    token = sanitize_text(str(value or '')).strip().lower()
    token = re.sub(r'[^a-z0-9]+', '_', token)
    token = re.sub(r'_+', '_', token).strip('_')
    return token or 'todo'


def _parse_json(raw, fallback):
    if isinstance(raw, (list, dict)):
        return raw
    text = str(raw or '').strip()
    if not text:
        return fallback
    try:
        return json.loads(text)
    except Exception:
        return fallback


def _parse_tags(raw_tags) -> list[str]:
    tags = []
    data = _parse_json(raw_tags, raw_tags)
    if isinstance(data, list):
        for item in data:
            val = sanitize_text(str(item or '')).strip()
            if val:
                tags.append(val)
    elif isinstance(data, str):
        for item in data.split(','):
            val = sanitize_text(item).strip()
            if val:
                tags.append(val)
    return sorted(set(tags), key=lambda t: t.lower())


def _family_members() -> list[str]:
    rows = FamilyMember.query.order_by(FamilyMember.name.asc()).all()
    names = [sanitize_text((row.name or '')).strip() for row in rows if sanitize_text((row.name or '')).strip()]
    if not names:
        cfg_members = current_app.config.get('HOMEHUB_CONFIG', {}).get('family_members', [])
        names = [sanitize_text(str(item or '')).strip() for item in cfg_members if sanitize_text(str(item or '')).strip()]
    admin_name = sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator')).strip()
    if admin_name and admin_name not in names:
        names.append(admin_name)
    return sorted(set(names), key=lambda n: n.lower())


def _parse_mentions(description: str, details_html: str, explicit_mentions) -> list[str]:
    members = _family_members()
    member_map = {name.lower(): name for name in members}
    parsed = set()

    for item in _parse_json(explicit_mentions, []):
        clean = sanitize_text(str(item or '')).strip()
        if clean:
            mapped = member_map.get(clean.lower())
            parsed.add(mapped or clean)

    merged_text = f"{description or ''} {bleach.clean(details_html or '', strip=True)}"
    for raw in re.findall(r'@([A-Za-z0-9_.-]{2,64})', merged_text or ''):
        clean = sanitize_text(raw).strip()
        if not clean:
            continue
        mapped = member_map.get(clean.lower())
        parsed.add(mapped or clean)

    return sorted(parsed, key=lambda n: n.lower())


def _sanitize_details_html(value) -> str:
    return bleach.clean(
        str(value or ''),
        tags=[
            'b', 'strong', 'i', 'em', 'u', 's', 'del', 'strike', 'p', 'br', 'ul', 'ol', 'li',
            'blockquote', 'code', 'pre', 'h1', 'h2', 'h3', 'h4', 'a', 'table', 'thead', 'tbody',
            'tr', 'th', 'td', 'input', 'label', 'span', 'div'
        ],
        attributes={
            'a': ['href', 'title', 'target', 'rel'],
            'input': ['type', 'checked', 'disabled'],
            'th': ['colspan', 'rowspan'],
            'td': ['colspan', 'rowspan'],
        },
        protocols=['http', 'https', 'mailto', 'tel'],
        strip=True,
    )


def _normalize_status_options(raw_options, fallback=None) -> list[str]:
    options = []
    data = _parse_json(raw_options, raw_options)
    if isinstance(data, list):
        items = data
    elif isinstance(data, str):
        items = [x.strip() for x in data.split(',') if x.strip()]
    else:
        items = []
    for item in items:
        token = _normalize_status_token(item)
        if token and token not in options:
            options.append(token)

    base = fallback[:] if fallback else _DEFAULT_STATUS_OPTIONS[:]
    for token in base:
        if token not in options:
            options.append(token)
    return options


def _parse_due_date(raw_value):
    text = sanitize_text(str(raw_value or '')).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, '%Y-%m-%d').date()
    except Exception:
        return None


def _parse_checklist(raw_value):
    data = _parse_json(raw_value, [])
    rows = []
    if not isinstance(data, list):
        return rows
    for item in data:
        if isinstance(item, dict):
            text = sanitize_text(str(item.get('text') or '')).strip()
            if not text:
                continue
            rows.append({'text': text, 'done': bool(item.get('done'))})
        elif isinstance(item, str):
            text = sanitize_text(item).strip()
            if text:
                rows.append({'text': text, 'done': False})
    return rows


def _parse_chart_data(raw_value):
    data = _parse_json(raw_value, [])
    rows = []
    if not isinstance(data, list):
        return rows
    for item in data:
        if not isinstance(item, dict):
            continue
        label = sanitize_text(str(item.get('label') or '')).strip()
        if not label:
            continue
        try:
            value = float(item.get('value') or 0)
        except Exception:
            value = 0.0
        rows.append({'label': label, 'value': value})
    return rows


def _parse_table_data(raw_value):
    data = _parse_json(raw_value, {})
    headers = []
    rows = []
    if isinstance(data, dict):
        raw_headers = data.get('headers') or []
        if isinstance(raw_headers, list):
            headers = [sanitize_text(str(h or '')).strip() for h in raw_headers]
            headers = [h for h in headers if h]

        raw_rows = data.get('rows') or []
        if isinstance(raw_rows, list):
            for row in raw_rows:
                if not isinstance(row, list):
                    continue
                cleaned = [sanitize_text(str(c or '')).strip() for c in row]
                rows.append(cleaned)

    return {'headers': headers, 'rows': rows}


def _serialize_task(item: Chore) -> dict:
    tags = _parse_tags(item.tags)
    status_options = _normalize_status_options(getattr(item, 'status_options_json', '[]'))
    status = _normalize_status_token(getattr(item, 'status', 'todo'))
    if status not in status_options:
        status_options.append(status)
    mentions = _parse_json(getattr(item, 'mentions_json', '[]'), [])
    if not isinstance(mentions, list):
        mentions = []
    mentions = [sanitize_text(str(m or '')).strip() for m in mentions if sanitize_text(str(m or '')).strip()]

    checklist = _parse_checklist(getattr(item, 'checklist_json', '[]'))
    chart_data = _parse_chart_data(getattr(item, 'chart_data_json', '[]'))
    table_data = _parse_table_data(getattr(item, 'table_data_json', '{}'))

    return {
        'id': item.id,
        'description': item.description,
        'title': item.description,
        'done': bool(item.done),
        'creator': item.creator,
        'timestamp': item.timestamp.isoformat() if item.timestamp else None,
        'updated_at': item.updated_at.isoformat() if getattr(item, 'updated_at', None) else None,
        'completed_at': item.completed_at.isoformat() if getattr(item, 'completed_at', None) else None,
        'tags': tags,
        'task_type': sanitize_text(getattr(item, 'task_type', 'chore') or 'chore').strip().lower() or 'chore',
        'assignee': sanitize_text(getattr(item, 'assignee', '') or '').strip() or None,
        'due_date': item.due_date.isoformat() if getattr(item, 'due_date', None) else None,
        'status': status,
        'status_options': status_options,
        'mentions': sorted(set(mentions), key=lambda n: n.lower()),
        'details_html': getattr(item, 'details_html', '') or '',
        'checklist': checklist,
        'chart_data': chart_data,
        'table_data': table_data,
    }


def _emit_task_notifications(*, before: dict | None, after: dict, actor: str):
    before_assignee = (before or {}).get('assignee')
    after_assignee = after.get('assignee')

    if after_assignee and after_assignee != before_assignee:
        publish_notification(
            event_type='task_assigned',
            title='New task assigned',
            message=f"{actor or 'A family member'} assigned you: {after.get('description') or 'Task'}",
            target_members=[after_assignee],
            created_by=actor,
            payload={'task_id': after.get('id'), 'status': after.get('status'), 'due_date': after.get('due_date')},
            channels=['in_app', 'mobile'],
        )

    before_mentions = set((before or {}).get('mentions') or [])
    after_mentions = set(after.get('mentions') or [])
    new_mentions = sorted(after_mentions - before_mentions - ({after_assignee} if after_assignee else set()), key=lambda n: n.lower())
    if new_mentions:
        publish_notification(
            event_type='task_mention',
            title='You were mentioned in a task',
            message=f"{actor or 'A family member'} mentioned you in: {after.get('description') or 'Task'}",
            target_members=new_mentions,
            created_by=actor,
            payload={'task_id': after.get('id'), 'status': after.get('status'), 'due_date': after.get('due_date')},
            channels=['in_app', 'mobile'],
        )


def _log_task_timeline_event(*, task_id: int | None, actor: str, action: str, summary: str):
    row = ActivityLog(
        actor=actor or 'Family',
        action=action,
        summary=summary,
        entity_type='task',
        entity_id=task_id,
        visibility='family',
    )
    db.session.add(row)


@main_bp.route('/chores', methods=['GET', 'POST'])
def chores():
    if request.method == 'POST':
        description = sanitize_text(request.form.get('description') or request.form.get('title') or '')
        creator = sanitize_text(request.form.get('creator') or '').strip()
        task_type = sanitize_text(request.form.get('task_type') or 'todo').strip().lower() or 'todo'
        assignee = sanitize_text(request.form.get('assignee') or '').strip() or None
        due_date = _parse_due_date(request.form.get('due_date'))
        details_html = _sanitize_details_html(request.form.get('details_html') or '')
        status = _normalize_status_token(request.form.get('status') or 'todo')
        status_options = _normalize_status_options(request.form.get('status_options') or '', fallback=_DEFAULT_STATUS_OPTIONS)
        if status not in status_options:
            status_options.append(status)
        done = status == 'completed'

        tags_list = _parse_tags(request.form.get('tags', '').strip())
        mentions = _parse_mentions(description, details_html, request.form.get('mentions') or [])
        checklist = _parse_checklist(request.form.get('checklist') or [])
        chart_data = _parse_chart_data(request.form.get('chart_data') or [])
        table_data = _parse_table_data(request.form.get('table_data') or '{}')

        chore = Chore(
            description=description,
            creator=creator,
            tags=json.dumps(tags_list),
            task_type=task_type,
            assignee=assignee,
            due_date=due_date,
            status=status,
            status_options_json=json.dumps(status_options),
            mentions_json=json.dumps(mentions),
            details_html=details_html,
            checklist_json=json.dumps(checklist),
            chart_data_json=json.dumps(chart_data),
            table_data_json=json.dumps(table_data),
            done=done,
            completed_at=datetime.utcnow() if done else None,
            updated_at=datetime.utcnow(),
        )
        db.session.add(chore)
        db.session.flush()
        _log_task_timeline_event(
            task_id=chore.id,
            actor=creator,
            action='chore.created',
            summary=f"created task: {description[:120]}",
        )
        db.session.commit()
        _emit_task_notifications(before=None, after=_serialize_task(chore), actor=creator)
        return redirect(url_for('main.chores'))
    # Filter and sort (unchecked first)
    filter_tags = request.args.get('tags')
    chores = Chore.query.order_by(Chore.done.asc(), Chore.due_date.asc(), Chore.timestamp.desc()).all()
    if filter_tags:
        try:
            selected = json.loads(filter_tags)
            if isinstance(selected, list) and selected:
                def match(item_tags):
                    try:
                        arr = json.loads(item_tags or '[]')
                    except Exception:
                        arr = []
                    return any(t in arr for t in selected)
                chores = [i for i in chores if match(i.tags)]
        except Exception:
            pass
    config = current_app.config['HOMEHUB_CONFIG']
    return render_template('chores.html', chores=chores, config=config, members=_family_members())


@main_bp.route('/chores/toggle/<int:chore_id>', methods=['POST'])
def toggle_chore(chore_id):
    chore = Chore.query.get_or_404(chore_id)
    before = _serialize_task(chore)
    chore.done = not getattr(chore, 'done', False)
    chore.status = 'completed' if chore.done else _normalize_status_token(getattr(chore, 'status', 'todo') or 'todo')
    if not chore.done and chore.status == 'completed':
        chore.status = 'todo'
    chore.completed_at = datetime.utcnow() if chore.done else None
    chore.updated_at = datetime.utcnow()
    db.session.commit()
    _emit_task_notifications(before=before, after=_serialize_task(chore), actor=sanitize_text(chore.creator or '').strip())
    return redirect(url_for('main.chores'))


@main_bp.route('/chores/delete/<int:chore_id>', methods=['POST'])
def delete_chore(chore_id):
    chore = Chore.query.get_or_404(chore_id)
    user = sanitize_text(request.form['user'])
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if user in admin_aliases or user == chore.creator:
        db.session.delete(chore)
        db.session.commit()
    return redirect(url_for('main.chores'))


@main_bp.route('/api/chores/<int:chore_id>/tags', methods=['POST'])
def update_chore_tags(chore_id):
    chore = Chore.query.get_or_404(chore_id)
    try:
        before = _serialize_task(chore)
        data = request.get_json(force=True) or {}
        tags = data.get('tags', [])
        cleaned = _parse_tags(tags)
        chore.tags = json.dumps(cleaned)
        chore.updated_at = datetime.utcnow()
        db.session.commit()
        _emit_task_notifications(before=before, after=_serialize_task(chore), actor=sanitize_text(data.get('actor') or chore.creator or '').strip())
        return jsonify({"ok": True, "id": chore.id, "tags": cleaned})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/chores', methods=['GET'])
def api_get_chores():
    tags = request.args.get('tags')
    status = _normalize_status_token(request.args.get('status') or '') if request.args.get('status') else ''
    assignee = sanitize_text(request.args.get('assignee') or '').strip()
    include_completed = str(request.args.get('include_completed') or '').strip().lower() in {'1', 'true', 'yes'}

    query = Chore.query
    if not include_completed:
        query = query.filter(Chore.done.is_(False))
    if assignee:
        query = query.filter(Chore.assignee == assignee)
    if status:
        query = query.filter(Chore.status == status)

    items = query.order_by(Chore.done.asc(), Chore.due_date.asc(), Chore.timestamp.desc()).all()
    if tags:
        try:
            sel = json.loads(tags)
            if isinstance(sel, list) and sel:
                def match(item_tags):
                    try:
                        arr = json.loads(item_tags or '[]')
                    except Exception:
                        arr = []
                    return any(t in arr for t in sel)
                items = [i for i in items if match(i.tags)]
        except Exception:
            pass
    return jsonify([_serialize_task(i) for i in items])


@main_bp.route('/api/chores', methods=['POST'])
def api_create_chore():
    try:
        data = request.get_json(force=True) or {}
        description = sanitize_text(str(data.get('description', data.get('title', ''))).strip())
        creator = sanitize_text(str(data.get('creator', '')).strip())
        raw_tags = data.get('tags', [])

        if not description:
            return jsonify({'ok': False, 'error': 'Description is required'}), 400
        if not creator:
            return jsonify({'ok': False, 'error': 'Creator is required'}), 400

        tags = _parse_tags(raw_tags)
        task_type = sanitize_text(data.get('task_type') or 'todo').strip().lower() or 'todo'
        assignee = sanitize_text(data.get('assignee') or '').strip() or None
        due_date = _parse_due_date(data.get('due_date'))
        status_options = _normalize_status_options(data.get('status_options') or [], fallback=_DEFAULT_STATUS_OPTIONS)
        status = _normalize_status_token(data.get('status') or 'todo')
        if status not in status_options:
            status_options.append(status)

        details_html = _sanitize_details_html(data.get('details_html') or '')
        mentions = _parse_mentions(description, details_html, data.get('mentions') or [])
        checklist = _parse_checklist(data.get('checklist') or [])
        chart_data = _parse_chart_data(data.get('chart_data') or [])
        table_data = _parse_table_data(data.get('table_data') or {})

        done = bool(data.get('done', False)) or status == 'completed'
        if done:
            status = 'completed'

        chore = Chore(
            description=description,
            creator=creator,
            tags=json.dumps(tags),
            done=done,
            task_type=task_type,
            assignee=assignee,
            due_date=due_date,
            status=status,
            status_options_json=json.dumps(status_options),
            mentions_json=json.dumps(mentions),
            details_html=details_html,
            checklist_json=json.dumps(checklist),
            chart_data_json=json.dumps(chart_data),
            table_data_json=json.dumps(table_data),
            completed_at=datetime.utcnow() if done else None,
            updated_at=datetime.utcnow(),
        )
        db.session.add(chore)
        db.session.flush()
        _log_task_timeline_event(
            task_id=chore.id,
            actor=creator,
            action='chore.created',
            summary=f"created task: {description[:120]}",
        )
        db.session.commit()
        payload = _serialize_task(chore)
        _emit_task_notifications(before=None, after=payload, actor=creator)

        return jsonify({
            'ok': True,
            'item': payload,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@main_bp.route('/api/chores/<int:chore_id>', methods=['PUT'])
def api_update_chore(chore_id):
    chore = Chore.query.get_or_404(chore_id)
    try:
        before = _serialize_task(chore)
        data = request.get_json(force=True) or {}
        desc = data.get('description')
        raw_tags = data.get('tags', None)
        if isinstance(desc, str):
            chore.description = sanitize_text(desc)
        elif isinstance(data.get('title'), str):
            chore.description = sanitize_text(data.get('title'))

        if isinstance(data.get('assignee'), str) or data.get('assignee') is None:
            chore.assignee = sanitize_text(data.get('assignee') or '').strip() or None
        if 'due_date' in data:
            chore.due_date = _parse_due_date(data.get('due_date'))
        if isinstance(data.get('task_type'), str):
            chore.task_type = sanitize_text(data.get('task_type') or 'todo').strip().lower() or 'todo'

        if 'details_html' in data:
            chore.details_html = _sanitize_details_html(data.get('details_html') or '')

        current_options = _normalize_status_options(data.get('status_options') if 'status_options' in data else getattr(chore, 'status_options_json', '[]'))

        if 'status' in data:
            next_status = _normalize_status_token(data.get('status') or 'todo')
            if next_status not in current_options:
                current_options.append(next_status)
            chore.status = next_status

        if 'done' in data:
            chore.done = bool(data.get('done'))

        if chore.done:
            chore.status = 'completed'
            chore.completed_at = chore.completed_at or datetime.utcnow()
        else:
            if _normalize_status_token(getattr(chore, 'status', 'todo')) == 'completed':
                chore.status = 'todo'
            chore.completed_at = None

        if raw_tags is not None:
            tags = _parse_tags(raw_tags)
            chore.tags = json.dumps(tags)

        if 'mentions' in data or 'details_html' in data or 'description' in data or 'title' in data:
            mentions = _parse_mentions(chore.description, chore.details_html or '', data.get('mentions') if 'mentions' in data else _parse_json(getattr(chore, 'mentions_json', '[]'), []))
            chore.mentions_json = json.dumps(mentions)

        if 'checklist' in data:
            chore.checklist_json = json.dumps(_parse_checklist(data.get('checklist')))

        if 'chart_data' in data:
            chore.chart_data_json = json.dumps(_parse_chart_data(data.get('chart_data')))

        if 'table_data' in data:
            chore.table_data_json = json.dumps(_parse_table_data(data.get('table_data')))

        chore.status_options_json = json.dumps(current_options)
        chore.updated_at = datetime.utcnow()
        _log_task_timeline_event(
            task_id=chore.id,
            actor=sanitize_text(data.get('actor') or chore.creator or '').strip(),
            action='chore.updated',
            summary=f"updated task: {chore.description[:120]}",
        )
        db.session.commit()
        payload = _serialize_task(chore)
        _emit_task_notifications(before=before, after=payload, actor=sanitize_text(data.get('actor') or chore.creator or '').strip())
        return jsonify({"ok": True, "item": payload})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/chores/<int:chore_id>/done', methods=['POST'])
def api_set_chore_done(chore_id):
    chore = Chore.query.get_or_404(chore_id)
    try:
        before = _serialize_task(chore)
        data = request.get_json(force=True) or {}
        chore.done = bool(data.get('done'))
        chore.status = 'completed' if chore.done else 'todo'
        chore.completed_at = datetime.utcnow() if chore.done else None
        chore.updated_at = datetime.utcnow()
        _log_task_timeline_event(
            task_id=chore.id,
            actor=sanitize_text(data.get('actor') or chore.creator or '').strip(),
            action='chore.completed' if chore.done else 'chore.reopened',
            summary=(
                f"completed task: {chore.description[:120]}"
                if chore.done
                else f"reopened task: {chore.description[:120]}"
            ),
        )
        db.session.commit()
        payload = _serialize_task(chore)
        _emit_task_notifications(before=before, after=payload, actor=sanitize_text(data.get('actor') or chore.creator or '').strip())
        return jsonify({'ok': True, 'item': payload})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@main_bp.route('/api/chores/<int:chore_id>', methods=['DELETE'])
def api_delete_chore(chore_id):
    chore = Chore.query.get_or_404(chore_id)
    user = sanitize_text((request.args.get('user') or (request.get_json(silent=True) or {}).get('user') or '').strip())
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}

    if user not in admin_aliases and user != chore.creator:
        return jsonify({'ok': False, 'error': 'Permission denied'}), 403

    _log_task_timeline_event(
        task_id=chore.id,
        actor=user,
        action='chore.deleted',
        summary=f"deleted task: {sanitize_text(chore.description or '')[:120]}",
    )
    db.session.delete(chore)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/chores/status-options', methods=['GET'])
def api_chores_status_options():
    options = []
    for row in Chore.query.with_entities(Chore.status_options_json).all():
        raw = row[0] if isinstance(row, tuple) else row
        for token in _normalize_status_options(raw, fallback=[]):
            if token not in options:
                options.append(token)
    for token in _DEFAULT_STATUS_OPTIONS:
        if token not in options:
            options.append(token)
    return jsonify({'ok': True, 'status_options': options})
