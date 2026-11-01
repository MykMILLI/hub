from datetime import date, datetime
import json
import re

import bleach
from flask import render_template, request, redirect, url_for, current_app, jsonify

from ..models import ActivityLog, db, Chore, ChoreRewardAward, ChoreWeeklyCompletion, FamilyMember
from ..blueprints import main_bp
from ..security import sanitize_text
from ..services.chore_recurrence import (
    WEEKDAY_LABELS as _WEEKDAY_LABELS,
    day_label as _day_label,
    parse_reference_date as _parse_reference_date,
    resolve_completion_day as _resolve_completion_day,
    resolve_reopen_day as _resolve_reopen_day,
    sunday_week_start as _sunday_week_start,
    weekly_progress as _weekly_progress,
)
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


_DAY_NAMES = {
    'sun': 0, 'sunday': 0, '0': 0,
    'mon': 1, 'monday': 1, '1': 1,
    'tue': 2, 'tues': 2, 'tuesday': 2, '2': 2,
    'wed': 3, 'wednesday': 3, '3': 3,
    'thu': 4, 'thur': 4, 'thurs': 4, 'thursday': 4, '4': 4,
    'fri': 5, 'friday': 5, '5': 5,
    'sat': 6, 'saturday': 6, '6': 6,
}
def _parse_name_list(raw_items, *, fallback=None) -> list[str]:
    items = _parse_json(raw_items, raw_items)
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        items = []
    if not items and fallback:
        items = [fallback]

    members = {name.lower(): name for name in _family_members()}
    cleaned = []
    seen = set()
    for item in items:
        if isinstance(item, (list, tuple, set)):
            nested = item
        else:
            nested = [item]
        for value in nested:
            clean = sanitize_text(str(value or '')).strip()
            if not clean:
                continue
            parts = [part.strip() for part in clean.split(',') if part.strip()]
            for part in parts:
                canonical = members.get(part.lower(), part)
                key = canonical.lower()
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append(canonical)
    return cleaned


def _parse_assignees(raw_assignees, fallback_assignee=None, *, task_type='todo') -> list[str]:
    return _parse_name_list(raw_assignees, fallback=fallback_assignee)


def _parse_days_of_week(raw_days) -> list[str]:
    data = _parse_json(raw_days, raw_days)
    if isinstance(data, str):
        data = [segment.strip() for segment in data.split(',') if segment.strip()]
    if not isinstance(data, list):
        return []

    days = []
    seen = set()
    for item in data:
        value = sanitize_text(str(item or '')).strip().lower()
        if not value:
            continue
        if value.isdigit():
            day_index = int(value)
        else:
            day_index = _DAY_NAMES.get(value.replace('-', '').replace('_', '').replace('.', ''), None)
            if day_index is None and len(value) > 3 and value.endswith('day'):
                day_index = _DAY_NAMES.get(value[:-3], None)
        if day_index is None or day_index not in range(7):
            continue
        if day_index in seen:
            continue
        seen.add(day_index)
        days.append(_WEEKDAY_LABELS[day_index])
    return sorted(days, key=lambda label: _WEEKDAY_LABELS.index(label))


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
            note = sanitize_text(str(item.get('note') or '')).strip()[:500]
            rows.append({'text': text, 'done': bool(item.get('done')), 'note': note})
        elif isinstance(item, str):
            text = sanitize_text(item).strip()
            if text:
                rows.append({'text': text, 'done': False, 'note': ''})
    return rows


_DEFAULT_CHECKLIST_META = {'item_label': 'step', 'item_label_plural': 'steps', 'progress_verb': ''}


def _pluralize(word: str) -> str:
    lowered = word.lower()
    if lowered.endswith('y') and len(word) > 1 and lowered[-2] not in 'aeiou':
        return word[:-1] + 'ies'
    if lowered.endswith(('s', 'x', 'z', 'ch', 'sh')):
        return word + 'es'
    return word + 's'


def _singularize(word: str) -> str:
    lowered = word.lower()
    if lowered.endswith('ies') and len(word) > 3:
        return word[:-3] + 'y'
    if lowered.endswith('es') and lowered[:-2].endswith(('ss', 'x', 'z', 'ch', 'sh')):
        return word[:-2]
    if lowered.endswith('s') and len(word) > 1:
        return word[:-1]
    return word


