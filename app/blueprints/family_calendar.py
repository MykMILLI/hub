from __future__ import annotations

import json
import os
import secrets
import re
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
from datetime import date, datetime, timedelta, timezone
import calendar as _calendar
from urllib.parse import quote

import bleach
from flask import Response, current_app, jsonify, render_template, request, send_from_directory, session, url_for
from werkzeug.utils import secure_filename

from ..blueprints import main_bp
from ..models import AppNotification, FamilyCalendarEvent, FamilyCalendarShareLink, FamilyMember, db
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
    text = sanitize_text(value or '').strip()
    if not text:
        return None
    normalized = text.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.replace(second=0, microsecond=0)
    except Exception:
        pass
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(text, fmt)
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


def _safe_string_list(raw_value) -> list[str]:
    values: list[str] = []
    if isinstance(raw_value, list):
        values = [sanitize_text(str(v)).strip() for v in raw_value]
    elif isinstance(raw_value, str):
        text = raw_value.strip()
        if text:
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
        'reminder_at': (f"{event.reminder_at.replace(second=0, microsecond=0).isoformat()}Z" if event.reminder_at else None),
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


def emit_due_calendar_reminders_for_member(member_name: str) -> int:
    member = sanitize_text(member_name or '').strip()
    if not member:
        return 0

    now_utc = datetime.utcnow()
    # Allow a look-back window so periodic pollers still emit reminders on time.
    window_start = now_utc - timedelta(minutes=25)
    rows = (
        FamilyCalendarEvent.query
        .filter(FamilyCalendarEvent.reminder_at.isnot(None))
        .filter(FamilyCalendarEvent.reminder_at <= now_utc)
        .filter(FamilyCalendarEvent.reminder_at >= window_start)
        .order_by(FamilyCalendarEvent.reminder_at.asc(), FamilyCalendarEvent.id.asc())
        .all()
    )

    sent = 0
    for event in rows:
        if not _can_view_event(event, member):
            continue

        reminder_at = event.reminder_at
        if not reminder_at:
            continue
        reminder_key = f"{event.id}:{reminder_at.replace(second=0, microsecond=0).isoformat()}"

        exists = (
            AppNotification.query
            .filter(AppNotification.event_type == 'calendar_reminder')
            .filter(AppNotification.target_member == member)
            .filter(AppNotification.payload_json.contains(reminder_key))
            .first()
        )
        if exists:
            continue

        when_label = reminder_at.strftime('%Y-%m-%d %H:%M')
        publish_notification(
            event_type='calendar_reminder',
            title='Calendar reminder',
            message=f"{event.title} starts soon ({when_label}).",
            target_members=[member],
            created_by=event.created_by,
            payload={
                'calendar_event_id': event.id,
                'reminder_at': reminder_at.isoformat(),
                'reminder_key': reminder_key,
            },
            channels=['in_app', 'mobile'],
        )
        sent += 1

    return sent


def _public_request_root() -> str:
    configured_root = str((current_app.config.get('HOMEHUB_CONFIG') or {}).get('public_base_url') or '').strip().rstrip('/')
    if configured_root.startswith('http://') or configured_root.startswith('https://'):
        return configured_root

    forwarded_proto = str(request.headers.get('X-Forwarded-Proto') or request.scheme or 'http').split(',')[0].strip()
    forwarded_host = str(request.headers.get('X-Forwarded-Host') or request.host or '').split(',')[0].strip()
    if forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}"
    return request.url_root.rstrip('/')


def _build_sync_links(token: str) -> dict[str, str]:
    root = _public_request_root()
    ics_url = f"{root}{url_for('main.api_calendar_ics_feed', token=token)}"
    ics_download_url = f"{ics_url}?download=1"
    webcal_url = ics_url.replace('https://', 'webcal://').replace('http://', 'webcal://')
    encoded_ics = quote(ics_url, safe='')
    encoded_webcal = quote(webcal_url, safe='')
    return {
        'ics_url': ics_url,
        'ics_download_url': ics_download_url,
        'webcal_url': webcal_url,
        'google_url': f"https://calendar.google.com/calendar/u/0/r?cid={encoded_webcal}",
        'outlook_url': f"https://outlook.live.com/calendar/0/addcalendar?url={encoded_ics}&name=Family%20Calendar",
        'apple_url': webcal_url,
    }


