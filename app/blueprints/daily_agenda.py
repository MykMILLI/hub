from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import current_app, jsonify, render_template, request, session

from ..blueprints import main_bp
from ..models import Chore, DailyAgendaEntry, DailyAgendaLocation, FamilyCalendarEvent, FamilyMember, db
from ..permissions import is_admin_name
from ..security import sanitize_text


def _actor() -> str:
    return sanitize_text(session.get('current_member') or current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')).strip()


def _members() -> list[str]:
    names = [sanitize_text(row.name or '').strip() for row in FamilyMember.query.order_by(FamilyMember.name.asc()).all()]
    if not names:
        names = [sanitize_text(str(name or '')).strip() for name in current_app.config['HOMEHUB_CONFIG'].get('family_members', [])]
    admin = sanitize_text(current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')).strip()
    if admin:
        names.append(admin)
    return sorted({name for name in names if name}, key=str.lower)


def _parse_date(value, fallback: date | None = None) -> date:
    text = sanitize_text(str(value or '')).strip()
    if not text and fallback:
        return fallback
    return datetime.strptime(text, '%Y-%m-%d').date()


def _parse_time(value, required: bool = False) -> str | None:
    text = sanitize_text(str(value or '')).strip()
    if not text:
        if required:
            raise ValueError('Start time is required')
        return None
    return datetime.strptime(text, '%H:%M').strftime('%H:%M')


def _entry_payload(row: DailyAgendaEntry) -> dict:
    return {
        'id': row.id,
        'member_name': row.member_name,
        'date': row.agenda_date.isoformat(),
        'start_time': row.start_time,
        'end_time': row.end_time,
        'location_id': row.location_id,
        'location_name': row.location_name or '',
        'location_address': row.location_address or '',
        'location_notes': row.location_notes or '',
        'details': row.details or '',
        'calendar_event_id': row.calendar_event_id,
        'created_by': row.created_by,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


def _location_payload(row: DailyAgendaLocation) -> dict:
    return {
        'id': row.id,
        'name': row.name,
        'address': row.address,
        'notes': row.notes or '',
        'created_by': row.created_by,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


def _entry_location(data, existing: DailyAgendaEntry | None = None) -> tuple[int | None, str, str, str]:
    supplied_id = data.get('location_id')
    if supplied_id not in (None, ''):
        try:
            location = db.session.get(DailyAgendaLocation, int(supplied_id))
        except (TypeError, ValueError):
            location = None
        if not location:
            raise ValueError('Saved location not found')
        return location.id, location.name, location.address, location.notes or ''

    if existing:
        return existing.location_id, existing.location_name or '', existing.location_address or '', existing.location_notes or ''

    return (
        None,
        sanitize_text(str(data.get('location_name') or '')).strip(),
        sanitize_text(str(data.get('location_address') or '')).strip(),
        sanitize_text(str(data.get('location_notes') or '')).strip(),
    )


def _calendar_description(entry: DailyAgendaEntry) -> str:
    lines = []
    if entry.location_address:
        lines.append(entry.location_address)
    if entry.location_notes:
        lines.append(entry.location_notes)
    if entry.details:
        lines.append(entry.details)
    return '<br>'.join(line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') for line in lines)


def _sync_calendar_event(entry: DailyAgendaEntry, actor: str) -> None:
    title_suffix = entry.location_name or entry.details or 'Travel'
    event = db.session.get(FamilyCalendarEvent, entry.calendar_event_id) if entry.calendar_event_id else None
    if not event:
        event = FamilyCalendarEvent(
            created_by=actor,
            title=f'{entry.member_name}: {title_suffix}'[:256],
            start_date=entry.agenda_date,
            end_date=entry.agenda_date,
        )
        db.session.add(event)
        db.session.flush()
        entry.calendar_event_id = event.id
    event.event_type = 'event'
    event.title = f'{entry.member_name}: {title_suffix}'[:256]
    event.description_html = _calendar_description(entry)
    event.color = '#0ea5e9'
    event.start_date = entry.agenda_date
    event.end_date = entry.agenda_date
    event.start_time = entry.start_time
    event.end_time = entry.end_time
    event.is_all_day = False
    event.visibility = 'family'
    event.is_recurring = False
    event.recurrence_frequency = None
    event.recurrence_until = None
    event.recurrence_count = None
    event.invited_members_json = f'["{entry.member_name.replace(chr(34), "")}"]'


def _can_manage(row) -> bool:
    return bool(is_admin_name(_actor()) or row.created_by == _actor())


@main_bp.route('/daily-agenda', methods=['GET'])
def daily_agenda_page():
    return render_template('daily_agenda.html', config=current_app.config['HOMEHUB_CONFIG'])


@main_bp.route('/api/daily-agenda/members', methods=['GET'])
def api_daily_agenda_members():
    return jsonify({'ok': True, 'members': _members(), 'current_member': _actor()})


@main_bp.route('/api/daily-agenda/locations', methods=['GET'])
def api_daily_agenda_locations():
    rows = DailyAgendaLocation.query.order_by(DailyAgendaLocation.name.asc(), DailyAgendaLocation.id.asc()).all()
    return jsonify({'ok': True, 'locations': [_location_payload(row) for row in rows]})


@main_bp.route('/api/daily-agenda/locations', methods=['POST'])
def api_daily_agenda_create_location():
    data = request.get_json(silent=True) or {}
    name = sanitize_text(str(data.get('name') or '')).strip()
    address = sanitize_text(str(data.get('address') or '')).strip()
    if not name or not address:
        return jsonify({'ok': False, 'error': 'Location name and address are required'}), 400
    row = DailyAgendaLocation(name=name[:160], address=address[:512], notes=sanitize_text(str(data.get('notes') or '')).strip(), created_by=_actor())
    db.session.add(row)
    db.session.commit()
    return jsonify({'ok': True, 'location': _location_payload(row)}), 201


@main_bp.route('/api/daily-agenda/locations/<int:location_id>', methods=['PUT'])
def api_daily_agenda_update_location(location_id: int):
    row = DailyAgendaLocation.query.get_or_404(location_id)
    if not _can_manage(row):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    data = request.get_json(silent=True) or {}
    name = sanitize_text(str(data.get('name') or row.name)).strip()
    address = sanitize_text(str(data.get('address') or row.address)).strip()
    if not name or not address:
        return jsonify({'ok': False, 'error': 'Location name and address are required'}), 400
    row.name, row.address = name[:160], address[:512]
    if 'notes' in data:
        row.notes = sanitize_text(str(data.get('notes') or '')).strip()
    db.session.commit()
    return jsonify({'ok': True, 'location': _location_payload(row)})


@main_bp.route('/api/daily-agenda/locations/<int:location_id>', methods=['DELETE'])
def api_daily_agenda_delete_location(location_id: int):
    row = DailyAgendaLocation.query.get_or_404(location_id)
    if not _can_manage(row):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/daily-agenda/entries', methods=['GET'])
def api_daily_agenda_entries():
    try:
        start = _parse_date(request.args.get('start'), date.today())
        end = _parse_date(request.args.get('end'), start + timedelta(days=6))
    except ValueError:
        return jsonify({'ok': False, 'error': 'Dates must use YYYY-MM-DD'}), 400
    if end < start:
        start, end = end, start
    rows = (DailyAgendaEntry.query.filter(DailyAgendaEntry.agenda_date >= start, DailyAgendaEntry.agenda_date <= end)
            .order_by(DailyAgendaEntry.agenda_date.asc(), DailyAgendaEntry.start_time.asc(), DailyAgendaEntry.id.asc()).all())
    return jsonify({'ok': True, 'entries': [_entry_payload(row) for row in rows]})


@main_bp.route('/api/daily-agenda/member-work', methods=['GET'])
def api_daily_agenda_member_work():
    member = sanitize_text(request.args.get('member') or _actor()).strip()
    if member not in _members():
        return jsonify({'ok': False, 'error': 'Select a valid family member'}), 400
    rows = (Chore.query.filter(Chore.done.is_(False))
            .filter(db.func.lower(Chore.assignee) == member.lower())
            .order_by(Chore.due_date.asc(), Chore.updated_at.desc(), Chore.id.desc()).limit(30).all())
    groups = {'todo': [], 'task': [], 'chore': []}
    for row in rows:
        kind = sanitize_text(row.task_type or 'task').strip().lower()
        kind = kind if kind in groups else 'task'
        groups[kind].append({'id': row.id, 'title': sanitize_text(row.description or '').strip() or kind.title(),
                             'due_date': row.due_date.isoformat() if row.due_date else '', 'status': row.status or 'todo'})
    return jsonify({'ok': True, 'member': member, 'work': groups})


@main_bp.route('/api/daily-agenda/entries', methods=['POST'])
def api_daily_agenda_create_entry():
    data = request.get_json(silent=True) or {}
    member = sanitize_text(str(data.get('member_name') or '')).strip()
    if not member or member not in _members():
        return jsonify({'ok': False, 'error': 'Select a valid family member'}), 400
    try:
        agenda_date = _parse_date(data.get('date'))
        start_time = _parse_time(data.get('start_time'), required=True)
        end_time = _parse_time(data.get('end_time'))
        if end_time and end_time <= start_time:
            raise ValueError('End time must be after start time')
        location_id, name, address, notes = _entry_location(data)
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    row = DailyAgendaEntry(member_name=member, agenda_date=agenda_date, start_time=start_time, end_time=end_time,
                           location_id=location_id, location_name=name, location_address=address, location_notes=notes,
                           details=sanitize_text(str(data.get('details') or '')).strip(), created_by=_actor())
    db.session.add(row)
    db.session.flush()
    _sync_calendar_event(row, _actor())
    db.session.commit()
    return jsonify({'ok': True, 'entry': _entry_payload(row)}), 201


@main_bp.route('/api/daily-agenda/entries/<int:entry_id>', methods=['PUT'])
def api_daily_agenda_update_entry(entry_id: int):
    row = DailyAgendaEntry.query.get_or_404(entry_id)
    if not _can_manage(row):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    data = request.get_json(silent=True) or {}
    member = sanitize_text(str(data.get('member_name') or row.member_name)).strip()
    if member not in _members():
        return jsonify({'ok': False, 'error': 'Select a valid family member'}), 400
    try:
        agenda_date = _parse_date(data.get('date'), row.agenda_date)
        start_time = _parse_time(data.get('start_time', row.start_time), required=True)
        end_time = _parse_time(data.get('end_time', row.end_time))
        if end_time and end_time <= start_time:
            raise ValueError('End time must be after start time')
        location_id, name, address, notes = _entry_location(data, row)
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    row.member_name, row.agenda_date, row.start_time, row.end_time = member, agenda_date, start_time, end_time
    row.location_id, row.location_name, row.location_address, row.location_notes = location_id, name, address, notes
    if 'details' in data:
        row.details = sanitize_text(str(data.get('details') or '')).strip()
    _sync_calendar_event(row, _actor())
    db.session.commit()
    return jsonify({'ok': True, 'entry': _entry_payload(row)})


@main_bp.route('/api/daily-agenda/entries/<int:entry_id>', methods=['DELETE'])
def api_daily_agenda_delete_entry(entry_id: int):
    row = DailyAgendaEntry.query.get_or_404(entry_id)
    if not _can_manage(row):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    event = db.session.get(FamilyCalendarEvent, row.calendar_event_id) if row.calendar_event_id else None
    if event:
        db.session.delete(event)
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True})
