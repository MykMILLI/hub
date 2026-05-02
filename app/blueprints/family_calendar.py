from __future__ import annotations

import json
import os
import secrets
from datetime import date, datetime, timedelta
import calendar as _calendar
from urllib.parse import quote

import bleach
from flask import Response, current_app, jsonify, render_template, request, send_from_directory, session, url_for
from werkzeug.utils import secure_filename

from ..blueprints import main_bp
from ..models import FamilyCalendarEvent, FamilyCalendarShareLink, FamilyMember, db
from ..security import sanitize_text
from ..services.notification_service import publish_notification


def _current_member_name() -> str:
    name = sanitize_text(session.get('current_member') or '').strip()
    if name:
        return name
    return sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator'))


def _is_admin_member(name: str) -> bool:
    clean_name = sanitize_text(name or '').strip()
    if not clean_name:
        return False
    cfg_admin = sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator'))
    if clean_name == cfg_admin:
        return True
    member = FamilyMember.query.filter_by(name=clean_name).first()
    return bool(member and (member.role or 'standard') == 'admin')


def _parse_date(value: str | None, fallback: date | None = None) -> date:
    if not value:
        if fallback is not None:
            return fallback
        raise ValueError('date is required')
    return datetime.strptime(value, '%Y-%m-%d').date()


def _parse_optional_datetime(value: str | None):
    if not value:
        return None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(value, fmt)
        except Exception:
            continue
    return None


def _normalize_time_text(value: str | None) -> str | None:
    text = sanitize_text(value or '').strip()
    if not text:
        return None
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.strftime('%H:%M')
        except Exception:
            continue
    return None


def _safe_invited_members(raw_value) -> list[str]:
    values: list[str] = []
    if isinstance(raw_value, list):
        values = [sanitize_text(str(v)).strip() for v in raw_value]
    elif isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            values = []
        else:
            try:
                loaded = json.loads(text)
                if isinstance(loaded, list):
                    values = [sanitize_text(str(v)).strip() for v in loaded]
                else:
                    values = [sanitize_text(x).strip() for x in text.split(',')]
            except Exception:
                values = [sanitize_text(x).strip() for x in text.split(',')]
    return sorted({v for v in values if v})


def _event_to_dict(event: FamilyCalendarEvent) -> dict:
    invited = []
    try:
        invited = json.loads(event.invited_members_json or '[]')
        if not isinstance(invited, list):
            invited = []
    except Exception:
        invited = []

    return {
        'id': event.id,
        'base_event_id': event.id,
        'event_type': event.event_type,
        'title': event.title,
        'description_html': event.description_html or '',
        'color': event.color or '#3b82f6',
        'start_date': event.start_date.isoformat() if event.start_date else None,
        'end_date': event.end_date.isoformat() if event.end_date else None,
        'start_time': sanitize_text(getattr(event, 'start_time', '') or '').strip() or None,
        'end_time': sanitize_text(getattr(event, 'end_time', '') or '').strip() or None,
        'base_start_date': event.start_date.isoformat() if event.start_date else None,
        'base_end_date': event.end_date.isoformat() if event.end_date else None,
        'reminder_at': event.reminder_at.isoformat() if event.reminder_at else None,
        'is_all_day': bool(event.is_all_day),
        'visibility': (event.visibility or 'family'),
        'is_recurring': bool(getattr(event, 'is_recurring', False)),
        'recurrence_frequency': (event.recurrence_frequency or ''),
        'recurrence_interval': int(event.recurrence_interval or 1),
        'recurrence_until': event.recurrence_until.isoformat() if getattr(event, 'recurrence_until', None) else None,
        'recurrence_count': int(event.recurrence_count) if getattr(event, 'recurrence_count', None) else None,
        'occurrence_start': event.start_date.isoformat() if event.start_date else None,
        'occurrence_end': event.end_date.isoformat() if event.end_date else None,
        'created_by': event.created_by,
        'invited_members': invited,
        'photo_url': f"/uploads/calendar/{event.photo_filename}" if event.photo_filename else None,
        'created_at': event.created_at.isoformat() if event.created_at else None,
        'updated_at': event.updated_at.isoformat() if event.updated_at else None,
    }


def _add_months(base: date, months: int) -> date:
    idx = (base.year * 12 + (base.month - 1)) + months
    yy = idx // 12
    mm = (idx % 12) + 1
    day = min(base.day, _calendar.monthrange(yy, mm)[1])
    return date(yy, mm, day)