def _share_link_permissions(share: FamilyCalendarShareLink) -> dict:
    return {
        'include_private_events': bool(getattr(share, 'include_private_events', False)),
        'include_descriptions': bool(getattr(share, 'include_descriptions', True)),
    }


def _share_link_filters(share: FamilyCalendarShareLink) -> dict:
    title_keywords = _safe_string_list(getattr(share, 'filter_title_keywords_json', '[]'))
    event_types = [v.lower() for v in _safe_string_list(getattr(share, 'filter_event_types_json', '[]'))]
    event_types = [v for v in event_types if v in {'event', 'reminder', 'birthday'}]
    created_by = _safe_string_list(getattr(share, 'filter_created_by_json', '[]'))
    return {
        'title_keywords': title_keywords,
        'event_types': event_types,
        'created_by': created_by,
    }


def _row_matches_share_filter(row: FamilyCalendarEvent, filters: dict) -> bool:
    title_keywords = filters.get('title_keywords') or []
    event_types = filters.get('event_types') or []
    created_by = filters.get('created_by') or []

    if event_types:
        if (sanitize_text(row.event_type or '').strip().lower()) not in event_types:
            return False

    if created_by:
        if sanitize_text(row.created_by or '').strip() not in created_by:
            return False

    if title_keywords:
        title_lower = sanitize_text(row.title or '').strip().lower()
        if not any(keyword.lower() in title_lower for keyword in title_keywords):
            return False

    return True


def _apply_share_link_filter(rows: list[FamilyCalendarEvent], share: FamilyCalendarShareLink) -> list[FamilyCalendarEvent]:
    filters = _share_link_filters(share)
    if not filters.get('title_keywords') and not filters.get('event_types') and not filters.get('created_by'):
        return rows
    return [row for row in rows if _row_matches_share_filter(row, filters)]


def _apply_share_link_permissions(rows: list[FamilyCalendarEvent], share: FamilyCalendarShareLink) -> list[FamilyCalendarEvent]:
    perms = _share_link_permissions(share)
    if perms.get('include_private_events'):
        return rows
    return [row for row in rows if (row.visibility or 'family') == 'family']


def _share_link_summary(share: FamilyCalendarShareLink) -> dict:
    url = _public_request_root() + url_for('main.calendar_shared', token=share.token)
    sync = _build_sync_links(share.token)
    return {
        'id': share.id,
        'token': share.token,
        'label': sanitize_text(getattr(share, 'label', '') or '').strip() or 'Family Calendar Share',
        'active': bool(share.active),
        'expires_at': share.expires_at.isoformat() if share.expires_at else None,
        'filters': _share_link_filters(share),
        'permissions': _share_link_permissions(share),
        'url': url,
        **sync,
    }


def _ics_escape(value: str | None) -> str:
    raw = str(value or '')
    return raw.replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n').replace('\r', '')


def _format_ics_date(d: date | None) -> str:
    if not d:
        return ''
    return d.strftime('%Y%m%d')


def _format_ics_datetime(d: date, hhmm: str | None) -> str:
    hour = 0
    minute = 0
    try:
        if hhmm:
            parts = str(hhmm).split(':')
            hour = int(parts[0])
            minute = int(parts[1])
    except Exception:
        hour = 0
        minute = 0
    return f"{d.strftime('%Y%m%d')}T{hour:02d}{minute:02d}00"


def _ics_unescape(value: str | None) -> str:
    text = str(value or '')
    text = text.replace('\\n', '\n').replace('\\N', '\n')
    text = text.replace('\\,', ',').replace('\\;', ';').replace('\\\\', '\\')
    return text.strip()


