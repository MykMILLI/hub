from flask import render_template, request, redirect, url_for, flash, jsonify, current_app, session
from datetime import datetime, date, timedelta, time, timezone
from zoneinfo import ZoneInfo
from ..models import db, HomeStatus, MemberStatus, Notice, Reminder, RecurringReminder, FamilyMember, ActivityLog, FamilyNotice, FamilyNoticeAcknowledgement, HouseholdRoutineState, LibraryBookLoan, PhotoAsset, FamilyCalendarEvent, FamilyChatMessage, Chore, AppNotification
from ..blueprints import main_bp
from ..security import sanitize_html, sanitize_text
from ..services.chore_recurrence import is_effectively_done
from ..services.notification_service import publish_notification
import json

_BATH_NOTIFICATION_MEMBERS = ('Mykal', 'Rachel')


_FINANCE_ACTION_PREFIXES = (
    'money.',
    'expense.',
    'income.',
    'budget.',
    'debt.',
    'shared_expense.',
)
_FINANCE_ENTITY_TYPES = {
    'expense_entry',
    'income_entry',
    'family_budget',
    'budget_category',
    'shared_expense',
    'debt_account',
    'debt_payment',
    'financial_goal',
    'recurring_income',
    'recurring_expense',
    'shared_recurring_bill',
}


def _parse_date_param(value, default=None):
    if not value:
        return default
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except Exception:
        return default