def _parse_checklist_meta(raw_value):
    """Per-task checklist vocabulary so a list can track houses, books, places, etc."""
    data = _parse_json(raw_value, {})
    meta = dict(_DEFAULT_CHECKLIST_META)
    if not isinstance(data, dict):
        return meta

    singular = sanitize_text(str(data.get('item_label') or '')).strip()[:40]
    plural = sanitize_text(str(data.get('item_label_plural') or '')).strip()[:40]
    verb = sanitize_text(str(data.get('progress_verb') or '')).strip()[:40]

    if not singular and not plural:
        return meta
    if not plural:
        plural = _pluralize(singular)
    if not singular:
        singular = _singularize(plural)

    meta['item_label'] = singular
    meta['item_label_plural'] = plural
    meta['progress_verb'] = verb
    return meta


def _checklist_progress_label(meta: dict, done: int, total: int) -> str:
    noun = (meta.get('item_label_plural') if total != 1 else meta.get('item_label')) or 'steps'
    verb = (meta.get('progress_verb') or '').strip()
    return f"{done} of {total} {noun}{' ' + verb if verb else ''}"


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


def _serialize_task(item: Chore, reference_date: date | None = None) -> dict:
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
    checklist_meta = _parse_checklist_meta(getattr(item, 'checklist_meta_json', '{}'))
    chart_data = _parse_chart_data(getattr(item, 'chart_data_json', '[]'))
    table_data = _parse_table_data(getattr(item, 'table_data_json', '{}'))
    task_type = sanitize_text(getattr(item, 'task_type', 'chore') or 'chore').strip().lower() or 'chore'
    assignees = _parse_assignees(
        getattr(item, 'assignees_json', '[]'),
        getattr(item, 'assignee', None),
        task_type=task_type,
    )
    watchers = _parse_name_list(getattr(item, 'watchers_json', '[]'))
    if not watchers and assignees:
        watchers = assignees[:]
    days_of_week = _parse_days_of_week(getattr(item, 'days_of_week_json', '[]'))
    progress = (
        _weekly_progress(item, days_of_week, reference_date or date.today())
        if task_type == 'chore'
        else None
    )
    effective_done = bool(progress['complete']) if progress else bool(item.done)
    effective_status = 'completed' if effective_done else status
    if progress and not effective_done and effective_status == 'completed':
        effective_status = 'todo'

    return {
        'id': item.id,
        'description': item.description,
        'title': item.description,
        'done': effective_done,
        'creator': item.creator,
        'timestamp': item.timestamp.isoformat() if item.timestamp else None,
        'updated_at': item.updated_at.isoformat() if getattr(item, 'updated_at', None) else None,
        'completed_at': (
            item.completed_at.isoformat()
            if effective_done and getattr(item, 'completed_at', None)
            else (progress.get('last_completed_at') if progress else None)
        ),
        'tags': tags,
        'task_type': task_type,
        'assignee': assignees[0] if assignees else None,
        'assignees': assignees,
        'watchers': watchers,
        'days_of_week': days_of_week,
        'weekly_progress': progress,
        'due_date': item.due_date.isoformat() if getattr(item, 'due_date', None) else None,
        'status': effective_status,
        'status_options': status_options,
        'mentions': sorted(set(mentions), key=lambda n: n.lower()),
        'details_html': getattr(item, 'details_html', '') or '',
        'checklist': checklist,
        'checklist_meta': checklist_meta,
        'checklist_progress_label': _checklist_progress_label(
            checklist_meta,
            sum(1 for row in checklist if row.get('done')),
            len(checklist),
        ) if checklist else '',
        'chart_data': chart_data,
        'table_data': table_data,
        'reward': sanitize_text(getattr(item, 'reward', '') or '').strip(),
    }


def _task_recipients(task: dict | None) -> list[str]:
    assignees = set((task or {}).get('assignees') or ([(task or {}).get('assignee')] if (task or {}).get('assignee') else []))
    watchers = set((task or {}).get('watchers') or [])
    merged = sorted(assignees | watchers, key=lambda name: name.lower())
    return [name for name in merged if name]