def _strip_html(value: str | None) -> str:
    text = re.sub(r'<br\\s*/?>', '\n', str(value or ''), flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()


def _normalize_source_url(raw: str | None) -> str:
    text = sanitize_text(raw or '').strip()
    if text.startswith('webcal://'):
        return 'https://' + text[len('webcal://'):]
    return text


def _fetch_ics_payload(source_url: str) -> str:
    normalized = _normalize_source_url(source_url)
    if not normalized:
        raise ValueError('Calendar URL is required')
    req = Request(
        normalized,
        headers={
            'User-Agent': 'HomeHub-Calendar-Importer/1.0',
            'Accept': 'text/calendar,text/plain,*/*',
        },
    )
    try:
        with urlopen(req, timeout=20) as response:
            return response.read().decode('utf-8', errors='replace')
    except HTTPError as ex:
        raise ValueError(f'Calendar URL returned HTTP {int(ex.code)}')
    except URLError:
        raise ValueError('Unable to reach the calendar URL')


def _parse_ics_datetime(raw_value: str, is_date_only: bool) -> tuple[date | None, str | None, bool]:
    text = str(raw_value or '').strip()
    if not text:
        return None, None, True

    if is_date_only:
        try:
            d = datetime.strptime(text[:8], '%Y%m%d').date()
            return d, None, True
        except Exception:
            return None, None, True

    work = text.replace('Z', '')
    for fmt in ('%Y%m%dT%H%M%S', '%Y%m%dT%H%M'):
        try:
            dt = datetime.strptime(work[:len(datetime.now().strftime(fmt))], fmt)
            return dt.date(), dt.strftime('%H:%M'), False
        except Exception:
            continue

    # Fallback if upstream emits DATE in a DATE-TIME field.
    try:
        d = datetime.strptime(text[:8], '%Y%m%d').date()
        return d, None, True
    except Exception:
        return None, None, True


def _parse_ics_event(block_lines: list[str]) -> dict | None:
    props: dict[str, tuple[str, dict[str, str]]] = {}
    for line in block_lines:
        if ':' not in line:
            continue
        lhs, rhs = line.split(':', 1)
        parts = lhs.split(';')
        key = parts[0].strip().upper()
        params: dict[str, str] = {}
        for param in parts[1:]:
            if '=' not in param:
                continue
            pkey, pval = param.split('=', 1)
            params[pkey.strip().upper()] = pval.strip()
        props[key] = (rhs.strip(), params)

    summary = _ics_unescape((props.get('SUMMARY') or ('', {}))[0])
    if not summary:
        return None
    description = _ics_unescape((props.get('DESCRIPTION') or ('', {}))[0])
    uid = _ics_unescape((props.get('UID') or ('', {}))[0])

    dtstart_value, dtstart_params = props.get('DTSTART', ('', {}))
    dtend_value, dtend_params = props.get('DTEND', ('', {}))
    if not dtstart_value:
        return None

    start_date, start_time, start_all_day = _parse_ics_datetime(
        dtstart_value,
        (dtstart_params.get('VALUE', '').upper() == 'DATE'),
    )
    if not start_date:
        return None

    end_date = start_date
    end_time = None
    is_all_day = bool(start_all_day)
    if dtend_value:
        parsed_end_date, parsed_end_time, end_is_all_day = _parse_ics_datetime(
            dtend_value,
            (dtend_params.get('VALUE', '').upper() == 'DATE'),
        )
        if parsed_end_date:
            if end_is_all_day:
                # RFC5545 all-day DTEND is exclusive.
                end_date = max(start_date, parsed_end_date - timedelta(days=1))
                end_time = None
                is_all_day = True
            else:
                end_date = parsed_end_date
                end_time = parsed_end_time
                is_all_day = False

    recurrence = {
        'is_recurring': False,
        'frequency': None,
        'interval': 1,
        'until': None,
        'count': None,
    }
    rrule_value = (props.get('RRULE') or ('', {}))[0]
    if rrule_value:
        parts: dict[str, str] = {}
        for token in rrule_value.split(';'):
            if '=' not in token:
                continue
            k, v = token.split('=', 1)
            parts[k.strip().upper()] = v.strip()
        freq = parts.get('FREQ', '').lower()
        if freq in {'daily', 'weekly', 'monthly', 'yearly'}:
            recurrence['is_recurring'] = True
            recurrence['frequency'] = freq
            try:
                recurrence['interval'] = max(1, int(parts.get('INTERVAL') or 1))
            except Exception:
                recurrence['interval'] = 1
            until_raw = parts.get('UNTIL', '')
            if until_raw:
                until_date, _until_time, _until_all_day = _parse_ics_datetime(
                    until_raw,
                    len(until_raw) == 8,
                )
                recurrence['until'] = until_date
            count_raw = parts.get('COUNT', '')
            if count_raw:
                try:
                    recurrence['count'] = max(1, int(count_raw))
                except Exception:
                    recurrence['count'] = None

    return {
        'uid': uid,
        'title': summary[:256],
        'description': description,
        'start_date': start_date,
        'end_date': max(end_date, start_date),
        'start_time': start_time,
        'end_time': end_time,
        'is_all_day': is_all_day,
        'recurrence': recurrence,
    }


def _parse_ics_events(payload: str) -> list[dict]:
    if not payload:
        return []

    unfolded: list[str] = []
    for raw in str(payload).replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if not raw:
            continue
        if unfolded and (raw.startswith(' ') or raw.startswith('\t')):
            unfolded[-1] += raw[1:]
        else:
            unfolded.append(raw)

    events: list[dict] = []
    in_event = False
    current: list[str] = []
    for line in unfolded:
        upper = line.strip().upper()
        if upper == 'BEGIN:VEVENT':
            in_event = True
            current = []
            continue
        if upper == 'END:VEVENT':
            if in_event:
                parsed = _parse_ics_event(current)
                if parsed:
                    events.append(parsed)
            in_event = False
            current = []
            continue
        if in_event:
            current.append(line)

    return events


@main_bp.route('/api/calendar/ics/<token>.ics', methods=['GET'])
def api_calendar_ics_feed(token: str):
    token_clean = sanitize_text(token).strip()
    share = FamilyCalendarShareLink.query.filter_by(token=token_clean, active=True).first()
    if not share:
        return Response('Invalid share link', status=404)
    if share.expires_at and share.expires_at < datetime.utcnow():
        return Response('Share link expired', status=410)

    rows = FamilyCalendarEvent.query.order_by(FamilyCalendarEvent.start_date.asc(), FamilyCalendarEvent.created_at.asc()).all()
    rows = _apply_share_link_permissions(rows, share)
    rows = _apply_share_link_filter(rows, share)
    feed_start = date.today() - timedelta(days=365)
    feed_end = date.today() + timedelta(days=730)
    expanded = _expand_events(rows, feed_start, feed_end)
    permissions = _share_link_permissions(share)

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
        start_time = _normalize_time_text(ev.get('start_time'))
        end_time = _normalize_time_text(ev.get('end_time'))
        is_all_day = bool(ev.get('is_all_day', True))
        uid = f"homehub-{ev.get('base_event_id') or ev.get('id')}-{(ev.get('occurrence_start') or ev.get('start_date') or '').replace('-', '')}@homehub"
        event_lines = [
            'BEGIN:VEVENT',
            f"UID:{_ics_escape(uid)}",
            f"DTSTAMP:{stamp}",
            f"SUMMARY:{_ics_escape(ev.get('title') or 'Event')}",
            f"DESCRIPTION:{_ics_escape(_strip_html(ev.get('description_html') or '') if permissions.get('include_descriptions') else '')}",
        ]

        if is_all_day:
            event_lines.append(f"DTSTART;VALUE=DATE:{_format_ics_date(start_d)}")
            event_lines.append(f"DTEND;VALUE=DATE:{_format_ics_date((end_d or start_d) + timedelta(days=1))}")
        else:
            safe_end_d = end_d or start_d
            if safe_end_d < start_d:
                safe_end_d = start_d
            event_lines.append(f"DTSTART:{_format_ics_datetime(start_d, start_time)}")
            if end_time:
                event_lines.append(f"DTEND:{_format_ics_datetime(safe_end_d, end_time)}")
            else:
                fallback_end_time = start_time or '01:00'
                event_lines.append(f"DTEND:{_format_ics_datetime(safe_end_d, fallback_end_time)}")

        if ev.get('is_recurring') and ev.get('recurrence_frequency'):
            freq = str(ev.get('recurrence_frequency') or '').upper()
            if freq in {'DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY'}:
                interval = max(1, int(ev.get('recurrence_interval') or 1))
                rrule_parts = [f"FREQ={freq}", f"INTERVAL={interval}"]
                rec_until = _parse_date(ev.get('recurrence_until')) if ev.get('recurrence_until') else None
                if rec_until:
                    rrule_parts.append(f"UNTIL={_format_ics_date(rec_until)}")
                rec_count = ev.get('recurrence_count')
                if rec_count:
                    try:
                        rrule_parts.append(f"COUNT={max(1, int(rec_count))}")
                    except Exception:
                        pass
                event_lines.append(f"RRULE:{';'.join(rrule_parts)}")

        event_lines.append('END:VEVENT')
        lines.extend(event_lines)

    lines.append('END:VCALENDAR')
    ics_payload = '\r\n'.join(lines) + '\r\n'
    as_download = str(request.args.get('download') or '').strip().lower() in {'1', 'true', 'yes'}
    disposition = 'attachment' if as_download else 'inline'
    return Response(
        ics_payload,
        mimetype='text/calendar',
        headers={
            'Content-Disposition': f'{disposition}; filename="homehub-family-calendar.ics"',
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

    return render_template('calendar.html', config=current_app.config['HOMEHUB_CONFIG'], shared_mode=True, share_token=token_clean, invalid_share=False)


@main_bp.route('/api/calendar/members', methods=['GET'])
def api_calendar_members():
    members = FamilyMember.query.order_by(FamilyMember.added_at.asc()).all()
    data = [{'id': m.id, 'name': m.name, 'role': m.role or 'standard', 'avatar': m.avatar} for m in members]
    admin_name = sanitize_text(current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator'))
    if not any(m['name'] == admin_name for m in data):
        data.insert(0, {'id': 0, 'name': admin_name, 'role': 'admin', 'avatar': ''})
    return jsonify({'ok': True, 'members': data, 'self_name': _current_member_name()})


def _strip_birthday_prefix(title: str) -> str:
    text = sanitize_text(title or '').strip()
    if text.lower().startswith('birthday:'):
        return text.split(':', 1)[1].strip() or text
    return text


def _today_birthday_entries() -> list[dict]:
    today = date.today()
    rows: list[dict] = []

    for member in FamilyMember.query.order_by(FamilyMember.name.asc()).all():
        if not member.birthday:
            continue
        if member.birthday.month == today.month and member.birthday.day == today.day:
            rows.append({
                'key': f"member:{member.id}:{today.isoformat()}",
                'name': sanitize_text(member.name or '').strip(),
                'date': today.isoformat(),
                'source': 'family_member',
            })

    birthday_events = (
        FamilyCalendarEvent.query
        .filter(FamilyCalendarEvent.event_type == 'birthday')
        .order_by(FamilyCalendarEvent.start_date.asc(), FamilyCalendarEvent.id.asc())
        .all()
    )
    for ev in birthday_events:
        if not ev.start_date:
            continue
        if ev.start_date.month != today.month or ev.start_date.day != today.day:
            continue
        name = _strip_birthday_prefix(ev.title or '') or 'Birthday'
        rows.append({
            'key': f"event:{ev.id}:{today.isoformat()}",
            'name': name,
            'date': today.isoformat(),
            'source': 'calendar_event',
            'event_id': ev.id,
        })

    deduped: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        k = f"{sanitize_text(row.get('name') or '').strip().lower()}::{row.get('date')}"
        if k in seen:
            continue
        seen.add(k)
        deduped.append(row)
    return deduped


def _birthday_opted_in_members() -> list[str]:
    members = FamilyMember.query.order_by(FamilyMember.name.asc()).all()
    targets = [
        sanitize_text(member.name or '').strip()
        for member in members
        if bool(member.birthday_notifications_enabled if member.birthday_notifications_enabled is not None else True)
    ]
    return [name for name in targets if name]


def _emit_birthday_notifications(entries: list[dict]):
    if not entries:
        return
    targets = _birthday_opted_in_members()
    if not targets:
        return

    today = date.today()
    window_start = datetime(today.year, today.month, today.day)
    names = [sanitize_text(item.get('name') or '').strip() for item in entries if sanitize_text(item.get('name') or '').strip()]
    if not names:
        return

    if len(names) == 1:
        title = 'Birthday today'
        message = f"Today is {names[0]}'s birthday."
    else:
        title = 'Birthdays today'
        message = f"Today's birthdays: {', '.join(names)}."

    for member_name in targets:
        existing = (
            AppNotification.query
            .filter(AppNotification.event_type == 'birthday_today')
            .filter(AppNotification.target_member == member_name)
            .filter(AppNotification.created_at >= window_start)
            .order_by(AppNotification.id.desc())
            .first()
        )
        if existing:
            continue
        publish_notification(
            event_type='birthday_today',
            title=title,
            message=message,
            target_members=[member_name],
            created_by='system',
            payload={
                'birthday_date': today.isoformat(),
                'names': names,
            },
            channels=['in_app', 'mobile'],
        )


@main_bp.route('/api/calendar/birthday-optin', methods=['GET'])
def api_calendar_birthday_optin_get():
    actor = _current_member_name()
    row = FamilyMember.query.filter_by(name=actor).first()
    enabled = bool(row.birthday_notifications_enabled if row and row.birthday_notifications_enabled is not None else True)
    return jsonify({'ok': True, 'enabled': enabled, 'member': actor})


@main_bp.route('/api/calendar/birthday-optin', methods=['PUT'])
def api_calendar_birthday_optin_put():
    actor = _current_member_name()
    data = request.get_json(silent=True) or {}
    enabled = str(data.get('enabled') if data.get('enabled') is not None else '').strip().lower() in {'1', 'true', 'yes', 'on'}
    row = FamilyMember.query.filter_by(name=actor).first()
    if not row:
        return jsonify({'ok': False, 'error': 'Current member not found'}), 404
    row.birthday_notifications_enabled = enabled
    db.session.commit()
    return jsonify({'ok': True, 'enabled': bool(row.birthday_notifications_enabled), 'member': actor})


@main_bp.route('/api/calendar/birthdays/list', methods=['GET'])
def api_calendar_birthdays_list():
    members = FamilyMember.query.order_by(FamilyMember.name.asc()).all()
    family_rows = [
        {
            'id': f"member-{m.id}",
            'name': sanitize_text(m.name or '').strip(),
            'date': m.birthday.isoformat() if m.birthday else None,
            'source': 'family_member',
        }
        for m in members if m.birthday
    ]
    custom_rows = [
        {
            'id': f"event-{ev.id}",
            'name': _strip_birthday_prefix(ev.title or ''),
            'date': ev.start_date.isoformat() if ev.start_date else None,
            'source': 'calendar_event',
            'event_id': ev.id,
        }
        for ev in FamilyCalendarEvent.query.filter(FamilyCalendarEvent.event_type == 'birthday').order_by(FamilyCalendarEvent.start_date.asc(), FamilyCalendarEvent.id.asc()).all()
        if ev.start_date
    ]
    rows = [row for row in (family_rows + custom_rows) if row.get('name') and row.get('date')]
    rows.sort(key=lambda item: (str(item.get('date') or ''), str(item.get('name') or '')))
    return jsonify({'ok': True, 'birthdays': rows})


@main_bp.route('/api/calendar/birthdays', methods=['POST'])
def api_calendar_birthdays_create():
    actor = _current_member_name()
    data = request.get_json(silent=True) or {}
    name = sanitize_text(data.get('name') or '').strip()
    relation = sanitize_text(data.get('relation') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Name is required'}), 400
    try:
        birthday = _parse_date(data.get('date'))
    except Exception:
        return jsonify({'ok': False, 'error': 'Valid birthday date is required (YYYY-MM-DD)'}), 400

    notes = sanitize_text(data.get('notes') or '').strip()
    parts = []
    if relation:
        parts.append(f"Relation: {relation}")
    if notes:
        parts.append(notes)
    description_html = '<br>'.join(parts)

    row = FamilyCalendarEvent(
        event_type='birthday',
        title=f"Birthday: {name}",
        description_html=description_html,
        color='#ec4899',
        start_date=birthday,
        end_date=birthday,
        is_all_day=True,
        visibility='family',
        is_recurring=True,
        recurrence_frequency='yearly',
        recurrence_interval=1,
        recurrence_until=None,
        recurrence_count=None,
        created_by=actor,
        invited_members_json='[]',
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({'ok': True, 'birthday': _event_to_dict(row)})


@main_bp.route('/api/calendar/birthdays/today', methods=['GET'])
def api_calendar_birthdays_today():
    entries = _today_birthday_entries()
    _emit_birthday_notifications(entries)
    return jsonify({'ok': True, 'date': date.today().isoformat(), 'birthdays': entries})


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
    rows = _apply_share_link_permissions(rows, share)
    rows = _apply_share_link_filter(rows, share)
    events = _expand_events(rows, start_date, end_date)
    if not _share_link_permissions(share).get('include_descriptions'):
        for item in events:
            item['description_html'] = ''
    return jsonify({'ok': True, 'events': events})


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
    data = request.get_json(silent=True) or {}
    token = secrets.token_urlsafe(24)
    label = sanitize_text(data.get('label') or '').strip() or 'Family Calendar Share'

    filter_title_keywords = _safe_string_list(data.get('filter_title_keywords'))
    filter_event_types = [v.lower() for v in _safe_string_list(data.get('filter_event_types')) if v]
    filter_event_types = [v for v in filter_event_types if v in {'event', 'reminder', 'birthday'}]
    filter_created_by = _safe_string_list(data.get('filter_created_by'))
    include_private_events = str(data.get('include_private_events') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    include_descriptions = str(data.get('include_descriptions') if data.get('include_descriptions') is not None else '1').strip().lower() in {'1', 'true', 'yes', 'on'}

    row = FamilyCalendarShareLink(
        token=token,
        label=label,
        created_by=actor,
        filter_title_keywords_json=json.dumps(filter_title_keywords),
        filter_event_types_json=json.dumps(filter_event_types),
        filter_created_by_json=json.dumps(filter_created_by),
        include_private_events=include_private_events,
        include_descriptions=include_descriptions,
        active=True,
    )
    db.session.add(row)
    db.session.commit()

    return jsonify({'ok': True, **_share_link_summary(row)})


@main_bp.route('/api/calendar/share-link', methods=['GET'])
def api_calendar_share_link_get():
    row = (
        FamilyCalendarShareLink.query
        .filter_by(active=True)
        .order_by(FamilyCalendarShareLink.created_at.desc(), FamilyCalendarShareLink.id.desc())
        .first()
    )
    if not row:
        return jsonify({'ok': True, 'url': None, 'token': None, 'ics_url': None, 'ics_download_url': None, 'webcal_url': None, 'google_url': None, 'outlook_url': None, 'apple_url': None})
    if row.expires_at and row.expires_at < datetime.utcnow():
        return jsonify({'ok': True, 'url': None, 'token': None, 'ics_url': None, 'ics_download_url': None, 'webcal_url': None, 'google_url': None, 'outlook_url': None, 'apple_url': None})
    return jsonify({'ok': True, **_share_link_summary(row)})


@main_bp.route('/api/calendar/share-links', methods=['GET'])
def api_calendar_share_link_list():
    rows = (
        FamilyCalendarShareLink.query
        .filter_by(active=True)
        .order_by(FamilyCalendarShareLink.created_at.desc(), FamilyCalendarShareLink.id.desc())
        .all()
    )
    visible = [row for row in rows if not row.expires_at or row.expires_at >= datetime.utcnow()]
    return jsonify({'ok': True, 'links': [_share_link_summary(row) for row in visible]})


@main_bp.route('/api/calendar/share-link', methods=['PUT'])
def api_calendar_share_link_update():
    target_id = request.args.get('id', type=int)
    if target_id:
        row = FamilyCalendarShareLink.query.filter_by(id=target_id, active=True).first()
    else:
        row = (
            FamilyCalendarShareLink.query
            .filter_by(active=True)
            .order_by(FamilyCalendarShareLink.created_at.desc(), FamilyCalendarShareLink.id.desc())
            .first()
        )
    if not row:
        return jsonify({'ok': False, 'error': 'Create a share link first'}), 404

    data = request.get_json(silent=True) or {}
    if 'label' in data:
        row.label = sanitize_text(data.get('label') or '').strip() or row.label
    row.filter_title_keywords_json = json.dumps(_safe_string_list(data.get('filter_title_keywords')))

    event_types = [v.lower() for v in _safe_string_list(data.get('filter_event_types')) if v]
    event_types = [v for v in event_types if v in {'event', 'reminder', 'birthday'}]
    row.filter_event_types_json = json.dumps(event_types)

    row.filter_created_by_json = json.dumps(_safe_string_list(data.get('filter_created_by')))
    if 'include_private_events' in data:
        row.include_private_events = str(data.get('include_private_events') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    if 'include_descriptions' in data:
        row.include_descriptions = str(data.get('include_descriptions') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    db.session.commit()

    return jsonify({'ok': True, **_share_link_summary(row)})


@main_bp.route('/api/calendar/share-link', methods=['DELETE'])
def api_calendar_share_link_delete():
    target_id = request.args.get('id', type=int)
    if target_id:
        row = FamilyCalendarShareLink.query.filter_by(id=target_id, active=True).first()
    else:
        row = (
            FamilyCalendarShareLink.query
            .filter_by(active=True)
            .order_by(FamilyCalendarShareLink.created_at.desc(), FamilyCalendarShareLink.id.desc())
            .first()
        )
    if not row:
        return jsonify({'ok': False, 'error': 'Share link not found'}), 404

    row.active = False
    db.session.commit()
    return jsonify({'ok': True, 'message': 'Share link deleted', 'id': row.id})


@main_bp.route('/api/calendar/import-ics', methods=['POST'])
def api_calendar_import_ics():
    actor = _current_member_name()
    data = request.get_json(silent=True) or {}

    source_url = sanitize_text(data.get('source_url') or '').strip()
    source_name = sanitize_text(data.get('source_name') or '').strip() or 'External Calendar'
    mode = sanitize_text(data.get('mode') or 'upsert').strip().lower()
    if mode not in {'upsert', 'create-only'}:
        mode = 'upsert'

    payload = str(data.get('ics_content') or '').strip()
    normalized_source = _normalize_source_url(source_url)
    if not payload:
        try:
            payload = _fetch_ics_payload(normalized_source)
        except ValueError as ex:
            return jsonify({'ok': False, 'error': str(ex)}), 400

    events = _parse_ics_events(payload)
    if not events:
        return jsonify({'ok': False, 'error': 'No events were found in the ICS feed'}), 400

    created = 0
    updated = 0
    skipped = 0
    imported_ids: list[int] = []

    for item in events:
        uid_raw = sanitize_text(item.get('uid') or '').strip()
        uid = uid_raw or f"generated-{abs(hash((item.get('title'), item.get('start_date'), item.get('end_date'))))}"

        row = None
        if normalized_source:
            row = FamilyCalendarEvent.query.filter_by(external_uid=uid, external_source_url=normalized_source).first()
        else:
            row = FamilyCalendarEvent.query.filter_by(external_uid=uid).first()

        rec = item.get('recurrence') or {}
        common_kwargs = {
            'event_type': 'event',
            'title': sanitize_text(item.get('title') or '').strip()[:256] or source_name,
            'description_html': bleach.clean(str(item.get('description') or ''), tags=['br'], strip=True).replace('\n', '<br>'),
            'color': '#3b82f6',
            'start_date': item.get('start_date'),
            'end_date': item.get('end_date') or item.get('start_date'),
            'start_time': item.get('start_time'),
            'end_time': item.get('end_time'),
            'is_all_day': bool(item.get('is_all_day', True)),
            'visibility': 'family',
            'is_recurring': bool(rec.get('is_recurring')),
            'recurrence_frequency': rec.get('frequency') if rec.get('is_recurring') else None,
            'recurrence_interval': int(rec.get('interval') or 1),
            'recurrence_until': rec.get('until') if rec.get('is_recurring') else None,
            'recurrence_count': rec.get('count') if rec.get('is_recurring') else None,
            'invited_members_json': '[]',
            'external_uid': uid,
            'external_source_url': normalized_source or None,
        }

        if row:
            if mode == 'create-only':
                skipped += 1
                continue
            for key, val in common_kwargs.items():
                setattr(row, key, val)
            updated += 1
            imported_ids.append(int(row.id))
            continue

        new_row = FamilyCalendarEvent(
            **common_kwargs,
            created_by=actor,
        )
        db.session.add(new_row)
        db.session.flush()
        created += 1
        imported_ids.append(int(new_row.id))

    db.session.commit()
    return jsonify({
        'ok': True,
        'message': f'Imported {created + updated} event(s)',
        'created': created,
        'updated': updated,
        'skipped': skipped,
        'source_name': source_name,
        'source_url': normalized_source,
        'imported_ids': imported_ids,
    })