def _get_or_create_household_routine_state(actor: str | None = None) -> HouseholdRoutineState:
    row = HouseholdRoutineState.query.first()
    if row:
        return row
    row = HouseholdRoutineState(
        bedtime_anchor_date=date.today(),
        bedtime_anchor_reader='Rachel',
        bath_mode=0,
        updated_by=sanitize_text(actor or '').strip() or None,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _bedtime_reader_for_date(anchor_date: date, anchor_reader: str, on_date: date) -> str:
    first = sanitize_text(anchor_reader or '').strip() or 'Rachel'
    second = 'Mykal' if first.lower() == 'rachel' else 'Rachel'
    delta_days = (on_date - anchor_date).days
    return first if delta_days % 2 == 0 else second


def _next_bath_date(state: HouseholdRoutineState, today: date) -> date:
    last_completed_date = state.updated_at.date() if state.updated_at else None
    return (last_completed_date + timedelta(days=2)) if last_completed_date else today


def _build_routine_payload(state: HouseholdRoutineState) -> dict:
    bath_mode = int(state.bath_mode or 0) % 2
    if bath_mode == 0:
        bath_assignment = {'avery': 'Avery & Mommy', 'harley': 'Harley & Daddy'}
    else:
        bath_assignment = {'avery': 'Avery & Daddy', 'harley': 'Harley & Mommy'}

    today = date.today()
    bedtime_reader = _bedtime_reader_for_date(
        state.bedtime_anchor_date or today,
        state.bedtime_anchor_reader or 'Rachel',
        today,
    )
    completed_at = None
    if state.updated_at:
        completed_at = state.updated_at.replace(microsecond=0).isoformat() + 'Z'
    next_bath_date = _next_bath_date(state, today)
    overdue_days = max(0, (today - next_bath_date).days)

    return {
        'bedtime': {
            'for_date': today.isoformat(),
            'reader': bedtime_reader,
            'anchor_date': (state.bedtime_anchor_date or today).isoformat(),
            'anchor_reader': state.bedtime_anchor_reader or 'Rachel',
            'alternates_with': 'Mykal' if bedtime_reader == 'Rachel' else 'Rachel',
        },
        'bath': {
            'mode': bath_mode,
            'avery': bath_assignment['avery'],
            'harley': bath_assignment['harley'],
            'completed_by': state.updated_by or '',
            'completed_at': completed_at,
            'next_due_date': next_bath_date.isoformat(),
            'overdue_days': overdue_days,
        }
    }


def emit_due_bedtime_reader_notification_for_member(member_name: str) -> int:
    eastern = ZoneInfo('America/New_York')
    now_utc = datetime.now(timezone.utc)
    now_eastern = now_utc.astimezone(eastern)
    scheduled_utc = datetime.combine(now_eastern.date(), time(20, 15), tzinfo=eastern).astimezone(timezone.utc)
    if not (scheduled_utc <= now_utc <= scheduled_utc + timedelta(minutes=25)):
        return 0
    state = _get_or_create_household_routine_state(member_name)
    reader = _bedtime_reader_for_date(state.bedtime_anchor_date or now_eastern.date(), state.bedtime_anchor_reader or 'Rachel', now_eastern.date())
    if reader.lower() != member_name.lower():
        return 0
    key = f'bedtime-reader:{now_eastern.date().isoformat()}:{reader.lower()}'
    if AppNotification.query.filter(AppNotification.event_type == 'bedtime_reader').filter(AppNotification.target_member == member_name).filter(AppNotification.payload_json.contains(key)).first():
        return 0
    publish_notification(event_type='bedtime_reader', title='Bedtime Reader', message='It is time for your bedtime reading assignment.', target_members=[member_name], created_by='HomeHub', payload={'reminder_key': key}, channels=['in_app', 'mobile'])
    db.session.commit()
    return 1


def emit_due_bath_day_notification() -> int:
    """Notify the bath-day parents at 5:00 PM Eastern whenever a bath is due."""
    eastern = ZoneInfo('America/New_York')
    now_eastern = datetime.now(timezone.utc).astimezone(eastern)
    today_eastern = now_eastern.date()
    if now_eastern.time() < time(17, 0):
        return 0

    state = _get_or_create_household_routine_state('HomeHub')
    if today_eastern < _next_bath_date(state, today_eastern):
        return 0

    payload = _build_routine_payload(state)['bath']
    pairings = ', '.join(
        pairing.replace(' & ', ' with ')
        for pairing in (payload['avery'], payload['harley'])
    )
    key = f'bath-day:{today_eastern.isoformat()}'
    sent = 0
    for member in _BATH_NOTIFICATION_MEMBERS:
        already_sent = (AppNotification.query
                        .filter(AppNotification.event_type == 'bath_day')
                        .filter(AppNotification.target_member == member)
                        .filter(AppNotification.payload_json.contains(key))
                        .first())
        if already_sent:
            continue
        publish_notification(
            event_type='bath_day',
            title='Bath Day',
            message=f'Bath time tonight: {pairings}.',
            target_members=[member],
            created_by='HomeHub',
            payload={
                'reminder_key': key,
                'avery': payload['avery'],
                'harley': payload['harley'],
                'overdue_days': payload['overdue_days'],
            },
            channels=['in_app', 'mobile'],
        )
        sent += 1

    if sent:
        db.session.commit()
    return sent


def _is_finance_timeline_row(row: ActivityLog) -> bool:
    action = sanitize_text(getattr(row, 'action', '') or '').strip().lower()
    entity_type = sanitize_text(getattr(row, 'entity_type', '') or '').strip().lower()
    if any(action.startswith(prefix) for prefix in _FINANCE_ACTION_PREFIXES):
        return True
    if entity_type in _FINANCE_ENTITY_TYPES:
        return True
    return False


def _parse_iso_datetime(value: str | None) -> datetime | None:
    text = sanitize_text(str(value or '')).strip()
    if not text:
        return None
    normalized = text.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if parsed.tzinfo is not None:
        try:
            parsed = parsed.astimezone(tz=None).replace(tzinfo=None)
        except Exception:
            parsed = parsed.replace(tzinfo=None)
    return parsed


def _task_mentions_member(chore: Chore, member_name: str) -> bool:
    name_key = sanitize_text(member_name or '').strip().lower()
    if not name_key:
        return False
    try:
        mentions = json.loads(chore.mentions_json or '[]')
    except Exception:
        mentions = []
    if not isinstance(mentions, list):
        return False
    for item in mentions:
        mention = sanitize_text(str(item or '')).strip().lower()
        if mention == name_key:
            return True
    return False


def _task_assigned_to_member(chore: Chore, member_name: str) -> bool:
    name_key = sanitize_text(member_name or '').strip().lower()
    if not name_key:
        return False
    try:
        assignees = json.loads(getattr(chore, 'assignees_json', '') or '[]')
    except Exception:
        assignees = []
    if not isinstance(assignees, list):
        assignees = []
    legacy_assignee = sanitize_text(getattr(chore, 'assignee', '') or '').strip()
    if legacy_assignee:
        assignees.append(legacy_assignee)
    return any(sanitize_text(str(item or '')).strip().lower() == name_key for item in assignees)


def _coalesce_timestamp(*values):
    for value in values:
        if isinstance(value, datetime):
            return value
    return None


@main_bp.route('/')
def index():
    config = current_app.config['HOMEHUB_CONFIG']
    current_member_name = sanitize_text(session.get('current_member') or '').strip() or sanitize_text(config.get('admin_name', 'Administrator'))
    current_member_row = FamilyMember.query.filter_by(name=current_member_name).first()
    routine_state = _get_or_create_household_routine_state(current_member_name)
    routines = _build_routine_payload(routine_state)
    current_member_status_text = ''
    if current_member_row and (current_member_row.status or '').strip():
        current_member_status_text = current_member_row.status
    assigned_open_tasks = [
        row for row in (
            Chore.query.filter(Chore.done.is_(False))
            .filter(db.func.lower(Chore.task_type) != 'chore')
            .order_by(Chore.due_date.asc(), Chore.updated_at.desc())
            .all()
        )
        if _task_assigned_to_member(row, current_member_name)
    ]
    notice = Notice.query.order_by(Notice.updated_at.desc()).first()
    # Calendar: gather reminders grouped by date
    try:
        rows = Reminder.query.with_entities(
            Reminder.id,
            Reminder.title,
            Reminder.description,
            Reminder.creator,
            Reminder.date,
            Reminder.time,
            Reminder.category,
        ).all()
    except Exception:
        rows = []
    by_date = {}
    for rid, title, description, creator, rdate, rtime, rcat in rows:
        try:
            key = rdate.strftime('%Y-%m-%d')
        except Exception:
            key = str(rdate) if rdate else ''
        by_date.setdefault(key, []).append({
            'id': int(rid),
            'title': title or '',
            'description': description or '',
            'creator': creator or '',
            'time': rtime or None,
            'category': rcat or None,
        })
    # Who is Home summary - get family members from database with their current status
    db_family = FamilyMember.query.all()
    family = [m.name for m in db_family]
    # Get current status for each family member (from new status field)
    family_statuses = {m.name: m.status or 'home' for m in db_family}
    family_status_times = {m.name: m.status_updated_at for m in db_family}
    # Get avatars for each family member
    family_avatars = {m.name: m.avatar for m in db_family}
    family_members_data = {m.name: {'id': m.id, 'avatar': m.avatar, 'status': m.status or 'home'} for m in db_family}
    # Legacy status lookup (for backward compatibility if needed)
    who_statuses = {s.name: s.status for s in HomeStatus.query.all() if s.name in family}
    member_statuses = {ms.name: ms.text for ms in MemberStatus.query.all() if ms.name in family and (ms.text or '').strip()}
    # Extract reminder categories
    reminder_categories = []
    try:
        rcfg = (config.get('reminders') or {}).get('categories') or []
        if isinstance(rcfg, list):
            for entry in rcfg:
                if not isinstance(entry, dict):
                    continue
                key = entry.get('key')
                if not key:
                    continue
                reminder_categories.append({
                    'key': key,
                    'label': entry.get('label') or key,
                    'color': entry.get('color') or None,
                })
    except Exception:
        reminder_categories = []
    # Backward compatibility: provide both Python object and pre-serialized JSON
    try:
        reminders_json = json.dumps(by_date)
    except Exception:
        reminders_json = '{}'
    # Pass Python object; template will use |tojson safely
    timeline_posts = []
    try:
        timeline_rows = (
            ActivityLog.query
            .filter(ActivityLog.visibility == 'family')
            .order_by(ActivityLog.timestamp.desc(), ActivityLog.id.desc())
            .limit(40)
            .all()
        )
        timeline_posts = [row for row in timeline_rows if not _is_finance_timeline_row(row)][:5]
    except Exception:
        timeline_posts = []

    timeline_media_items = []
    try:
        media_rows = (
            PhotoAsset.query
            .filter(PhotoAsset.visibility == 'family')
            .order_by(PhotoAsset.uploaded_at.desc(), PhotoAsset.id.desc())
            .limit(10)
            .all()
        )
        timeline_media_items = [
            {
                'id': row.id,
                'filename': row.filename,
                'original_filename': row.original_filename or row.filename,
                'uploader': row.uploader or '',
                'is_video': bool(row.is_video),
                'preview_url': f"/uploads/photos/preview/{row.filename}",
                'uploaded_at': row.uploaded_at.isoformat() if row.uploaded_at else None,
                'uploaded_at_display': row.uploaded_at.strftime('%b %d, %I:%M %p') if row.uploaded_at else '',
            }
            for row in media_rows
            if (row.filename or '').strip()
        ]
    except Exception:
        timeline_media_items = []

    # Disabled per request: remove photos/videos from Timeline surfaces.
    timeline_media_items = []

    recent_notice_rows = FamilyNotice.query.filter_by(sender=current_member_name).order_by(FamilyNotice.created_at.desc()).limit(5).all()
    family_notice_summary = []
    for n in recent_notice_rows:
        try:
            recipients = json.loads(n.recipients_json or '[]')
            if not isinstance(recipients, list):
                recipients = []
        except Exception:
            recipients = []
        ack_rows = FamilyNoticeAcknowledgement.query.filter_by(notice_id=n.id).all()
        acked = sorted([sanitize_text(a.member_name or '').strip() for a in ack_rows if sanitize_text(a.member_name or '').strip()])
        pending = sorted([r for r in recipients if r and r not in acked])
        family_notice_summary.append({
            'id': n.id,
            'title': n.title,
            'message': n.message,
            'created_at': n.created_at,
            'recipients': recipients,
            'acked': acked,
            'pending': pending,
            'acked_count': len(acked),
            'total_recipients': len(recipients),
        })

    return render_template(
        'index.html',
        config=config,
        notice=notice,
        reminders_data=by_date,
        reminders_json=reminders_json,
        timeline_posts=timeline_posts,
        who_statuses=who_statuses,
        member_statuses=member_statuses,
        reminder_categories=reminder_categories,
        family_members=family,
        family_statuses=family_statuses,
        family_status_times=family_status_times,
        family_avatars=family_avatars,
        family_members_data=family_members_data,
        assigned_open_tasks=assigned_open_tasks,
        current_member_name=current_member_name,
        current_member_status_text=current_member_status_text,
        welcome_modal_pending=bool(session.get('show_welcome_modal')),
        current_member_mini_slideshow_enabled=bool(getattr(current_member_row, 'mini_slideshow_enabled', False)),
        family_notice_summary=family_notice_summary,
        routines=routines,
        timeline_media_items=timeline_media_items,
    )


@main_bp.route('/api/dashboard/missed-updates', methods=['GET'])
def api_dashboard_missed_updates():
    member_name = sanitize_text(session.get('current_member') or '').strip()
    if not member_name:
        return jsonify({'ok': False, 'error': 'Authentication required'}), 401

    member = FamilyMember.query.filter_by(name=member_name).first()
    from_session = _parse_iso_datetime(session.get('welcome_last_seen_at'))
    since_dt = from_session or (member.last_hub_seen_at if member else None) or (datetime.utcnow() - timedelta(days=14))
    since_dt = min(since_dt, datetime.utcnow())

    calendar_rows = (
        FamilyCalendarEvent.query
        .filter(FamilyCalendarEvent.visibility == 'family')
        .filter(FamilyCalendarEvent.created_at >= since_dt)
        .filter(FamilyCalendarEvent.created_by != member_name)
        .order_by(FamilyCalendarEvent.created_at.desc(), FamilyCalendarEvent.id.desc())
        .limit(8)
        .all()
    )

    message_rows = (
        FamilyChatMessage.query
        .filter(FamilyChatMessage.recipient == member_name)
        .filter(FamilyChatMessage.created_at >= since_dt)
        .order_by(FamilyChatMessage.created_at.desc(), FamilyChatMessage.id.desc())
        .limit(8)
        .all()
    )

    chore_candidates = (
        Chore.query
        .filter((Chore.timestamp >= since_dt) | (Chore.updated_at >= since_dt))
        .order_by(Chore.updated_at.desc(), Chore.timestamp.desc(), Chore.id.desc())
        .limit(80)
        .all()
    )
    task_rows = []
    for row in chore_candidates:
        is_assigned = _task_assigned_to_member(row, member_name)
        is_mentioned = _task_mentions_member(row, member_name)
        if not (is_assigned or is_mentioned):
            continue
        if sanitize_text(row.creator or '').strip().lower() == member_name.lower() and not is_mentioned:
            continue
        task_rows.append(row)
        if len(task_rows) >= 8:
            break

    calendar_items = [
        {
            'title': sanitize_text(row.title or 'Calendar event').strip() or 'Calendar event',
            'subtitle': f"Added by {sanitize_text(row.created_by or 'Family').strip() or 'Family'}",
            'timestamp': row.created_at.isoformat() if row.created_at else None,
        }
        for row in calendar_rows
    ]
    message_items = [
        {
            'title': f"New message from {sanitize_text(row.sender or 'Family').strip() or 'Family'}",
            'subtitle': 'Message received',
            'timestamp': row.created_at.isoformat() if row.created_at else None,
        }
        for row in message_rows
    ]
    task_items = [
        {
            'title': sanitize_text(row.description or 'Task update').strip() or 'Task update',
            'subtitle': f"From {sanitize_text(row.creator or 'Family').strip() or 'Family'}",
            'timestamp': _coalesce_timestamp(row.updated_at, row.timestamp).isoformat() if _coalesce_timestamp(row.updated_at, row.timestamp) else None,
        }
        for row in task_rows
        if (sanitize_text(getattr(row, 'task_type', '') or '').strip().lower() != 'chore')
    ]

    # Open chores assigned to this member always show, regardless of when they were created.
    assigned_chores = (
        Chore.query
        .filter(db.func.lower(Chore.task_type) == 'chore')
        .order_by(Chore.due_date.asc(), Chore.id.desc())
        .limit(80)
        .all()
    )
    assigned_chores = [
        row for row in assigned_chores
        if _task_assigned_to_member(row, member_name) and not is_effectively_done(row)
    ][:12]
    chore_items = [
        {
            'title': sanitize_text(row.description or 'Chore').strip() or 'Chore',
            'subtitle': ('Due ' + row.due_date.isoformat()) if row.due_date else 'No due date',
            'timestamp': None,
        }
        for row in assigned_chores
    ]

    today = date.today()
    birthday_items = []
    seen_birthdays = set()
    for m in FamilyMember.query.filter(FamilyMember.birthday.isnot(None)).all():
        if m.birthday.month != today.month:
            continue
        nm = sanitize_text(m.name or '').strip()
        seen_birthdays.add(nm.lower())
        birthday_items.append((m.birthday.day, nm, m.birthday))
    for ev in FamilyCalendarEvent.query.filter(FamilyCalendarEvent.event_type == 'birthday').all():
        if not ev.start_date or ev.start_date.month != today.month:
            continue
        nm = sanitize_text(ev.title or '').strip()
        if nm.lower().startswith('birthday:'):
            nm = nm.split(':', 1)[1].strip()
        if not nm or nm.lower() in seen_birthdays:
            continue
        seen_birthdays.add(nm.lower())
        birthday_items.append((ev.start_date.day, nm, ev.start_date))
    birthday_items.sort(key=lambda t: (t[0], t[1].lower()))
    birthday_items = [
        {
            'title': f"{nm}'s birthday" + (' — today!' if day == today.day else ''),
            'subtitle': bd.strftime('%B %d'),
            'timestamp': None,
        }
        for day, nm, bd in birthday_items
    ]
    birthday_modal_eligible = session.get('welcome_last_birthday_modal_date') != today.isoformat()

    sections = [
        {
            'key': 'birthdays',
            'title': 'Birthdays this month',
            'icon': 'fa-cake-candles',
            'href': '/calendar',
            'count': len(birthday_items) if birthday_modal_eligible else 0,
            'items': birthday_items if birthday_modal_eligible else [],
        },
        {
            'key': 'chores',
            'title': 'Chores assigned to you',
            'icon': 'fa-broom',
            'href': '/chores?tab=chores',
            'count': len(chore_items),
            'items': chore_items,
        },
        {
            'key': 'calendar',
            'title': 'Calendar updates',
            'icon': 'fa-calendar-days',
            'href': '/calendar',
            'count': len(calendar_items),
            'items': calendar_items,
        },
        {
            'key': 'messages',
            'title': 'Messages you missed',
            'icon': 'fa-comments',
            'href': '/messages',
            'count': len(message_items),
            'items': message_items,
        },
        {
            'key': 'tasks',
            'title': 'Task updates for you',
            'icon': 'fa-list-check',
            'href': '/chores?tab=todo',
            'count': len(task_items),
            'items': task_items,
        },
    ]
    sections = [s for s in sections if s['count'] > 0]

    total_updates = sum(section['count'] for section in sections)
    return jsonify({
        'ok': True,
        'should_show': total_updates > 0,
        'since': since_dt.isoformat() if since_dt else None,
        'no_updates': total_updates == 0,
        'total_updates': total_updates,
        'sections': sections,
    })


@main_bp.route('/api/dashboard/missed-updates/ack', methods=['POST'])
def api_dashboard_missed_updates_ack():
    member_name = sanitize_text(session.get('current_member') or '').strip()
    if not member_name:
        return jsonify({'ok': False, 'error': 'Authentication required'}), 401

    now = datetime.utcnow()
    member = FamilyMember.query.filter_by(name=member_name).first()
    if member:
        member.last_hub_seen_at = now
        db.session.add(member)
        db.session.commit()

    session['show_welcome_modal'] = False
    session['welcome_last_seen_at'] = now.isoformat()
    session['welcome_last_birthday_modal_date'] = now.date().isoformat()
    return jsonify({'ok': True, 'seen_at': now.isoformat()})


@main_bp.route('/api/routines/bath/complete', methods=['POST'])
def api_routine_bath_complete():
    actor = sanitize_text(session.get('current_member') or '').strip() or sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator'))
    row = _get_or_create_household_routine_state(actor)
    row.bath_mode = 1 if int(row.bath_mode or 0) == 0 else 0
    row.updated_by = actor
    row.updated_at = datetime.utcnow()
    db.session.add(row)
    db.session.commit()
    return jsonify({'ok': True, 'routines': _build_routine_payload(row)})


@main_bp.route('/api/routines', methods=['GET'])
def api_routine_snapshot():
    actor = sanitize_text(session.get('current_member') or '').strip() or sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator'))
    row = _get_or_create_household_routine_state(actor)
    return jsonify({'ok': True, 'routines': _build_routine_payload(row)})


def _serialize_library_book(row: LibraryBookLoan) -> dict:
    return {
        'id': row.id,
        'child_name': row.child_name,
        'title': row.title,
        'due_date': row.due_date.isoformat() if row.due_date else None,
        'reminder_date': row.reminder_date.isoformat() if row.reminder_date else None,
        'reminder_time': row.reminder_time or '08:00',
        'calendar_event_id': row.calendar_event_id,
    }


def _parse_library_date(value, fallback=None):
    if not value:
        return fallback
    return datetime.strptime(str(value), '%Y-%m-%d').date()


def _sync_library_book_reminder(row: LibraryBookLoan, actor: str) -> None:
    reminder_date = row.reminder_date or (date.today() + timedelta(days=1))
    try:
        reminder_time = datetime.strptime(row.reminder_time or '08:00', '%H:%M').time()
    except ValueError:
        reminder_time = datetime.strptime('08:00', '%H:%M').time()
        row.reminder_time = '08:00'
    event = db.session.get(FamilyCalendarEvent, row.calendar_event_id) if row.calendar_event_id else None
    if not event:
        event = FamilyCalendarEvent(
            created_by=actor,
            title=f'Return library book: {row.title}'[:256],
            start_date=reminder_date,
            end_date=reminder_date,
        )
        db.session.add(event)
        db.session.flush()
        row.calendar_event_id = event.id
    event.event_type = 'reminder'
    event.title = f'Return library book: {row.title}'[:256]
    event.description_html = f'{row.child_name} has this library book checked out. Please return it.'
    event.color = '#8b5cf6'
    event.start_date = reminder_date
    event.end_date = reminder_date
    event.start_time = row.reminder_time
    event.end_time = None
    event.reminder_at = datetime.combine(reminder_date, reminder_time)
    event.is_all_day = False
    event.visibility = 'family'
    event.is_recurring = False
    event.recurrence_frequency = None
    event.recurrence_until = None
    event.recurrence_count = None
    event.invited_members_json = json.dumps([row.child_name])


@main_bp.route('/api/routines/library-books', methods=['GET'])
def api_library_books_list():
    rows = LibraryBookLoan.query.order_by(LibraryBookLoan.reminder_date.asc(), LibraryBookLoan.id.asc()).all()
    return jsonify({'ok': True, 'books': [_serialize_library_book(row) for row in rows]})


@main_bp.route('/api/routines/library-books', methods=['POST'])
def api_library_books_create():
    data = request.get_json(silent=True) or {}
    child_name = sanitize_text(data.get('child_name') or '').strip()
    title = sanitize_text(data.get('title') or '').strip()
    if child_name not in {'Avery', 'Harley'}:
        return jsonify({'ok': False, 'error': 'Select Avery or Harley'}), 400
    if not title:
        return jsonify({'ok': False, 'error': 'Book title is required'}), 400
    try:
        reminder_date = _parse_library_date(data.get('reminder_date'), date.today() + timedelta(days=1))
        due_date = _parse_library_date(data.get('due_date'))
        reminder_time = datetime.strptime(str(data.get('reminder_time') or '08:00'), '%H:%M').strftime('%H:%M')
    except ValueError:
        return jsonify({'ok': False, 'error': 'Use valid dates and a valid reminder time'}), 400
    actor = sanitize_text(session.get('current_member') or '').strip() or 'system'
    row = LibraryBookLoan(child_name=child_name, title=title[:256], due_date=due_date, reminder_date=reminder_date, reminder_time=reminder_time, created_by=actor)
    db.session.add(row)
    db.session.flush()
    _sync_library_book_reminder(row, actor)
    db.session.commit()
    return jsonify({'ok': True, 'book': _serialize_library_book(row)}), 201


@main_bp.route('/api/routines/library-books/<int:book_id>', methods=['DELETE'])
def api_library_books_delete(book_id):
    row = LibraryBookLoan.query.get_or_404(book_id)
    actor = sanitize_text(session.get('current_member') or '').strip()
    if row.created_by != actor and actor != sanitize_text(current_app.config['HOMEHUB_CONFIG'].get('admin_name', '')).strip():
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    event = db.session.get(FamilyCalendarEvent, row.calendar_event_id) if row.calendar_event_id else None
    if event:
        db.session.delete(event)
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True})