def _advance_occurrence(d: date, frequency: str, interval: int) -> date:
    freq = (frequency or 'weekly').lower()
    step = max(1, int(interval or 1))
    if freq == 'daily':
        return d + timedelta(days=step)
    if freq == 'monthly':
        return _add_months(d, step)
    if freq == 'yearly':
        return _add_months(d, step * 12)
    return d + timedelta(weeks=step)


def _expand_events(rows: list[FamilyCalendarEvent], start_date: date, end_date: date) -> list[dict]:
    expanded: list[dict] = []
    for row in rows:
        base_payload = _event_to_dict(row)
        is_recurring = bool(getattr(row, 'is_recurring', False)) and bool(row.recurrence_frequency)
        if not is_recurring:
            if row.start_date <= end_date and row.end_date >= start_date:
                expanded.append(base_payload)
            continue

        occ_start = row.start_date
        base_duration = max(0, (row.end_date - row.start_date).days)
        occ_index = 0
        max_iterations = 1200

        while occ_start <= end_date and occ_index < max_iterations:
            if row.recurrence_count and occ_index >= int(row.recurrence_count):
                break
            if row.recurrence_until and occ_start > row.recurrence_until:
                break

            occ_end = occ_start + timedelta(days=base_duration)
            if occ_end >= start_date and occ_start <= end_date:
                payload = dict(base_payload)
                payload['start_date'] = occ_start.isoformat()
                payload['end_date'] = occ_end.isoformat()
                payload['occurrence_start'] = occ_start.isoformat()
                payload['occurrence_end'] = occ_end.isoformat()
                payload['occurrence_index'] = occ_index
                expanded.append(payload)

            occ_index += 1
            occ_start = _advance_occurrence(occ_start, row.recurrence_frequency or 'weekly', int(row.recurrence_interval or 1))

    expanded.sort(key=lambda item: ((item.get('start_date') or ''), (item.get('title') or '')))
    return expanded


def _can_view_event(event: FamilyCalendarEvent, member_name: str) -> bool:
    if _is_admin_member(member_name):
        return True
    visibility = (getattr(event, 'visibility', None) or 'family').strip().lower()
    if visibility == 'family':
        return True
    if visibility == 'private':
        return member_name == event.created_by
    invited = []
    try:
        invited = json.loads(event.invited_members_json or '[]')
        if not isinstance(invited, list):
            invited = []
    except Exception:
        invited = []
    if visibility == 'restricted':
        return member_name == event.created_by or member_name in invited
    if not invited:
        return True
    return member_name == event.created_by or member_name in invited


def _uploads_calendar_dir() -> str:
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    folder = os.path.join(base_dir, 'uploads', 'calendar')
    os.makedirs(folder, exist_ok=True)
    return folder


def _build_sync_links(token: str) -> dict[str, str]:
    root = request.url_root.rstrip('/')
    ics_url = f"{root}{url_for('main.api_calendar_ics_feed', token=token)}"
    webcal_url = ics_url.replace('https://', 'webcal://').replace('http://', 'webcal://')
    encoded_ics = quote(ics_url, safe='')
    encoded_webcal = quote(webcal_url, safe='')
    return {
        'ics_url': ics_url,
        'webcal_url': webcal_url,
        'google_url': f"https://calendar.google.com/calendar/u/0/r?cid={encoded_webcal}",
        'outlook_url': f"https://outlook.live.com/calendar/0/addcalendar?url={encoded_ics}&name=Family%20Calendar",
        'apple_url': webcal_url,
    }


def _ics_escape(value: str | None) -> str:
    raw = str(value or '')
    return raw.replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n').replace('\r', '')


def _format_ics_date(d: date | None) -> str:
    if not d:
        return ''
    return d.strftime('%Y%m%d')