def _emit_task_notifications(*, before: dict | None, after: dict, actor: str):
    before_assignees = set((before or {}).get('assignees') or ([before.get('assignee')] if before and before.get('assignee') else []))
    after_assignees = set(after.get('assignees') or ([after.get('assignee')] if after.get('assignee') else []))
    new_assignees = sorted(after_assignees - before_assignees, key=lambda name: name.lower())

    if new_assignees:
        publish_notification(
            event_type='task_assigned',
            title='New task assigned',
            message=f"{actor or 'A family member'} assigned you: {after.get('description') or 'Task'}",
            target_members=new_assignees,
            created_by=actor,
            payload={'task_id': after.get('id'), 'status': after.get('status'), 'due_date': after.get('due_date')},
            channels=['in_app', 'mobile'],
        )

    before_mentions = set((before or {}).get('mentions') or [])
    after_mentions = set(after.get('mentions') or [])
    new_mentions = sorted(after_mentions - before_mentions - after_assignees, key=lambda n: n.lower())
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

    if before is None:
        return

    before_done = bool((before or {}).get('done'))
    after_done = bool(after.get('done'))
    if before_done == after_done or not after_done:
        return

    recipients = [name for name in _task_recipients(after) if name and name.lower() != (actor or '').lower()]
    if recipients:
        item_label = 'Chore' if after.get('task_type') == 'chore' else 'Task'
        publish_notification(
            event_type='task_completed',
            title=f'{item_label} completed',
            message=f"{actor or 'A family member'} marked this as done: {after.get('description') or 'Task'}",
            target_members=recipients,
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
        assignees = _parse_assignees(request.form.get('assignees'), request.form.get('assignee'), task_type=task_type)
        assignee = assignees[0] if assignees else None
        watchers = _parse_name_list(request.form.get('watchers') or request.form.get('watcher') or [], fallback=assignee)
        days_of_week = _parse_days_of_week(request.form.get('days_of_week') or request.form.get('days') or [])
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
        checklist_meta = _parse_checklist_meta(request.form.get('checklist_meta') or '{}')
        chart_data = _parse_chart_data(request.form.get('chart_data') or [])
        table_data = _parse_table_data(request.form.get('table_data') or '{}')

        chore = Chore(
            description=description,
            creator=creator,
            tags=json.dumps(tags_list),
            task_type=task_type,
            assignee=assignee,
            assignees_json=json.dumps(assignees),
            watchers_json=json.dumps(watchers),
            days_of_week_json=json.dumps(days_of_week),
            due_date=due_date,
            status=status,
            status_options_json=json.dumps(status_options),
            mentions_json=json.dumps(mentions),
            details_html=details_html,
            checklist_json=json.dumps(checklist),
            checklist_meta_json=json.dumps(checklist_meta),
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
    reference_date = _parse_reference_date(request.args.get('as_of'))

    items = Chore.query.order_by(Chore.done.asc(), Chore.due_date.asc(), Chore.timestamp.desc()).all()
    serialized = [_serialize_task(item, reference_date) for item in items]
    if not include_completed:
        serialized = [item for item in serialized if not item['done']]
    if status:
        serialized = [item for item in serialized if item['status'] == status]
    if assignee:
        serialized = [
            item for item in serialized
            if any(name.lower() == assignee.lower() for name in item['assignees'])
        ]
    if tags:
        try:
            sel = json.loads(tags)
            if isinstance(sel, list) and sel:
                serialized = [
                    item for item in serialized
                    if any(tag in item['tags'] for tag in sel)
                ]
        except Exception:
            pass
    return jsonify(serialized)


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
        assignees = _parse_assignees(data.get('assignees'), data.get('assignee'), task_type=task_type)
        assignee = assignees[0] if assignees else None
        watchers = _parse_name_list(data.get('watchers') or data.get('watcher') or [], fallback=assignee)
        days_of_week = _parse_days_of_week(data.get('days_of_week') or data.get('days') or data.get('weekdays') or [])
        due_date = _parse_due_date(data.get('due_date'))
        status_options = _normalize_status_options(data.get('status_options') or [], fallback=_DEFAULT_STATUS_OPTIONS)
        status = _normalize_status_token(data.get('status') or 'todo')
        if status not in status_options:
            status_options.append(status)

        details_html = _sanitize_details_html(data.get('details_html') or '')
        mentions = _parse_mentions(description, details_html, data.get('mentions') or [])
        checklist = _parse_checklist(data.get('checklist') or [])
        checklist_meta = _parse_checklist_meta(data.get('checklist_meta') or {})
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
            assignees_json=json.dumps(assignees),
            watchers_json=json.dumps(watchers),
            days_of_week_json=json.dumps(days_of_week),
            due_date=due_date,
            status=status,
            status_options_json=json.dumps(status_options),
            mentions_json=json.dumps(mentions),
            details_html=details_html,
            checklist_json=json.dumps(checklist),
            checklist_meta_json=json.dumps(checklist_meta),
            chart_data_json=json.dumps(chart_data),
            table_data_json=json.dumps(table_data),
            completed_at=datetime.utcnow() if done else None,
            reward=sanitize_text(str(data.get('reward') or '')).strip()[:200] or None,
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

        if 'due_date' in data:
            chore.due_date = _parse_due_date(data.get('due_date'))
        if isinstance(data.get('task_type'), str):
            chore.task_type = sanitize_text(data.get('task_type') or 'todo').strip().lower() or 'todo'
        if 'assignees' in data or 'assignee' in data or 'task_type' in data:
            existing_assignees = _parse_json(getattr(chore, 'assignees_json', '[]'), [])
            raw_assignees = data.get('assignees') if 'assignees' in data else existing_assignees
            fallback_assignee = data.get('assignee') if 'assignee' in data else chore.assignee
            assignees = _parse_assignees(raw_assignees, fallback_assignee, task_type=chore.task_type)
            chore.assignee = assignees[0] if assignees else None
            chore.assignees_json = json.dumps(assignees)

        if 'watchers' in data or 'watcher' in data:
            chore.watchers_json = json.dumps(_parse_name_list(data.get('watchers') or data.get('watcher') or [], fallback=chore.assignee))

        if 'days_of_week' in data or 'days' in data or 'weekdays' in data:
            chore.days_of_week_json = json.dumps(_parse_days_of_week(data.get('days_of_week') or data.get('days') or data.get('weekdays') or []))

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

        if 'checklist_meta' in data:
            chore.checklist_meta_json = json.dumps(_parse_checklist_meta(data.get('checklist_meta')))

        if 'chart_data' in data:
            chore.chart_data_json = json.dumps(_parse_chart_data(data.get('chart_data')))

        if 'table_data' in data:
            chore.table_data_json = json.dumps(_parse_table_data(data.get('table_data')))

        if 'reward' in data:
            chore.reward = sanitize_text(str(data.get('reward') or '')).strip()[:200] or None

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
        data = request.get_json(force=True) or {}
        actor = sanitize_text(data.get('actor') or chore.creator or '').strip()
        requested_done = bool(data.get('done'))
        occurrence_date = _parse_reference_date(data.get('occurrence_date'))
        days_of_week = _parse_days_of_week(getattr(chore, 'days_of_week_json', '[]'))
        before = _serialize_task(chore, occurrence_date)

        if chore.task_type == 'chore' and days_of_week:
            week_start = _sunday_week_start(occurrence_date)
            existing = ChoreWeeklyCompletion.query.filter_by(
                chore_id=chore.id,
                week_start=week_start,
            ).all()
            completed_days = [
                record.scheduled_day for record in existing if record.scheduled_day in days_of_week
            ]
            actual_day = _day_label(occurrence_date)

            if requested_done:
                scheduled_day = _resolve_completion_day(days_of_week, completed_days, occurrence_date)
                if scheduled_day is not None:
                    db.session.add(ChoreWeeklyCompletion(
                        chore_id=chore.id,
                        week_start=week_start,
                        scheduled_day=scheduled_day,
                        completed_by=actor,
                        completed_on=occurrence_date,
                        completed_at=datetime.utcnow(),
                    ))
            else:
                target = _resolve_reopen_day(
                    [record for record in existing if record.scheduled_day in days_of_week],
                    occurrence_date,
                )
                scheduled_day = target.scheduled_day if target is not None else actual_day
                if target is not None:
                    db.session.delete(target)

            db.session.flush()
            after_progress = _weekly_progress(chore, days_of_week, occurrence_date)
            chore.done = bool(after_progress and after_progress['complete'])
            chore.status = 'completed' if chore.done else 'todo'
            chore.completed_at = datetime.utcnow() if chore.done else None
            action = 'chore.occurrence_completed' if requested_done else 'chore.occurrence_reopened'
            early_note = ' (early)' if requested_done and scheduled_day and scheduled_day != actual_day else ''
            summary = (
                f"completed {scheduled_day} occurrence{early_note}: {chore.description[:120]}"
                if requested_done
                else f"reopened {scheduled_day} occurrence: {chore.description[:120]}"
            )
        else:
            chore.done = requested_done
            chore.status = 'completed' if chore.done else 'todo'
            chore.completed_at = datetime.utcnow() if chore.done else None
            action = 'chore.completed' if chore.done else 'chore.reopened'
            summary = (
                f"completed task: {chore.description[:120]}"
                if chore.done
                else f"reopened task: {chore.description[:120]}"
            )

        chore.updated_at = datetime.utcnow()
        award = None
        reward_text = sanitize_text(getattr(chore, 'reward', '') or '').strip()
        if requested_done and reward_text and not before.get('done'):
            award = ChoreRewardAward(
                chore_id=chore.id,
                chore_title=(chore.description or '')[:300],
                member_name=actor,
                reward=reward_text,
            )
            db.session.add(award)
        _log_task_timeline_event(
            task_id=chore.id,
            actor=actor,
            action=action,
            summary=summary,
        )
        db.session.commit()
        payload = _serialize_task(chore, occurrence_date)
        _emit_task_notifications(before=before, after=payload, actor=actor)
        response = {'ok': True, 'item': payload}
        if award:
            response['award'] = {'reward': award.reward, 'member': award.member_name, 'chore': award.chore_title}
        return jsonify(response)
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