def _serialize_reminder(r: Reminder):
    return {
        'id': r.id,
        'date': r.date.strftime('%Y-%m-%d') if r.date else None,
        'time': getattr(r, 'time', None) or None,
        'title': r.title,
        'description': r.description or '',
        'creator': r.creator or '',
        'category': getattr(r, 'category', None),
        'color': getattr(r, 'color', None),
        'recurring_id': getattr(r, 'recurring_id', None),
        'timestamp': r.timestamp.isoformat() if r.timestamp else None,
        'updated_at': getattr(r, 'updated_at', None).isoformat() if getattr(r, 'updated_at', None) else None,
    }

def _serialize_recurring_rule(rr: RecurringReminder):
    interval = getattr(rr, 'interval', None) or 1
    unit = (getattr(rr, 'unit', None) or '').lower()
    if not unit:
        if rr.frequency == 'daily': unit = 'day'
        elif rr.frequency == 'weekly': unit = 'week'
        else: unit = 'month'
    return {
        'id': rr.id,
        'title': rr.title,
        'description': rr.description or '',
        'creator': rr.creator or '',
        'interval': int(interval),
        'unit': unit,
        'time': rr.time,
        'category': rr.category,
        'color': rr.color,
        'start_date': rr.start_date.strftime('%Y-%m-%d') if rr.start_date else None,
        'end_date': rr.end_date.strftime('%Y-%m-%d') if rr.end_date else None,
    }