@main_bp.route('/api/calendar/ics/<token>.ics', methods=['GET'])
def api_calendar_ics_feed(token: str):
    token_clean = sanitize_text(token).strip()
    share = FamilyCalendarShareLink.query.filter_by(token=token_clean, active=True).first()
    if not share:
        return Response('Invalid share link', status=404)
    if share.expires_at and share.expires_at < datetime.utcnow():
        return Response('Share link expired', status=410)

    rows = FamilyCalendarEvent.query.order_by(FamilyCalendarEvent.start_date.asc(), FamilyCalendarEvent.created_at.asc()).all()
    feed_start = date.today() - timedelta(days=365)
    feed_end = date.today() + timedelta(days=730)
    expanded = _expand_events(rows, feed_start, feed_end)

    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//HomeHub//Family Calendar//EN',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'X-WR-CALNAME:HomeHub Family Calendar',
    ]
    stamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    for ev in expanded:
        start_d = _parse_date(ev.get('start_date')) if ev.get('start_date') else None
        end_d = _parse_date(ev.get('end_date')) if ev.get('end_date') else start_d
        if not start_d:
            continue
        uid = f"homehub-{ev.get('base_event_id') or ev.get('id')}-{(ev.get('occurrence_start') or ev.get('start_date') or '').replace('-', '')}@homehub"
        lines.extend([
            'BEGIN:VEVENT',
            f"UID:{_ics_escape(uid)}",
            f"DTSTAMP:{stamp}",
            f"SUMMARY:{_ics_escape(ev.get('title') or 'Event')}",
            f"DESCRIPTION:{_ics_escape((ev.get('description_html') or '').replace('<br>', '\\n').replace('<br/>', '\\n').replace('</p>', '\\n'))}",
            f"DTSTART;VALUE=DATE:{_format_ics_date(start_d)}",
            f"DTEND;VALUE=DATE:{_format_ics_date((end_d or start_d) + timedelta(days=1))}",
            'END:VEVENT',
        ])

    lines.append('END:VCALENDAR')
    ics_payload = '\r\n'.join(lines) + '\r\n'
    return Response(
        ics_payload,
        mimetype='text/calendar',
        headers={
            'Content-Disposition': 'inline; filename="homehub-family-calendar.ics"',
            'Cache-Control': 'no-cache',
        },
    )


@main_bp.route('/uploads/calendar/<path:filename>')
def uploaded_calendar_photo(filename: str):
    return send_from_directory(_uploads_calendar_dir(), filename, as_attachment=False)


@main_bp.route('/calendar', methods=['GET'])
def calendar_page():
    config = current_app.config['HOMEHUB_CONFIG']
    return render_template('calendar.html', config=config, shared_mode=False, share_token='')


@main_bp.route('/calendar/share/<token>', methods=['GET'])
def calendar_shared(token: str):
    token_clean = sanitize_text(token).strip()
    share = FamilyCalendarShareLink.query.filter_by(token=token_clean, active=True).first()
    if not share:
        return render_template('calendar.html', config=current_app.config['HOMEHUB_CONFIG'], shared_mode=True, share_token='', invalid_share=True)
    if share.expires_at and share.expires_at < datetime.utcnow():
        return render_template('calendar.html', config=current_app.config['HOMEHUB_CONFIG'], shared_mode=True, share_token='', invalid_share=True)

    # If this is a guest link visit, lock session to shared-calendar-only access.
    if not session.get('authed') and not session.get('current_member'):
        session['shared_guest_mode'] = True
        session['shared_calendar_token'] = token_clean

    return render_template('calendar.html', config=current_app.config['HOMEHUB_CONFIG'], shared_mode=True, share_token=token_clean, invalid_share=False)