@main_bp.route('/api/reminders')
def api_reminders_list():
    scope = (request.args.get('scope', 'day') or 'day').lower()
    base_date = _parse_date_param(request.args.get('date'), date.today())
    q = Reminder.query
    if scope == 'month':
        start = base_date.replace(day=1)
        if start.month == 12:
            next_month = start.replace(year=start.year + 1, month=1, day=1)
        else:
            next_month = start.replace(month=start.month + 1, day=1)
        end = next_month - timedelta(days=1)
        q = q.filter(Reminder.date >= start, Reminder.date <= end)
    elif scope == 'week':
        start = base_date - timedelta(days=base_date.weekday())
        end = start + timedelta(days=6)
        q = q.filter(Reminder.date >= start, Reminder.date <= end)
    else:
        q = q.filter(Reminder.date == base_date)
    try:
        from sqlalchemy import case
        rows = q.order_by(
            Reminder.date.asc(),
            case((Reminder.time.is_(None), 1), (Reminder.time == '', 1), else_=0).asc(),
            Reminder.time.asc(),
            Reminder.id.asc(),
        ).all()
    except Exception:
        rows = q.order_by(Reminder.date.asc(), Reminder.id.asc()).all()
    # Generate from recurring rules within scope window (without altering past)
    try:
        rules = RecurringReminder.query.all()
    except Exception:
        rules = []
    gen_rows = []
    rule_dates = {}  # rr.id -> list of dates within window
    if scope == 'month':
        window_start = start
        window_end = end
    elif scope == 'week':
        window_start = start
        window_end = end
    else:
        window_start = base_date
        window_end = base_date
    def add_months(dt: date, months: int) -> date:
        y = dt.year + (dt.month - 1 + months) // 12
        m = (dt.month - 1 + months) % 12 + 1
        # clamp to last day of target month
        last = (date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1) - timedelta(days=1)).day
        d = min(dt.day, last)
        return date(y, m, d)
    def add_years(dt: date, years: int) -> date:
        try:
            return date(dt.year + years, dt.month, dt.day)
        except ValueError:
            # Feb 29 -> Feb 28 fallback
            if dt.month == 2 and dt.day == 29:
                return date(dt.year + years, 2, 28)
            # else clamp to last valid day of month
            return add_months(dt, years * 12)
    def next_date_rule(rr, d):
        # Prefer new interval/unit if present
        interval = getattr(rr, 'interval', None) or 1
        unit = (getattr(rr, 'unit', None) or '').lower() or None
        if not unit:
            # legacy mapping
            if rr.frequency == 'daily':
                unit = 'day'; interval = 1
            elif rr.frequency == 'weekly':
                unit = 'week'; interval = 1
            else:
                unit = 'month'; interval = 1
        if unit == 'day':
            return d + timedelta(days=interval)
        if unit == 'week':
            return d + timedelta(weeks=interval)
        if unit == 'month':
            return add_months(d, interval)
        if unit == 'year':
            return add_years(d, interval)
        # default safety
        return d + timedelta(days=interval)
    for rr in rules:
        rs = rr.start_date or window_start
        d = rs
        # advance d to window_start if needed
        while d < window_start:
            nd = next_date_rule(rr, d)
            if nd == d:
                break
            d = nd
        while d <= window_end and (not rr.end_date or d <= rr.end_date):
            # ensure not already present in DB rows for that date/title
            if not any((r.date == d and r.title == rr.title and r.recurring_id == rr.id) for r in rows):
                temp = Reminder(date=d, title=rr.title, description=rr.description or '', creator=rr.creator or '', time=rr.time, category=rr.category, color=rr.color)
                temp.id = -(1000000 + rr.id)  # ephemeral negative ID
                temp.recurring_id = rr.id
                gen_rows.append(temp)
            rule_dates.setdefault(rr.id, []).append(d)
            d = next_date_rule(rr, d)
    combined = rows + gen_rows
    # Sort combined
    try:
        combined.sort(key=lambda r: (r.date, (r.time is None or r.time == ''), r.time or '', r.id))
    except Exception:
        pass
    data = [_serialize_reminder(r) for r in combined]
    counts = {}
    categories_counts = {}
    if scope == 'month':
        # Include stored and synthesized rows in counts for calendar dots
        for r in (rows + gen_rows):
            k = r.date.strftime('%Y-%m-%d')
            counts[k] = counts.get(k, 0) + 1
            cat = getattr(r, 'category', None) or '_uncategorized'
            if k not in categories_counts:
                categories_counts[k] = {}
            categories_counts[k][cat] = categories_counts[k].get(cat, 0) + 1

    # Build recurring rules summary for UI compression
    recurring_rules = []
    for rr in rules:
        # Determine interval/unit from new fields or legacy frequency
        interval = getattr(rr, 'interval', None) or 1
        unit = (getattr(rr, 'unit', None) or '').lower()
        if not unit:
            if rr.frequency == 'daily': unit = 'day'
            elif rr.frequency == 'weekly': unit = 'week'
            else: unit = 'month'
        recurring_rules.append({
            'id': rr.id,
            'title': rr.title,
            'description': rr.description or '',
            'creator': rr.creator or '',
            'interval': int(interval),
            'unit': unit,
            'time': rr.time,
            'category': rr.category,
            'color': rr.color,
            'end_date': rr.end_date.strftime('%Y-%m-%d') if rr.end_date else None,
            'dates': [d.strftime('%Y-%m-%d') for d in rule_dates.get(rr.id, [])],
        })
    return jsonify({
        'ok': True,
        'scope': scope,
        'date': base_date.strftime('%Y-%m-%d'),
        'reminders': data,
        'counts': counts,
        'categories_counts': categories_counts,
        'recurring_rules': recurring_rules,
    })


@main_bp.route('/api/recurring_rules/<int:rid>', methods=['PATCH', 'DELETE'])
def api_recurring_rules_update_delete(rid):
    rr = RecurringReminder.query.get_or_404(rid)
    if request.method == 'DELETE':
        payload = request.get_json(silent=True) or {}
        user = sanitize_text(payload.get('creator', ''))
        admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
        admin_aliases = {admin_name, 'Administrator', 'admin'}
        if not (user in admin_aliases or user == (rr.creator or '')):
            return jsonify({'ok': False, 'error': 'Not allowed'}), 403
        db.session.delete(rr)
        db.session.commit()
        return jsonify({'ok': True})
    # PATCH
    payload = request.get_json(silent=True) or {}
    user = sanitize_text(payload.get('creator', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if not (user in admin_aliases or user == (rr.creator or '')):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    # Updatable fields
    if 'title' in payload: rr.title = sanitize_text(payload.get('title') or rr.title)
    if 'description' in payload: rr.description = sanitize_html(payload.get('description') or '')
    if 'time' in payload:
        time_raw = payload.get('time')
        tval = None
        if isinstance(time_raw, str) and len(time_raw) == 5 and time_raw[2] == ':':
            hh, mm = time_raw.split(':', 1)
            if hh.isdigit() and mm.isdigit():
                hhi, mmi = int(hh), int(mm)
                if 0 <= hhi < 24 and 0 <= mmi < 60:
                    tval = f"{hhi:02d}:{mmi:02d}"
        rr.time = tval
    if 'category' in payload: rr.category = sanitize_text(payload.get('category')) or None
    if 'color' in payload: rr.color = sanitize_text(payload.get('color')) or None
    # interval/unit/end_date/start_date
    interval = payload.get('interval')
    try:
        interval = int(interval) if interval is not None else None
    except Exception:
        interval = None
    if interval and interval >= 1: rr.interval = interval
    unit = (payload.get('unit') or '').lower()
    if unit in {'day','week','month','year'}: rr.unit = unit
    def _pd(s):
        try:
            return datetime.strptime(s, '%Y-%m-%d').date() if s else None
        except Exception:
            return None
    if 'start_date' in payload:
        sd = _pd(payload.get('start_date'))
        if sd: rr.start_date = sd
    if 'end_date' in payload:
        rr.end_date = _pd(payload.get('end_date'))
    # Do not force effective_from to today; allow full-rule edits including anchor changes
    db.session.commit()
    return jsonify({'ok': True, 'rule': _serialize_recurring_rule(rr)})


@main_bp.route('/api/reminders', methods=['POST'])
def api_reminders_create():
    payload = request.get_json(silent=True) or {}
    title = sanitize_text(payload.get('title', ''))
    creator = sanitize_text(payload.get('creator', ''))
    description = sanitize_html(payload.get('description', ''))
    if not title:
        return jsonify({'ok': False, 'error': 'Title required'}), 400
    d = _parse_date_param(payload.get('date'), None)
    if not d:
        return jsonify({'ok': False, 'error': 'Invalid date'}), 400
    time_raw = payload.get('time')
    tval = None
    if isinstance(time_raw, str) and len(time_raw) == 5 and time_raw[2] == ':':
        hh, mm = time_raw.split(':', 1)
        if hh.isdigit() and mm.isdigit():
            hhi, mmi = int(hh), int(mm)
            if 0 <= hhi < 24 and 0 <= mmi < 60:
                tval = f"{hhi:02d}:{mmi:02d}"
    # Recurring support (optional)
    recur = payload.get('recurring')
    if recur and isinstance(recur, dict):
        # New shape: interval+unit; support legacy 'frequency' for compatibility
        interval = recur.get('interval')
        try:
            interval = int(interval)
        except Exception:
            interval = None
        if not interval or interval < 1:
            interval = 1
        unit = (recur.get('unit') or '').lower()
        if unit not in {'day','week','month','year'}:
            # legacy path
            freq = sanitize_text(recur.get('frequency') or 'daily')
            unit = 'day' if freq == 'daily' else ('week' if freq == 'weekly' else 'month')
        end_s = recur.get('end_date'); end_d = _parse_date_param(end_s, None)
        rr = RecurringReminder(title=title, description=description, creator=creator,
                               interval=interval, unit=unit,
                               frequency=None, monthly_mode=None,
                               time=tval, category=payload.get('category'), color=payload.get('color'),
                               start_date=d, end_date=end_d, effective_from=d)
        db.session.add(rr)
        db.session.commit()
        return jsonify({'ok': True, 'recurring_id': rr.id})
    r = Reminder(date=d, title=title, description=description, creator=creator, time=tval)
    cat = payload.get('category'); col = payload.get('color')
    if hasattr(r, 'category'):
        r.category = sanitize_text(cat) if cat else None
    if hasattr(r, 'color'):
        r.color = sanitize_text(col) if col else None
    db.session.add(r)
    db.session.commit()
    return jsonify({'ok': True, 'reminder': _serialize_reminder(r)})


@main_bp.route('/api/reminders/<int:rid>', methods=['PATCH'])
def api_reminders_update(rid):
    r = Reminder.query.get_or_404(rid)
    payload = request.get_json(silent=True) or {}
    user = sanitize_text(payload.get('creator', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if user not in admin_aliases and user != (r.creator or ''):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    if 'title' in payload:
        title = sanitize_text(payload['title'])
        if title:
            r.title = title
    if 'description' in payload:
        r.description = sanitize_html(payload['description'])
    if 'date' in payload:
        nd = _parse_date_param(payload['date'], None)
        if nd:
            r.date = nd
    if hasattr(r, 'time') and 'time' in payload:
        time_raw = payload.get('time')
        if isinstance(time_raw, str) and len(time_raw) == 5 and time_raw[2] == ':':
            hh, mm = time_raw.split(':', 1)
            if hh.isdigit() and mm.isdigit():
                hhi, mmi = int(hh), int(mm)
                if 0 <= hhi < 24 and 0 <= mmi < 60:
                    r.time = f"{hhi:02d}:{mmi:02d}"
    if hasattr(r, 'category') and 'category' in payload:
        r.category = sanitize_text(payload.get('category')) if payload.get('category') else None
    if hasattr(r, 'color') and 'color' in payload:
        r.color = sanitize_text(payload.get('color')) if payload.get('color') else None
    db.session.commit()
    return jsonify({'ok': True, 'reminder': _serialize_reminder(r)})


@main_bp.route('/api/reminders', methods=['DELETE'])
def api_reminders_delete_bulk():
    payload = request.get_json(silent=True) or {}
    ids = payload.get('ids') or []
    user = sanitize_text(payload.get('creator', ''))
    if not isinstance(ids, list) or not ids:
        return jsonify({'ok': False, 'error': 'No ids provided'}), 400
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    deleted = 0
    dates = set()
    for rid in ids:
        if not isinstance(rid, int):
            continue
        r = Reminder.query.get(rid)
        if not r:
            continue
        if user in admin_aliases or user == (r.creator or ''):
            if r.date:
                dates.add(r.date.strftime('%Y-%m-%d'))
            db.session.delete(r)
            deleted += 1
    if deleted:
        db.session.commit()
    return jsonify({'ok': True, 'deleted': deleted, 'dates': list(dates)})


@main_bp.route('/calendar/add', methods=['POST'])
def add_reminder():
    date_s = sanitize_text(request.form.get('date'))
    title = sanitize_text(request.form.get('title'))
    description = sanitize_html(request.form.get('description'))
    creator = sanitize_text(request.form.get('creator'))
    if not (date_s and title):
        flash('Date and title are required for reminders.', 'error')
        return redirect(url_for('main.index'))
    try:
        d = datetime.strptime(date_s, '%Y-%m-%d').date()
    except Exception:
        flash('Invalid date.', 'error')
        return redirect(url_for('main.index'))
    r = Reminder(date=d, title=title, description=description, creator=creator)
    db.session.add(r)
    db.session.commit()
    flash('Reminder added.', 'success')
    return redirect(url_for('main.index', date=date_s))


@main_bp.route('/calendar/delete/<int:reminder_id>', methods=['POST'])
def delete_reminder(reminder_id):
    r = Reminder.query.get_or_404(reminder_id)
    user = sanitize_text(request.form.get('user'))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if user in admin_aliases or user == r.creator:
        db.session.delete(r)
        db.session.commit()
        flash('Reminder deleted.', 'success')
    else:
        flash('Not allowed to delete this reminder.', 'error')
    date_s = None
    try:
        if r.date:
            date_s = r.date.strftime('%Y-%m-%d')
    except Exception:
        date_s = None
    return redirect(url_for('main.index', date=date_s) if date_s else url_for('main.index'))


@main_bp.route('/calendar/delete_bulk', methods=['POST'])
def delete_reminders_bulk():
    ids_raw = sanitize_text(request.form.get('ids', ''))
    user = sanitize_text(request.form.get('user', ''))
    if not ids_raw:
        return redirect(url_for('main.index'))
    id_list = []
    for part in ids_raw.split(','):
        part = part.strip()
        if part.isdigit():
            id_list.append(int(part))
    if not id_list:
        return redirect(url_for('main.index'))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    kept_date = None
    deleted = 0
    for rid in id_list:
        r = Reminder.query.get(rid)
        if not r:
            continue
        if kept_date is None and getattr(r, 'date', None):
            try:
                kept_date = r.date.strftime('%Y-%m-%d')
            except Exception:
                kept_date = None
        if user in admin_aliases or user == r.creator:
            db.session.delete(r)
            deleted += 1
    if deleted:
        db.session.commit()
        flash(f'Deleted {deleted} reminder(s).', 'success')
    else:
        flash('No reminders deleted (permission?).', 'error')
    return redirect(url_for('main.index', date=kept_date) if kept_date else url_for('main.index'))


@main_bp.route('/notice', methods=['POST'])
def update_notice():
    content = sanitize_html(request.form.get('content', ''))
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    if user != admin_name:
        flash('Only admin can update the notice.', 'error')
        return redirect(url_for('main.index'))
    n = Notice.query.order_by(Notice.updated_at.desc()).first()
    now = datetime.utcnow()
    if n:
        n.content = content
        n.updated_by = user
        n.updated_at = now
    else:
        db.session.add(Notice(content=content, updated_by=user, updated_at=now))
    db.session.commit()
    flash('Notice updated.', 'success')
    return redirect(url_for('main.index'))


@main_bp.route('/api/family-notices/send', methods=['POST'])
def api_send_family_notice():
    try:
        data = request.get_json(force=True) or {}
        sender = sanitize_text(str(data.get('sender') or session.get('current_member') or '').strip())
        message = sanitize_text(str(data.get('message', '')).strip())
        channels = data.get('channels', ['in_app'])
        require_ack = bool(data.get('require_ack', True))

        if not sender:
            return jsonify({'ok': False, 'error': 'Sender is required'}), 400
        if not message:
            return jsonify({'ok': False, 'error': 'Notice message is required'}), 400

        normalized_channels = []
        if isinstance(channels, list):
            for ch in channels:
                c = sanitize_text(str(ch or '').strip().lower())
                if c in {'in_app', 'email', 'mobile'} and c not in normalized_channels:
                    normalized_channels.append(c)
        if not normalized_channels:
            normalized_channels = ['in_app']

        members = FamilyMember.query.order_by(FamilyMember.name.asc()).all()
        recipients = [sanitize_text((m.name or '')).strip() for m in members if sanitize_text((m.name or '')).strip() and sanitize_text((m.name or '')).strip() != sender]

        if not recipients:
            return jsonify({'ok': False, 'error': 'No recipients available'}), 400

        notice = FamilyNotice(
            title='Family Notice',
            message=message,
            sender=sender,
            recipients_json=json.dumps(recipients),
            channels_json=json.dumps(normalized_channels),
            require_ack=require_ack,
        )
        db.session.add(notice)
        db.session.flush()

        publish_notification(
            event_type='family_notice',
            title='Family Notice',
            message=f"{sender}: {message}",
            target_members=recipients,
            created_by=sender,
            payload={'family_notice_id': notice.id, 'sender': sender},
            channels=normalized_channels,
        )
        db.session.commit()

        return jsonify({
            'ok': True,
            'notice': {
                'id': notice.id,
                'message': notice.message,
                'sender': notice.sender,
                'channels': normalized_channels,
                'recipients': recipients,
                'require_ack': require_ack,
                'acked_count': 0,
                'total_recipients': len(recipients),
            }
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@main_bp.route('/api/family-notices/pending', methods=['GET'])
def api_family_notices_pending():
    member = sanitize_text(str(request.args.get('member') or session.get('current_member') or '').strip())
    if not member:
        return jsonify({'ok': True, 'items': []})

    rows = FamilyNotice.query.order_by(FamilyNotice.created_at.asc(), FamilyNotice.id.asc()).all()
    pending = []
    for n in rows:
        try:
            recipients = json.loads(n.recipients_json or '[]')
            if not isinstance(recipients, list):
                recipients = []
        except Exception:
            recipients = []
        if member not in recipients:
            continue
        if not bool(getattr(n, 'require_ack', True)):
            continue
        ack = FamilyNoticeAcknowledgement.query.filter_by(notice_id=n.id, member_name=member).first()
        if ack:
            continue
        pending.append({
            'id': n.id,
            'title': n.title or 'Family Notice',
            'message': n.message or '',
            'sender': n.sender or '',
            'created_at': n.created_at.isoformat() if n.created_at else None,
        })

    return jsonify({'ok': True, 'items': pending})


@main_bp.route('/api/family-notices/<int:notice_id>/ack', methods=['POST'])
def api_ack_family_notice(notice_id):
    notice = FamilyNotice.query.get_or_404(notice_id)
    data = request.get_json(silent=True) or {}
    member = sanitize_text(str(data.get('member') or session.get('current_member') or '').strip())
    if not member:
        return jsonify({'ok': False, 'error': 'Member is required'}), 400

    try:
        recipients = json.loads(notice.recipients_json or '[]')
        if not isinstance(recipients, list):
            recipients = []
    except Exception:
        recipients = []

    if member not in recipients:
        return jsonify({'ok': False, 'error': 'Not a recipient for this notice'}), 403

    existing = FamilyNoticeAcknowledgement.query.filter_by(notice_id=notice.id, member_name=member).first()
    if not existing:
        db.session.add(FamilyNoticeAcknowledgement(notice_id=notice.id, member_name=member, acknowledged_at=datetime.utcnow()))
        db.session.commit()

    acked_rows = FamilyNoticeAcknowledgement.query.filter_by(notice_id=notice.id).all()
    acked = sorted([sanitize_text(a.member_name or '').strip() for a in acked_rows if sanitize_text(a.member_name or '').strip()])
    pending = sorted([r for r in recipients if r and r not in acked])

    return jsonify({
        'ok': True,
        'notice_id': notice.id,
        'acked': acked,
        'pending': pending,
        'acked_count': len(acked),
        'total_recipients': len(recipients),
    })


@main_bp.route('/api/family-notices/mine', methods=['GET'])
def api_family_notices_mine():
    sender = sanitize_text(str(request.args.get('sender') or session.get('current_member') or '').strip())
    if not sender:
        return jsonify({'ok': True, 'items': []})

    rows = FamilyNotice.query.filter_by(sender=sender).order_by(FamilyNotice.created_at.desc()).limit(20).all()
    items = []
    for n in rows:
        try:
            recipients = json.loads(n.recipients_json or '[]')
            if not isinstance(recipients, list):
                recipients = []
        except Exception:
            recipients = []
        acked_rows = FamilyNoticeAcknowledgement.query.filter_by(notice_id=n.id).all()
        acked = sorted([sanitize_text(a.member_name or '').strip() for a in acked_rows if sanitize_text(a.member_name or '').strip()])
        pending = sorted([r for r in recipients if r and r not in acked])
        items.append({
            'id': n.id,
            'title': n.title,
            'message': n.message,
            'sender': n.sender,
            'require_ack': bool(getattr(n, 'require_ack', True)),
            'created_at': n.created_at.isoformat() if n.created_at else None,
            'acked': acked,
            'pending': pending,
            'acked_count': len(acked),
            'total_recipients': len(recipients),
        })

    return jsonify({'ok': True, 'items': items})


@main_bp.route('/notice-history', methods=['GET'])
def notice_history_page():
    sender = sanitize_text(str(request.args.get('sender') or session.get('current_member') or '').strip())
    if not sender:
        sender = sanitize_text(current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator'))

    page = request.args.get('page', type=int) or 1
    if page < 1:
        page = 1
    per_page = 25

    query = FamilyNotice.query.filter_by(sender=sender).order_by(FamilyNotice.created_at.desc(), FamilyNotice.id.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    items = []
    for n in pagination.items:
        try:
            recipients = json.loads(n.recipients_json or '[]')
            if not isinstance(recipients, list):
                recipients = []
        except Exception:
            recipients = []

        acked_rows = FamilyNoticeAcknowledgement.query.filter_by(notice_id=n.id).all()
        acked = sorted([sanitize_text(a.member_name or '').strip() for a in acked_rows if sanitize_text(a.member_name or '').strip()])
        pending = sorted([r for r in recipients if r and r not in acked])

        try:
            channels = json.loads(n.channels_json or '[]')
            if not isinstance(channels, list):
                channels = []
        except Exception:
            channels = []

        items.append({
            'id': n.id,
            'title': n.title or 'Family Notice',
            'message': n.message or '',
            'sender': n.sender or '',
            'require_ack': bool(getattr(n, 'require_ack', True)),
            'channels': channels,
            'created_at': n.created_at,
            'recipients': recipients,
            'acked': acked,
            'pending': pending,
            'acked_count': len(acked),
            'total_recipients': len(recipients),
        })

    return render_template(
        'notice_history.html',
        sender=sender,
        items=items,
        page=pagination.page,
        pages=pagination.pages,
        total=pagination.total,
        has_prev=pagination.has_prev,
        has_next=pagination.has_next,
        prev_num=pagination.prev_num,
        next_num=pagination.next_num,
    )


@main_bp.route('/whoishome', methods=['POST'])
def who_is_home_action():
    action = sanitize_text(request.form.get('action', 'update'))
    config = current_app.config['HOMEHUB_CONFIG']
    family = set(m.name for m in FamilyMember.query.all())
    name = sanitize_text(request.form.get('name', ''))
    if not name or name not in family:
        if request.headers.get('X-Requested-With') != 'fetch':
            flash('Invalid user for status.', 'error')
        if request.headers.get('X-Requested-With') == 'fetch':
            return jsonify({'ok': False, 'error': 'Invalid user'}), 400
        return redirect(url_for('main.index'))
    result = None
    if action == 'clear':
        hs = HomeStatus.query.filter_by(name=name).first()
        if hs:
            db.session.delete(hs)
            db.session.commit()
            result = 'cleared'
            if request.headers.get('X-Requested-With') != 'fetch':
                flash('Status cleared.', 'success')
        else:
            result = 'none'
            if request.headers.get('X-Requested-With') != 'fetch':
                flash('No status to clear.', 'info')
    else:
        status = sanitize_text(request.form.get('status', '')) or 'Away'
        hs = HomeStatus.query.filter_by(name=name).first()
        if hs:
            hs.status = status
        else:
            db.session.add(HomeStatus(name=name, status=status))
        db.session.commit()
        result = 'updated'
        if request.headers.get('X-Requested-With') != 'fetch':
            flash('Status updated.', 'success')
    if request.headers.get('X-Requested-With') == 'fetch':
        who_statuses = {s.name: s.status for s in HomeStatus.query.all() if s.name in family}
        member_statuses = {ms.name: ms.text for ms in MemberStatus.query.all() if ms.name in family and (ms.text or '').strip()}
        result = result or 'updated'
        return jsonify({'ok': True, 'who_statuses': who_statuses, 'member_statuses': member_statuses, 'result': result})
    date_q = request.args.get('date') or request.form.get('date')
    return redirect(url_for('main.index', date=date_q) if date_q else url_for('main.index'))


@main_bp.route('/status/update', methods=['POST'])
def member_status_update():
    config = current_app.config['HOMEHUB_CONFIG']
    family = set(m.name for m in FamilyMember.query.all())
    name = sanitize_text(request.form.get('name', ''))
    raw_text = request.form.get('text', '') or ''
    text = sanitize_text(raw_text)
    if not name or name not in family:
        if request.headers.get('X-Requested-With') != 'fetch':
            flash('Invalid user for status.', 'error')
        if request.headers.get('X-Requested-With') == 'fetch':
            return jsonify({'ok': False, 'error': 'Invalid user'}), 400
        return redirect(url_for('main.index'))
    if not text:
        if request.headers.get('X-Requested-With') == 'fetch':
            return jsonify({'ok': False, 'error': 'Empty status'}), 400
        else:
            flash('Status cannot be empty.', 'error')
            return redirect(url_for('main.index'))
    ms = MemberStatus.query.filter_by(name=name).first()
    now = datetime.utcnow()
    if ms:
        ms.text = text
        ms.updated_at = now
    else:
        db.session.add(MemberStatus(name=name, text=text, updated_at=now))
    db.session.commit()
    if request.headers.get('X-Requested-With') != 'fetch':
        flash('Status saved.', 'success')
    if request.headers.get('X-Requested-With') == 'fetch':
        who_statuses = {s.name: s.status for s in HomeStatus.query.all() if s.name in family}
        member_statuses = {ms.name: ms.text for ms in MemberStatus.query.all() if ms.name in family and (ms.text or '').strip()}
        return jsonify({'ok': True, 'who_statuses': who_statuses, 'member_statuses': member_statuses, 'result': 'saved'})
    return redirect(url_for('main.index'))


@main_bp.route('/status/delete', methods=['POST'])
def member_status_delete():
    config = current_app.config['HOMEHUB_CONFIG']
    family = set(m.name for m in FamilyMember.query.all())
    name = sanitize_text(request.form.get('name', ''))
    if not name or name not in family:
        if request.headers.get('X-Requested-With') != 'fetch':
            flash('Invalid user for status removal.', 'error')
        if request.headers.get('X-Requested-With') == 'fetch':
            return jsonify({'ok': False, 'error': 'Invalid user'}), 400
        return redirect(url_for('main.index'))
    ms = MemberStatus.query.filter_by(name=name).first()
    removed = False
    if ms:
        db.session.delete(ms)
        db.session.commit()
        removed = True
        if request.headers.get('X-Requested-With') != 'fetch':
            flash('Status removed.', 'success')
    if request.headers.get('X-Requested-With') == 'fetch':
        who_statuses = {s.name: s.status for s in HomeStatus.query.all() if s.name in family}
        member_statuses = {ms.name: ms.text for ms in MemberStatus.query.all() if ms.name in family and (ms.text or '').strip()}
        return jsonify({'ok': True, 'who_statuses': who_statuses, 'member_statuses': member_statuses, 'result': 'removed' if removed else 'none'})
    return redirect(url_for('main.index'))


@main_bp.route('/personal-status', methods=['POST'])
def personal_status_alias():
    family = set(m.name for m in FamilyMember.query.all())
    name = sanitize_text(request.form.get('name', ''))
    action = sanitize_text(request.form.get('action', '')).strip().lower()
    text = sanitize_text(request.form.get('text', '') or '')

    if not name or name not in family:
        return jsonify({'ok': False, 'error': 'Invalid user'}), 400

    if action == 'delete':
        row = MemberStatus.query.filter_by(name=name).first()
        if row:
            db.session.delete(row)
            db.session.commit()
        return jsonify({'ok': True, 'result': 'removed'})

    if not text:
        return jsonify({'ok': False, 'error': 'Status cannot be empty'}), 400

    row = MemberStatus.query.filter_by(name=name).first()
    now = datetime.utcnow()
    if row:
        row.text = text
        row.updated_at = now
    else:
        db.session.add(MemberStatus(name=name, text=text, updated_at=now))
    db.session.commit()
    return jsonify({'ok': True, 'result': 'saved'})


@main_bp.route('/api/member-status/current', methods=['GET'])
def api_member_status_current():
    member = sanitize_text(str(request.args.get('member') or session.get('current_member') or '').strip())
    if not member:
        return jsonify({'ok': False, 'error': 'Member required'}), 400

    row = MemberStatus.query.filter_by(name=member).first()
    return jsonify({
        'ok': True,
        'member': member,
        'status': sanitize_text((row.text if row else '') or ''),
        'updated_at': row.updated_at.isoformat() if row and row.updated_at else None,
    })


@main_bp.route('/api/member-status', methods=['POST'])
def api_member_status_save():
    data = request.get_json(force=True) or {}
    member = sanitize_text(str(data.get('member') or session.get('current_member') or '').strip())
    text = sanitize_text(str(data.get('status', '') or '').strip())

    if not member:
        return jsonify({'ok': False, 'error': 'Member required'}), 400
    if not text:
        return jsonify({'ok': False, 'error': 'Status cannot be empty'}), 400

    family = set(m.name for m in FamilyMember.query.all())
    if member not in family:
        return jsonify({'ok': False, 'error': 'Invalid user'}), 400

    row = MemberStatus.query.filter_by(name=member).first()
    now = datetime.utcnow()
    if row:
        row.text = text
        row.updated_at = now
    else:
        db.session.add(MemberStatus(name=member, text=text, updated_at=now))
    db.session.commit()

    return jsonify({'ok': True, 'member': member, 'status': text, 'updated_at': now.isoformat()})


@main_bp.route('/api/member-status', methods=['DELETE'])
def api_member_status_delete():
    data = request.get_json(silent=True) or {}
    member = sanitize_text(str(data.get('member') or request.args.get('member') or session.get('current_member') or '').strip())
    if not member:
        return jsonify({'ok': False, 'error': 'Member required'}), 400

    row = MemberStatus.query.filter_by(name=member).first()
    if row:
        db.session.delete(row)
        db.session.commit()
    return jsonify({'ok': True, 'member': member, 'result': 'removed' if row else 'none'})