@main_bp.route('/api/calendar/members', methods=['GET'])
def api_calendar_members():
    members = FamilyMember.query.order_by(FamilyMember.added_at.asc()).all()
    data = [{'id': m.id, 'name': m.name, 'role': m.role or 'standard', 'avatar': m.avatar} for m in members]
    admin_name = sanitize_text(current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator'))
    if not any(m['name'] == admin_name for m in data):
        data.insert(0, {'id': 0, 'name': admin_name, 'role': 'admin', 'avatar': ''})
    return jsonify({'ok': True, 'members': data, 'self_name': _current_member_name()})


@main_bp.route('/api/calendar/events', methods=['GET'])
def api_calendar_events():
    try:
        start_date = _parse_date(request.args.get('start'))
        end_date = _parse_date(request.args.get('end'))
    except Exception:
        return jsonify({'ok': False, 'error': 'start and end (YYYY-MM-DD) are required'}), 400

    if end_date < start_date:
        start_date, end_date = end_date, start_date

    member_filter = sanitize_text(request.args.get('member') or '').strip()
    current_member = _current_member_name()

    rows = FamilyCalendarEvent.query.order_by(FamilyCalendarEvent.start_date.asc(), FamilyCalendarEvent.created_at.asc()).all()

    visible = [r for r in rows if _can_view_event(r, current_member)]
    if member_filter:
        filtered = []
        for event in visible:
            invited = []
            try:
                invited = json.loads(event.invited_members_json or '[]')
                if not isinstance(invited, list):
                    invited = []
            except Exception:
                invited = []
            if event.created_by == member_filter or member_filter in invited:
                filtered.append(event)
        visible = filtered

    return jsonify({'ok': True, 'events': _expand_events(visible, start_date, end_date)})


@main_bp.route('/api/calendar/shared/<token>/events', methods=['GET'])
def api_calendar_shared_events(token: str):
    token_clean = sanitize_text(token).strip()
    share = FamilyCalendarShareLink.query.filter_by(token=token_clean, active=True).first()
    if not share:
        return jsonify({'ok': False, 'error': 'Invalid share link'}), 404
    if share.expires_at and share.expires_at < datetime.utcnow():
        return jsonify({'ok': False, 'error': 'Share link expired'}), 410

    try:
        start_date = _parse_date(request.args.get('start'))
        end_date = _parse_date(request.args.get('end'))
    except Exception:
        return jsonify({'ok': False, 'error': 'start and end (YYYY-MM-DD) are required'}), 400

    if end_date < start_date:
        start_date, end_date = end_date, start_date

    rows = FamilyCalendarEvent.query.order_by(FamilyCalendarEvent.start_date.asc(), FamilyCalendarEvent.created_at.asc()).all()

    return jsonify({'ok': True, 'events': _expand_events(rows, start_date, end_date)})


@main_bp.route('/api/calendar/events', methods=['POST'])
def api_calendar_create_event():
    actor = _current_member_name()

    data = request.form if request.form else (request.get_json(silent=True) or {})

    title = sanitize_text(data.get('title') or '').strip()
    if not title:
        return jsonify({'ok': False, 'error': 'Title is required'}), 400

    event_type = sanitize_text((data.get('event_type') or 'event')).strip().lower()
    if event_type not in {'event', 'reminder', 'birthday'}:
        event_type = 'event'

    try:
        start_date = _parse_date(data.get('start_date'))
        end_date = _parse_date(data.get('end_date'), fallback=start_date)
    except Exception:
        return jsonify({'ok': False, 'error': 'Valid start date is required'}), 400

    if end_date < start_date:
        end_date = start_date

    color = sanitize_text(data.get('color') or '#3b82f6').strip() or '#3b82f6'
    visibility = sanitize_text((data.get('visibility') or 'family')).strip().lower()
    if visibility not in {'family', 'restricted', 'private'}:
        visibility = 'family'
    reminder_at = _parse_optional_datetime(data.get('reminder_at'))
    is_all_day = str(data.get('is_all_day') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    if 'is_all_day' not in data:
        is_all_day = True
    start_time = _normalize_time_text(data.get('start_time')) if not is_all_day else None
    end_time = _normalize_time_text(data.get('end_time')) if not is_all_day else None
    is_recurring = str(data.get('is_recurring') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    recurrence_frequency = sanitize_text((data.get('recurrence_frequency') or 'weekly')).strip().lower()
    if recurrence_frequency not in {'daily', 'weekly', 'monthly', 'yearly'}:
        recurrence_frequency = 'weekly'
    try:
        recurrence_interval = max(1, int(data.get('recurrence_interval') or 1))
    except Exception:
        recurrence_interval = 1
    recurrence_until = None
    if data.get('recurrence_until'):
        recurrence_until = _parse_date(data.get('recurrence_until'))
    try:
        recurrence_count = int(data.get('recurrence_count')) if data.get('recurrence_count') not in (None, '') else None
    except Exception:
        recurrence_count = None
    if recurrence_count is not None and recurrence_count < 1:
        recurrence_count = None

    invited_members = [n for n in _safe_invited_members(data.get('invited_members')) if n != actor]
    if visibility == 'private':
        invited_members = []
    elif visibility == 'restricted' and not invited_members:
        visibility = 'private'
    description_html = bleach.clean(
        data.get('description_html') or '',
        tags=['b', 'strong', 'i', 'em', 'u', 'p', 'br', 'ul', 'ol', 'li', 'a', 'span'],
        attributes={'a': ['href', 'target', 'rel'], 'span': ['style']},
        strip=True,
    )

    photo_filename = None
    if 'photo' in request.files:
        photo = request.files['photo']
        if photo and photo.filename:
            filename = secure_filename(photo.filename)
            ext = os.path.splitext(filename)[1].lower()
            if ext not in {'.png', '.jpg', '.jpeg', '.gif', '.webp'}:
                return jsonify({'ok': False, 'error': 'Unsupported photo format'}), 400
            stored = f"{secrets.token_hex(8)}_{filename}"
            photo.save(os.path.join(_uploads_calendar_dir(), stored))
            photo_filename = stored

    row = FamilyCalendarEvent(
        event_type=event_type,
        title=title,
        description_html=description_html,
        color=color,
        start_date=start_date,
        end_date=end_date,
        start_time=start_time,
        end_time=end_time,
        reminder_at=reminder_at,
        is_all_day=is_all_day,
        visibility=visibility,
        is_recurring=is_recurring,
        recurrence_frequency=(recurrence_frequency if is_recurring else None),
        recurrence_interval=(recurrence_interval if is_recurring else 1),
        recurrence_until=(recurrence_until if is_recurring else None),
        recurrence_count=(recurrence_count if is_recurring else None),
        created_by=actor,
        invited_members_json=json.dumps(invited_members),
        photo_filename=photo_filename,
    )
    db.session.add(row)
    db.session.commit()

    if visibility == 'family':
        family_members = [m.name for m in FamilyMember.query.order_by(FamilyMember.name.asc()).all()]
        targets = [n for n in family_members if n and n != actor]
    else:
        targets = [n for n in invited_members if n and n != actor]

    if targets:
        publish_notification(
            event_type='calendar_new_event',
            title='New calendar event',
            message=f"{actor} created '{title}' on {start_date.isoformat()}.",
            target_members=targets,
            created_by=actor,
            payload={'calendar_event_id': row.id, 'event_type': event_type, 'start_date': start_date.isoformat()},
            channels=['in_app'],
        )

    return jsonify({'ok': True, 'event': _event_to_dict(row)})


@main_bp.route('/api/calendar/events/<int:event_id>', methods=['PUT'])
def api_calendar_update_event(event_id: int):
    actor = _current_member_name()
    row = FamilyCalendarEvent.query.get_or_404(event_id)

    if not (_is_admin_member(actor) or actor == row.created_by):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    data = request.form if request.form else (request.get_json(silent=True) or {})

    title = sanitize_text(data.get('title') or row.title).strip()
    if not title:
        return jsonify({'ok': False, 'error': 'Title is required'}), 400

    event_type = sanitize_text((data.get('event_type') or row.event_type)).strip().lower()
    if event_type not in {'event', 'reminder', 'birthday'}:
        event_type = row.event_type

    start_date = row.start_date
    end_date = row.end_date
    if data.get('start_date'):
        start_date = _parse_date(data.get('start_date'))
    if data.get('end_date'):
        end_date = _parse_date(data.get('end_date'), fallback=start_date)
    if end_date < start_date:
        end_date = start_date

    reminder_at = _parse_optional_datetime(data.get('reminder_at')) if 'reminder_at' in data else row.reminder_at
    if 'is_all_day' in data:
        is_all_day = str(data.get('is_all_day') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    else:
        is_all_day = bool(getattr(row, 'is_all_day', True))

    start_time = row.start_time
    end_time = row.end_time
    if is_all_day:
        start_time = None
        end_time = None
    else:
        if 'start_time' in data:
            start_time = _normalize_time_text(data.get('start_time'))
        if 'end_time' in data:
            end_time = _normalize_time_text(data.get('end_time'))
    visibility = sanitize_text((data.get('visibility') if 'visibility' in data else (row.visibility or 'family'))).strip().lower()
    if visibility not in {'family', 'restricted', 'private'}:
        visibility = (row.visibility or 'family')
    if 'is_recurring' in data:
        is_recurring = str(data.get('is_recurring') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    else:
        is_recurring = bool(getattr(row, 'is_recurring', False))

    recurrence_frequency = sanitize_text((data.get('recurrence_frequency') or row.recurrence_frequency or 'weekly')).strip().lower()
    if recurrence_frequency not in {'daily', 'weekly', 'monthly', 'yearly'}:
        recurrence_frequency = (row.recurrence_frequency or 'weekly')
    try:
        recurrence_interval = max(1, int(data.get('recurrence_interval') if 'recurrence_interval' in data else (row.recurrence_interval or 1)))
    except Exception:
        recurrence_interval = int(row.recurrence_interval or 1)

    recurrence_until = row.recurrence_until
    if 'recurrence_until' in data:
        recurrence_until = _parse_date(data.get('recurrence_until')) if data.get('recurrence_until') else None

    recurrence_count = row.recurrence_count
    if 'recurrence_count' in data:
        try:
            recurrence_count = int(data.get('recurrence_count')) if data.get('recurrence_count') not in (None, '') else None
        except Exception:
            recurrence_count = row.recurrence_count
        if recurrence_count is not None and recurrence_count < 1:
            recurrence_count = None

    invited_members = _safe_invited_members(data.get('invited_members')) if 'invited_members' in data else _safe_invited_members(row.invited_members_json)
    invited_members = [n for n in invited_members if n != actor]
    if visibility == 'private':
        invited_members = []
    elif visibility == 'restricted' and not invited_members:
        visibility = 'private'

    description_html = row.description_html
    if 'description_html' in data:
        description_html = bleach.clean(
            data.get('description_html') or '',
            tags=['b', 'strong', 'i', 'em', 'u', 'p', 'br', 'ul', 'ol', 'li', 'a', 'span'],
            attributes={'a': ['href', 'target', 'rel'], 'span': ['style']},
            strip=True,
        )

    if 'photo' in request.files:
        photo = request.files['photo']
        if photo and photo.filename:
            filename = secure_filename(photo.filename)
            ext = os.path.splitext(filename)[1].lower()
            if ext not in {'.png', '.jpg', '.jpeg', '.gif', '.webp'}:
                return jsonify({'ok': False, 'error': 'Unsupported photo format'}), 400
            stored = f"{secrets.token_hex(8)}_{filename}"
            photo.save(os.path.join(_uploads_calendar_dir(), stored))
            row.photo_filename = stored

    row.title = title
    row.event_type = event_type
    row.start_date = start_date
    row.end_date = end_date
    row.start_time = start_time
    row.end_time = end_time
    row.color = sanitize_text(data.get('color') or row.color or '#3b82f6').strip() or '#3b82f6'
    row.description_html = description_html
    row.reminder_at = reminder_at
    row.is_all_day = is_all_day
    row.visibility = visibility
    row.is_recurring = is_recurring
    row.recurrence_frequency = recurrence_frequency if is_recurring else None
    row.recurrence_interval = recurrence_interval if is_recurring else 1
    row.recurrence_until = recurrence_until if is_recurring else None
    row.recurrence_count = recurrence_count if is_recurring else None
    row.invited_members_json = json.dumps(invited_members)
    db.session.commit()

    return jsonify({'ok': True, 'event': _event_to_dict(row)})


@main_bp.route('/api/calendar/events/<int:event_id>', methods=['DELETE'])
def api_calendar_delete_event(event_id: int):
    actor = _current_member_name()
    row = FamilyCalendarEvent.query.get_or_404(event_id)
    if not (_is_admin_member(actor) or actor == row.created_by):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True, 'message': 'Event deleted'})


@main_bp.route('/api/calendar/share-link', methods=['POST'])
def api_calendar_share_link_create():
    actor = _current_member_name()
    token = secrets.token_urlsafe(24)

    row = FamilyCalendarShareLink(
        token=token,
        created_by=actor,
        active=True,
    )
    db.session.add(row)
    db.session.commit()

    url = request.url_root.rstrip('/') + url_for('main.calendar_shared', token=token)
    sync = _build_sync_links(token)
    return jsonify({'ok': True, 'token': token, 'url': url, **sync})


@main_bp.route('/api/calendar/share-link', methods=['GET'])
def api_calendar_share_link_get():
    row = (
        FamilyCalendarShareLink.query
        .filter_by(active=True)
        .order_by(FamilyCalendarShareLink.created_at.desc(), FamilyCalendarShareLink.id.desc())
        .first()
    )
    if not row:
        return jsonify({'ok': True, 'url': None, 'token': None, 'ics_url': None, 'webcal_url': None, 'google_url': None, 'outlook_url': None, 'apple_url': None})
    if row.expires_at and row.expires_at < datetime.utcnow():
        return jsonify({'ok': True, 'url': None, 'token': None, 'ics_url': None, 'webcal_url': None, 'google_url': None, 'outlook_url': None, 'apple_url': None})
    url = request.url_root.rstrip('/') + url_for('main.calendar_shared', token=row.token)
    sync = _build_sync_links(row.token)
    return jsonify({'ok': True, 'url': url, 'token': row.token, **sync})
