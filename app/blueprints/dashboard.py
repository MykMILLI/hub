from flask import render_template, request, redirect, url_for, flash, jsonify, current_app, session
from datetime import datetime, date, timedelta, time, timezone
from zoneinfo import ZoneInfo
from ..models import db, HomeStatus, MemberStatus, Notice, Reminder, RecurringReminder, FamilyMember, ActivityLog, FamilyNotice, FamilyNoticeAcknowledgement, HouseholdRoutineState, LibraryBookLoan, PhotoAsset, FamilyCalendarEvent, FamilyChatMessage, Chore, AppNotification, KidRewardProfile, KidRewardRule, KidRewardEvent, KidRewardPendingGrant, KidRewardRedemption, KidRewardTimeout, KidRewardRepairTask, KidRewardSetting
from ..models import KidRewardParentActionTask
from ..blueprints import main_bp
from ..security import sanitize_html, sanitize_text
from ..services.chore_recurrence import is_effectively_done
from ..services.notification_service import publish_notification
import json
import hashlib

_BATH_NOTIFICATION_MEMBERS = ('Mykal', 'Rachel')
_KID_REWARD_ADMIN_NAMES = {'rachel', 'mykal'}
_KID_MEMBERS = ('Avery', 'Harley', 'Rachel', 'Mykal')
_KID_REWARD_MILESTONES = (
    {'stars': 10, 'label': 'Choose dessert night'},
    {'stars': 20, 'label': 'Pick a family game'},
    {'stars': 30, 'label': 'Choose a weekend activity'},
)
_DEFAULT_KID_REWARD_RULES = (
    {'behavior_kind': 'do', 'title': 'Used kind words', 'points': 1, 'emoji': '😊'},
    {'behavior_kind': 'do', 'title': 'Helped clean up toys', 'points': 2, 'emoji': '🧸'},
    {'behavior_kind': 'do', 'title': 'Followed directions the first time', 'points': 2, 'emoji': '👏'},
    {'behavior_kind': 'dont', 'title': 'Yelling or hurtful words', 'points': -2, 'emoji': '🗣️'},
    {'behavior_kind': 'dont', 'title': 'Hitting or rough play', 'points': -3, 'emoji': '🚫'},
    {'behavior_kind': 'dont', 'title': 'Not helping with cleanup', 'points': -1, 'emoji': '🧹'},
)
_DEFAULT_KID_REWARD_OPTIONS_BY_CHILD = {
    'Avery': (
        {'reward_label': 'Choose dessert night', 'stars_required': 10},
        {'reward_label': 'Pick bedtime movie', 'stars_required': 15},
        {'reward_label': 'Paint + chalk board time', 'stars_required': 20},
    ),
    'Harley': (
        {'reward_label': 'Choose dessert night', 'stars_required': 10},
        {'reward_label': 'Pick bedtime movie', 'stars_required': 15},
        {'reward_label': 'Play dough + snack choice', 'stars_required': 20},
    ),
    'Rachel': (
        {'reward_label': 'Pick Friday dinner', 'stars_required': 12},
        {'reward_label': 'Choose family game', 'stars_required': 18},
        {'reward_label': 'Pick weekend outing', 'stars_required': 24},
    ),
    'Mykal': (
        {'reward_label': 'Pick Friday dinner', 'stars_required': 12},
        {'reward_label': 'Choose family game', 'stars_required': 18},
        {'reward_label': 'Pick weekend outing', 'stars_required': 24},
    ),
}
_PARENT_NOTIFICATION_TARGETS = ('Rachel', 'Mykal')
_DISCIPLINE_METHODS = (
    'Use immediate, calm feedback so kids connect action to outcome.',
    'Keep consequences short, predictable, and age-appropriate.',
    'Pair correction with a repair step, like helping clean up.',
    'Return to positive reinforcement after timeout is complete.',
)
_DEFAULT_REPAIR_TASK = 'Apologize and help clean up the area.'
_HELPER_INSPIRATION_QUOTES = (
    'Small consistent steps today build a calmer home tomorrow.',
    'Connection before correction works better than pressure.',
    'You are building trust with every calm response.',
    'Progress, not perfection, is the parenting win this week.',
    'Teamwork at home starts with one helpful moment at a time.',
)


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


def _current_actor_name() -> str:
    member = sanitize_text(session.get('current_member') or '').strip()
    if member:
        return member
    return sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator')).strip() or 'Administrator'


def _can_manage_kid_rewards(actor_name: str) -> bool:
    clean_name = sanitize_text(actor_name or '').strip()
    if not clean_name:
        return False
    return sanitize_text(clean_name).strip().lower() in _KID_REWARD_ADMIN_NAMES


def _bedtime_child_assignments_for_reader(reader_name: str) -> dict:
    if sanitize_text(reader_name or '').strip().lower() == 'mykal':
        return {'book_picker': 'Avery', 'movie_picker': 'Harley'}
    return {'book_picker': 'Harley', 'movie_picker': 'Avery'}


def _ensure_kid_reward_profiles(actor_name: str) -> list[KidRewardProfile]:
    changed = False
    existing = {sanitize_text(row.child_name or '').strip(): row for row in KidRewardProfile.query.all()}
    for child in _KID_MEMBERS:
        if child in existing:
            continue
        row = KidRewardProfile(child_name=child, stars=0, updated_by=actor_name)
        db.session.add(row)
        existing[child] = row
        changed = True
    if changed:
        db.session.commit()
    return [existing[name] for name in _KID_MEMBERS if name in existing]


def _ensure_default_kid_reward_rules(actor_name: str) -> None:
    if KidRewardRule.query.count() > 0:
        return
    for index, rule in enumerate(_DEFAULT_KID_REWARD_RULES):
        db.session.add(KidRewardRule(
            behavior_kind=rule['behavior_kind'],
            title=rule['title'],
            points=rule['points'],
            emoji=rule['emoji'],
            sort_order=index,
            active=True,
            created_by=actor_name,
            updated_by=actor_name,
        ))
    db.session.commit()


def _get_or_create_kid_reward_settings(actor_name: str) -> KidRewardSetting:
    row = KidRewardSetting.query.first()
    if row:
        return row
    row = KidRewardSetting(
        confetti_enabled=True,
        sounds_enabled=False,
        timeout_default_minutes=5,
        timeout_default_star_loss=1,
        timeout_default_reward_minutes_loss=10,
        updated_by=actor_name,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _default_reward_options_for_child(child_name: str) -> list[dict]:
    options = _DEFAULT_KID_REWARD_OPTIONS_BY_CHILD.get(child_name, ())
    normalized = []
    for option in options[:3]:
        normalized.append({
            'reward_label': sanitize_text(option.get('reward_label') or '').strip()[:256],
            'stars_required': max(1, min(100, int(option.get('stars_required') or 1))),
        })
    return [row for row in normalized if row['reward_label']]


def _ensure_default_kid_reward_options(actor_name: str) -> None:
    existing_rows = (
        KidRewardRule.query
        .filter(KidRewardRule.active.is_(True))
        .filter(KidRewardRule.behavior_kind == 'reward_option')
        .all()
    )
    present_by_child: dict[str, list[KidRewardRule]] = {child: [] for child in _KID_MEMBERS}
    for row in existing_rows:
        child_name = sanitize_text(row.emoji or '').strip()
        if child_name in present_by_child:
            present_by_child[child_name].append(row)

    changed = False
    for child_name in _KID_MEMBERS:
        if present_by_child[child_name]:
            continue
        for index, option in enumerate(_default_reward_options_for_child(child_name)):
            db.session.add(KidRewardRule(
                behavior_kind='reward_option',
                title=option['reward_label'],
                points=int(option['stars_required']),
                emoji=child_name,
                sort_order=index,
                active=True,
                created_by=actor_name,
                updated_by=actor_name,
            ))
            changed = True
    if changed:
        db.session.commit()


def _reward_options_by_child() -> dict[str, list[dict]]:
    rows = (
        KidRewardRule.query
        .filter(KidRewardRule.active.is_(True))
        .filter(KidRewardRule.behavior_kind == 'reward_option')
        .order_by(KidRewardRule.emoji.asc(), KidRewardRule.sort_order.asc(), KidRewardRule.id.asc())
        .all()
    )
    by_child: dict[str, list[dict]] = {child: [] for child in _KID_MEMBERS}
    for row in rows:
        child_name = sanitize_text(row.emoji or '').strip()
        if child_name not in by_child:
            continue
        label = sanitize_text(row.title or '').strip()[:256]
        if not label:
            continue
        by_child[child_name].append({
            'id': row.id,
            'reward_label': label,
            'stars_required': max(1, min(100, int(row.points or 1))),
        })
    return by_child


def _reward_catalog_options() -> list[dict]:
    by_child = _reward_options_by_child()
    grouped: dict[tuple[str, int], dict] = {}
    for child_name, options in by_child.items():
        for option in options:
            label = sanitize_text(option.get('reward_label') or '').strip()[:256]
            stars_required = max(1, min(100, int(option.get('stars_required') or 1)))
            key = (label.lower(), stars_required)
            row = grouped.get(key)
            if not row:
                row = {
                    'reward_label': label,
                    'stars_required': stars_required,
                    'applies_to': [],
                }
                grouped[key] = row
            if child_name not in row['applies_to']:
                row['applies_to'].append(child_name)

    output = []
    for row in grouped.values():
        applies_to = sorted(row['applies_to'], key=lambda name: _KID_MEMBERS.index(name) if name in _KID_MEMBERS else 999)
        output.append({
            'reward_label': row['reward_label'],
            'stars_required': row['stars_required'],
            'applies_to': applies_to,
        })
    output.sort(key=lambda item: (int(item['stars_required']), str(item['reward_label']).lower()))
    return output


def _kid_reward_progress(stars: int, reward_options: list[dict] | None = None) -> dict:
    current = max(0, int(stars or 0))
    options = []
    for option in (reward_options or []):
        try:
            stars_required = int(option.get('stars_required'))
        except (TypeError, ValueError, AttributeError):
            continue
        label = sanitize_text((option or {}).get('reward_label') or '').strip()
        if stars_required <= 0 or not label:
            continue
        options.append({'stars_required': stars_required, 'reward_label': label})
    options.sort(key=lambda row: (row['stars_required'], row['reward_label']))

    if options:
        next_option = None
        for option in options:
            if current < option['stars_required']:
                next_option = option
                break
        if not next_option:
            final_target = options[-1]
            return {
                'stars': current,
                'next_target': final_target['stars_required'],
                'next_label': final_target['reward_label'],
                'progress_percent': 100,
                'remaining': 0,
            }
        target = int(next_option['stars_required'])
        lower_bound = 0
        for option in options:
            if option['stars_required'] < target:
                lower_bound = int(option['stars_required'])
        span = max(1, target - lower_bound)
        progressed = max(0, min(span, current - lower_bound))
        return {
            'stars': current,
            'next_target': target,
            'next_label': next_option['reward_label'],
            'progress_percent': int((progressed / span) * 100),
            'remaining': max(0, target - current),
        }

    next_milestone = None
    for milestone in _KID_REWARD_MILESTONES:
        if current < milestone['stars']:
            next_milestone = milestone
            break
    if not next_milestone:
        final_target = _KID_REWARD_MILESTONES[-1]
        return {
            'stars': current,
            'next_target': final_target['stars'],
            'next_label': final_target['label'],
            'progress_percent': 100,
            'remaining': 0,
        }
    target = int(next_milestone['stars'])
    lower_bound = 0
    for milestone in _KID_REWARD_MILESTONES:
        if milestone['stars'] < target:
            lower_bound = milestone['stars']
    span = max(1, target - lower_bound)
    progressed = max(0, min(span, current - lower_bound))
    return {
        'stars': current,
        'next_target': target,
        'next_label': next_milestone['label'],
        'progress_percent': int((progressed / span) * 100),
        'remaining': max(0, target - current),
    }


def _reward_milestones_crossed(before_stars: int, after_stars: int) -> list[dict]:
    start = max(0, int(before_stars or 0))
    end = max(0, int(after_stars or 0))
    if end <= start:
        return []
    crossed = []
    for milestone in _KID_REWARD_MILESTONES:
        target = int(milestone['stars'])
        if start < target <= end:
            crossed.append(milestone)
    return crossed


def _pending_repair_tasks_for_child(child_name: str) -> list[KidRewardRepairTask]:
    clean_child = sanitize_text(child_name or '').strip()
    if clean_child not in _KID_MEMBERS:
        return []
    return (
        KidRewardRepairTask.query
        .filter_by(child_name=clean_child, completed=False)
        .order_by(KidRewardRepairTask.created_at.asc(), KidRewardRepairTask.id.asc())
        .all()
    )


def _child_has_pending_repair(child_name: str) -> bool:
    return bool(_pending_repair_tasks_for_child(child_name))


def _reward_follow_through_parents_for_child(child_name: str) -> list[str]:
    clean_child = sanitize_text(child_name or '').strip()
    if clean_child == 'Rachel':
        return ['Mykal']
    if clean_child == 'Mykal':
        return ['Rachel']
    return list(_PARENT_NOTIFICATION_TARGETS)


def _reward_notification_targets() -> list[str]:
    config_members = current_app.config.get('HOMEHUB_CONFIG', {}).get('family_members') or []
    targets = []
    for name in config_members:
        clean_name = sanitize_text(name or '').strip()
        if clean_name and clean_name not in targets:
            targets.append(clean_name)
    if targets:
        return targets
    return list(_KID_MEMBERS)


def _publish_reward_event_notification(*, child_name: str, delta: int, actor_name: str, reason: str | None) -> None:
    profile = KidRewardProfile.query.filter_by(child_name=child_name).first()
    current_stars = max(0, int(getattr(profile, 'stars', 0) or 0))
    progress = _kid_reward_progress(current_stars, _reward_options_by_child().get(child_name) or [])
    remaining = max(0, int(progress.get('remaining') or 0))
    next_label = sanitize_text(progress.get('next_label') or '').strip() or 'next reward'
    clean_reason = sanitize_text(reason or '').strip()[:256]
    if delta >= 0:
        message = f"Way to go {child_name}! Only {remaining} more star{'s' if remaining != 1 else ''} to get {next_label}."
        if clean_reason:
            message = f"{message} Reason: {clean_reason}"
        event_type = 'kid_reward_star_awarded'
    else:
        message = f"{child_name} had a consequence. We will work to improve in this area moving forward."
        if clean_reason:
            message = f"{message} Reason: {clean_reason}"
        event_type = 'kid_reward_consequence'
    publish_notification(
        event_type=event_type,
        title='Gold Star Update',
        message=message,
        target_members=_reward_notification_targets(),
        created_by=actor_name,
        payload={
            'child_name': child_name,
            'delta': int(delta),
            'remaining_to_next_reward': remaining,
            'next_reward_label': next_label,
            'reason': clean_reason,
        },
        channels=['in_app'],
    )


def _create_parent_reward_tasks(*, redemption: KidRewardRedemption, actor_name: str) -> list[KidRewardParentActionTask]:
    created_rows = []
    for parent_name in _reward_follow_through_parents_for_child(redemption.child_name):
        chore = Chore(
            description=f"Reward follow-through for {redemption.child_name}: {redemption.reward_label}",
            creator=actor_name,
            done=False,
            tags=json.dumps(['reward_support', redemption.child_name]),
            task_type='todo',
            assignee=parent_name,
            assignees_json=json.dumps([parent_name]),
            watchers_json=json.dumps([]),
            days_of_week_json=json.dumps([]),
            due_date=date.today(),
            status='todo',
            status_options_json=json.dumps(['todo', 'in_progress', 'completed']),
            mentions_json=json.dumps([parent_name]),
            details_html='',
            checklist_json=json.dumps([]),
            checklist_meta_json=json.dumps({}),
            chart_data_json=json.dumps([]),
            table_data_json=json.dumps({'headers': [], 'rows': []}),
            completed_at=None,
            reward=None,
            updated_at=datetime.utcnow(),
        )
        db.session.add(chore)
        db.session.flush()
        row = KidRewardParentActionTask(
            redemption_id=redemption.id,
            child_name=redemption.child_name,
            reward_label=redemption.reward_label,
            parent_name=parent_name,
            chore_id=chore.id,
        )
        db.session.add(row)
        created_rows.append(row)
        publish_notification(
            event_type='reward_parent_task',
            title='Reward follow-through task',
            message=f"{redemption.child_name}'s reward needs your follow-through: {redemption.reward_label}",
            target_members=[parent_name],
            created_by=actor_name,
            payload={'reward_child': redemption.child_name, 'reward': redemption.reward_label, 'task_id': chore.id},
            channels=['in_app', 'mobile'],
        )
    return created_rows


def _hash_bucket(*parts: str) -> int:
    payload = '|'.join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    return int(digest[:8], 16)


def _next_sunday_on_or_after(day_value: date) -> date:
    return day_value + timedelta(days=(6 - day_value.weekday()) % 7)


def _meal_plan_helper_key(on_sunday: date, member_name: str) -> str:
    return f"meal-plan:{on_sunday.isoformat()}:{sanitize_text(member_name or '').strip().lower()}"


def _inspiration_window_for_member_on_date(member_name: str, on_date: date, tz: ZoneInfo) -> dict | None:
    clean_member = sanitize_text(member_name or '').strip()
    if clean_member not in _PARENT_NOTIFICATION_TARGETS:
        return None
    day_key = on_date.isoformat()
    gate = _hash_bucket('inspiration-day', clean_member.lower(), day_key) % 100
    if gate >= 45:
        return None
    random_minute = 9 * 60 + (_hash_bucket('inspiration-minute', clean_member.lower(), day_key) % (11 * 60))
    hour = random_minute // 60
    minute_of_hour = random_minute % 60
    scheduled_eastern = datetime.combine(on_date, time(hour, minute_of_hour), tzinfo=tz)
    scheduled_utc = scheduled_eastern.astimezone(timezone.utc)
    return {
        'member': clean_member,
        'date': day_key,
        'scheduled_eastern': scheduled_eastern,
        'scheduled_utc': scheduled_utc,
    }


def _parent_helper_summary(actor_name: str, now_utc: datetime | None = None) -> dict:
    clean_actor = sanitize_text(actor_name or '').strip()
    eastern = ZoneInfo('America/New_York')
    current_utc = now_utc or datetime.now(timezone.utc)
    now_eastern = current_utc.astimezone(eastern)
    sunday = _next_sunday_on_or_after(now_eastern.date())
    sunday_due_eastern = datetime.combine(sunday, time(10, 0), tzinfo=eastern)
    sunday_due_utc = sunday_due_eastern.astimezone(timezone.utc)

    parent_task_rows = (
        KidRewardParentActionTask.query
        .order_by(KidRewardParentActionTask.created_at.desc(), KidRewardParentActionTask.id.desc())
        .limit(200)
        .all()
    )
    pending_by_parent = {}
    for parent_name in _PARENT_NOTIFICATION_TARGETS:
        pending_by_parent[parent_name] = []
    for row in parent_task_rows:
        parent_name = sanitize_text(row.parent_name or '').strip()
        if parent_name not in pending_by_parent:
            continue
        chore_row = db.session.get(Chore, row.chore_id) if row.chore_id else None
        is_done = bool(getattr(chore_row, 'done', False))
        if is_done:
            continue
        pending_by_parent[parent_name].append({
            'child_name': row.child_name,
            'reward_label': row.reward_label,
            'chore_id': row.chore_id,
            'created_at': row.created_at.isoformat() if row.created_at else '',
        })

    meal_plan_by_parent = []
    for parent_name in _PARENT_NOTIFICATION_TARGETS:
        key = _meal_plan_helper_key(sunday, parent_name)
        sent_row = (
            AppNotification.query
            .filter(AppNotification.event_type == 'meal_plan_helper')
            .filter(AppNotification.target_member == parent_name)
            .filter(AppNotification.payload_json.contains(key))
            .first()
        )
        if sent_row:
            status_label = 'Reminder sent'
        elif current_utc >= sunday_due_utc:
            status_label = 'Due now (pending reminder send)'
        else:
            status_label = 'Scheduled'
        meal_plan_by_parent.append({
            'parent_name': parent_name,
            'status': status_label,
            'sent': bool(sent_row),
        })

    show_debug_windows = _can_manage_kid_rewards(clean_actor)
    next_windows = []
    if show_debug_windows:
        for parent_name in _PARENT_NOTIFICATION_TARGETS:
            found = None
            for offset in range(0, 8):
                target_date = now_eastern.date() + timedelta(days=offset)
                window = _inspiration_window_for_member_on_date(parent_name, target_date, eastern)
                if not window:
                    continue
                if window['scheduled_utc'] <= current_utc:
                    continue
                found = window
                break
            next_windows.append({
                'parent_name': parent_name,
                'scheduled_at': found['scheduled_eastern'].isoformat() if found else '',
                'scheduled_display': (
                    found['scheduled_eastern'].strftime('%a %b %d, %I:%M %p ET')
                    if found else 'No window in next 7 days'
                ),
            })

    return {
        'pending_by_parent': [
            {
                'parent_name': parent_name,
                'pending_count': len(items),
                'task_items': items[:3],
            }
            for parent_name, items in pending_by_parent.items()
        ],
        'meal_plan': {
            'sunday_date': sunday.isoformat(),
            'due_display': sunday_due_eastern.strftime('%a %b %d, %I:%M %p ET'),
            'by_parent': meal_plan_by_parent,
        },
        'show_debug_windows': show_debug_windows,
        'next_inspiration_windows': next_windows,
    }


def emit_due_parent_helper_notifications_for_member(member_name: str, now_utc: datetime | None = None) -> int:
    clean_member = sanitize_text(member_name or '').strip()
    if clean_member not in _PARENT_NOTIFICATION_TARGETS:
        return 0
    eastern = ZoneInfo('America/New_York')
    current_utc = now_utc or datetime.now(timezone.utc)
    now_eastern = current_utc.astimezone(eastern)
    sent = 0

    if now_eastern.weekday() == 6:
        scheduled_meal_utc = datetime.combine(now_eastern.date(), time(10, 0), tzinfo=eastern).astimezone(timezone.utc)
        if scheduled_meal_utc <= current_utc <= scheduled_meal_utc + timedelta(minutes=20):
            meal_key = _meal_plan_helper_key(now_eastern.date(), clean_member)
            already = (
                AppNotification.query
                .filter(AppNotification.event_type == 'meal_plan_helper')
                .filter(AppNotification.target_member == clean_member)
                .filter(AppNotification.payload_json.contains(meal_key))
                .first()
            )
            if not already:
                publish_notification(
                    event_type='meal_plan_helper',
                    title='Meal planning reminder',
                    message='Quick weekly reset: plan meals for the week today.',
                    target_members=[clean_member],
                    created_by='HomeHub',
                    payload={'reminder_key': meal_key},
                    channels=['in_app', 'mobile'],
                )
                sent += 1

    day_key = now_eastern.date().isoformat()
    inspiration_window = _inspiration_window_for_member_on_date(clean_member, now_eastern.date(), eastern)
    if inspiration_window:
        scheduled_inspiration_utc = inspiration_window['scheduled_utc']
        if scheduled_inspiration_utc <= current_utc <= scheduled_inspiration_utc + timedelta(minutes=20):
            insp_key = f"inspiration:{day_key}:{clean_member.lower()}"
            already = (
                AppNotification.query
                .filter(AppNotification.event_type == 'parent_inspiration')
                .filter(AppNotification.target_member == clean_member)
                .filter(AppNotification.payload_json.contains(insp_key))
                .first()
            )
            if not already:
                quote_idx = _hash_bucket('inspiration-quote', clean_member.lower(), day_key) % len(_HELPER_INSPIRATION_QUOTES)
                publish_notification(
                    event_type='parent_inspiration',
                    title='Parent helper note',
                    message=_HELPER_INSPIRATION_QUOTES[quote_idx],
                    target_members=[clean_member],
                    created_by='HomeHub',
                    payload={'reminder_key': insp_key},
                    channels=['in_app', 'mobile'],
                )
                sent += 1

    return sent


def _publish_kid_reward_milestone_notification(child_name: str, stars: int, milestone: dict, actor_name: str) -> None:
    milestone_stars = int(milestone.get('stars') or 0)
    key = f"kid-reward-milestone:{child_name.lower()}:{milestone_stars}"
    already_sent = (
        AppNotification.query
        .filter(AppNotification.event_type == 'kid_reward_milestone')
        .filter(AppNotification.payload_json.contains(key))
        .first()
    )
    if already_sent:
        return
    publish_notification(
        event_type='kid_reward_milestone',
        title='Gold Star Milestone',
        message=f'{child_name} reached {milestone_stars} stars: {milestone.get("label", "Milestone")}!',
        target_members=_PARENT_NOTIFICATION_TARGETS,
        created_by=actor_name,
        payload={
            'reminder_key': key,
            'child_name': child_name,
            'stars': stars,
            'milestone': milestone.get('label', ''),
        },
        channels=['in_app', 'mobile'],
    )


def _get_or_create_reward_profile(child_name: str, actor: str) -> KidRewardProfile | None:
    profile = KidRewardProfile.query.filter_by(child_name=child_name).first()
    if profile:
        return profile
    _ensure_kid_reward_profiles(actor)
    return KidRewardProfile.query.filter_by(child_name=child_name).first()


def _apply_star_delta(*, child_name: str, delta: int, actor: str, reason: str | None) -> tuple[bool, str, list[dict]]:
    profile = _get_or_create_reward_profile(child_name, actor)
    if not profile:
        return False, 'Could not load reward profile', []

    previous = max(0, int(profile.stars or 0))
    profile.stars = max(0, previous + int(delta))
    milestones_reached = _reward_milestones_crossed(previous, profile.stars)
    profile.updated_by = actor
    db.session.add(profile)
    db.session.add(KidRewardEvent(
        child_name=child_name,
        delta=int(delta),
        reason=reason,
        created_by=actor,
    ))
    _publish_reward_event_notification(
        child_name=child_name,
        delta=int(delta),
        actor_name=actor,
        reason=reason,
    )
    for milestone in milestones_reached:
        _publish_kid_reward_milestone_notification(child_name, profile.stars, milestone, actor)
    return True, '', milestones_reached


def _kid_reward_analytics_payload() -> dict:
    eastern = ZoneInfo('America/New_York')
    now_eastern = datetime.now(timezone.utc).astimezone(eastern)
    today_eastern = now_eastern.date()
    week_start = today_eastern - timedelta(days=today_eastern.weekday())
    week_end = week_start + timedelta(days=6)
    week_start_utc = datetime.combine(week_start, time(0, 0), tzinfo=eastern).astimezone(timezone.utc).replace(tzinfo=None)
    next_week_start_utc = datetime.combine(week_start + timedelta(days=7), time(0, 0), tzinfo=eastern).astimezone(timezone.utc).replace(tzinfo=None)
    trend_start_day = today_eastern - timedelta(days=27)
    trend_start_utc = datetime.combine(trend_start_day, time(0, 0), tzinfo=eastern).astimezone(timezone.utc).replace(tzinfo=None)

    weekly_events = (
        KidRewardEvent.query
        .filter(KidRewardEvent.created_at >= week_start_utc)
        .filter(KidRewardEvent.created_at < next_week_start_utc)
        .order_by(KidRewardEvent.created_at.desc(), KidRewardEvent.id.desc())
        .all()
    )
    trend_events = (
        KidRewardEvent.query
        .filter(KidRewardEvent.created_at >= trend_start_utc)
        .order_by(KidRewardEvent.created_at.asc(), KidRewardEvent.id.asc())
        .all()
    )
    recent_events = (
        KidRewardEvent.query
        .order_by(KidRewardEvent.created_at.desc(), KidRewardEvent.id.desc())
        .limit(120)
        .all()
    )

    weekly_awarded = sum(max(0, int(row.delta or 0)) for row in weekly_events)
    weekly_taken = sum(abs(min(0, int(row.delta or 0))) for row in weekly_events)
    by_child = []
    for child_name in _KID_MEMBERS:
        child_events = [row for row in weekly_events if sanitize_text(row.child_name or '').strip() == child_name]
        by_child.append({
            'child_name': child_name,
            'awarded': sum(max(0, int(row.delta or 0)) for row in child_events),
            'taken': sum(abs(min(0, int(row.delta or 0))) for row in child_events),
        })

    consequence_reason_map: dict[str, dict] = {}
    for row in weekly_events:
        delta = int(row.delta or 0)
        if delta >= 0:
            continue
        reason = sanitize_text(row.reason or '').strip()[:256] or 'Consequence recorded'
        item = consequence_reason_map.get(reason)
        if not item:
            item = {'reason': reason, 'count': 0, 'stars_taken': 0}
            consequence_reason_map[reason] = item
        item['count'] += 1
        item['stars_taken'] += abs(delta)
    weekly_consequence_reasons = sorted(
        consequence_reason_map.values(),
        key=lambda row: (-int(row['stars_taken']), -int(row['count']), row['reason']),
    )[:10]

    trend_by_day = {}
    for offset in range(28):
        day = trend_start_day + timedelta(days=offset)
        trend_by_day[day.isoformat()] = {'date': day.isoformat(), 'awarded': 0, 'taken': 0}
    for row in trend_events:
        created_at = row.created_at
        if not isinstance(created_at, datetime):
            continue
        day_key = created_at.replace(tzinfo=timezone.utc).astimezone(eastern).date().isoformat()
        if day_key not in trend_by_day:
            continue
        delta = int(row.delta or 0)
        if delta >= 0:
            trend_by_day[day_key]['awarded'] += delta
        else:
            trend_by_day[day_key]['taken'] += abs(delta)
    trend_daily = [trend_by_day[key] for key in sorted(trend_by_day.keys())]

    return {
        'weekly_summary': {
            'week_start': week_start.isoformat(),
            'week_end': week_end.isoformat(),
            'week_label': f"Week of {week_start.strftime('%b %d')}–{week_end.strftime('%b %d')}",
            'awarded_total': weekly_awarded,
            'taken_total': weekly_taken,
            'by_child': by_child,
            'consequence_reasons': weekly_consequence_reasons,
        },
        'behavior_trend': {
            'range_start': trend_start_day.isoformat(),
            'range_end': today_eastern.isoformat(),
            'daily': trend_daily,
        },
        'event_history': [{
            'id': row.id,
            'child_name': row.child_name,
            'delta': int(row.delta or 0),
            'reason': sanitize_text(row.reason or '').strip()[:256] or '',
            'created_by': row.created_by,
            'created_at': row.created_at.isoformat() if row.created_at else '',
        } for row in recent_events],
    }


def _kid_reward_state_payload(actor_name: str) -> dict:
    _ensure_default_kid_reward_rules(actor_name)
    _ensure_default_kid_reward_options(actor_name)
    profiles = _ensure_kid_reward_profiles(actor_name)
    settings = _get_or_create_kid_reward_settings(actor_name)
    all_rules = (
        KidRewardRule.query
        .filter(KidRewardRule.active.is_(True))
        .order_by(KidRewardRule.behavior_kind.asc(), KidRewardRule.sort_order.asc(), KidRewardRule.id.asc())
        .all()
    )
    rules = [row for row in all_rules if sanitize_text(row.behavior_kind or '').strip().lower() in {'do', 'dont'}]
    reward_options_by_child = _reward_options_by_child()
    reward_catalog_options = _reward_catalog_options()
    analytics = _kid_reward_analytics_payload()
    pending_grants = (
        KidRewardPendingGrant.query
        .filter_by(claimed=False)
        .order_by(KidRewardPendingGrant.created_at.asc(), KidRewardPendingGrant.id.asc())
        .all()
    )
    pending_by_child: dict[str, list[dict]] = {child: [] for child in _KID_MEMBERS}
    for row in pending_grants:
        child_name = sanitize_text(row.child_name or '').strip()
        if child_name not in pending_by_child:
            continue
        pending_by_child[child_name].append({
            'id': row.id,
            'child_name': child_name,
            'stars_awarded': max(1, min(10, int(row.stars_awarded or 1))),
            'reason': sanitize_text(row.reason or '').strip()[:256],
            'dispensed_by': sanitize_text(row.dispensed_by or '').strip() or 'Parent',
            'created_at': row.created_at.isoformat() if row.created_at else '',
        })
    entries = []
    for profile in profiles:
        child_name = sanitize_text(profile.child_name or '').strip()
        progress = _kid_reward_progress(profile.stars, reward_options_by_child.get(child_name) or [])
        pending_for_child = pending_by_child.get(child_name) or []
        entries.append({
            'child_name': child_name,
            'stars': max(0, int(profile.stars or 0)),
            'progress': progress,
            'pending_stars': sum(max(1, int(item.get('stars_awarded') or 1)) for item in pending_for_child),
            'pending_grants': pending_for_child,
        })
    redemptions = (
        KidRewardRedemption.query
        .order_by(KidRewardRedemption.created_at.desc(), KidRewardRedemption.id.desc())
        .limit(20)
        .all()
    )
    timeouts = (
        KidRewardTimeout.query
        .order_by(KidRewardTimeout.created_at.desc(), KidRewardTimeout.id.desc())
        .limit(20)
        .all()
    )
    repairs = (
        KidRewardRepairTask.query
        .order_by(KidRewardRepairTask.completed.asc(), KidRewardRepairTask.created_at.desc(), KidRewardRepairTask.id.desc())
        .limit(30)
        .all()
    )
    parent_action_rows = (
        KidRewardParentActionTask.query
        .order_by(KidRewardParentActionTask.created_at.desc(), KidRewardParentActionTask.id.desc())
        .limit(30)
        .all()
    )
    pending_repair_children = {
        row.child_name
        for row in KidRewardRepairTask.query.filter_by(completed=False).all()
        if sanitize_text(row.child_name or '').strip() in _KID_MEMBERS
    }
    return {
        'children': entries,
        'rules': [{
            'id': row.id,
            'behavior_kind': row.behavior_kind,
            'title': row.title,
            'points': int(row.points or 0),
            'emoji': row.emoji or '⭐',
            'sort_order': int(row.sort_order or 0),
        } for row in rules],
        'milestones': list(_KID_REWARD_MILESTONES),
        'reward_options_by_child': reward_options_by_child,
        'reward_options': reward_catalog_options,
        'weekly_summary': analytics['weekly_summary'],
        'behavior_trend': analytics['behavior_trend'],
        'event_history': analytics['event_history'],
        'pending_star_grants': [item for rows in pending_by_child.values() for item in rows],
        'settings': {
            'confetti_enabled': bool(settings.confetti_enabled),
            'sounds_enabled': bool(settings.sounds_enabled),
            'timeout_default_minutes': max(1, int(settings.timeout_default_minutes or 5)),
            'timeout_default_star_loss': max(0, int(settings.timeout_default_star_loss or 1)),
            'timeout_default_reward_minutes_loss': max(0, int(settings.timeout_default_reward_minutes_loss or 10)),
        },
        'recent_redemptions': [{
            'id': row.id,
            'child_name': row.child_name,
            'reward_label': row.reward_label,
            'stars_spent': int(row.stars_spent or 0),
            'redeemed_by': row.redeemed_by,
            'created_at': row.created_at.isoformat() if row.created_at else '',
        } for row in redemptions],
        'recent_timeouts': [{
            'id': row.id,
            'child_name': row.child_name,
            'reason': row.reason,
            'timeout_minutes': int(row.timeout_minutes or 0),
            'stars_deducted': int(row.stars_deducted or 0),
            'reward_minutes_lost': int(row.reward_minutes_lost or 0),
            'created_by': row.created_by,
            'created_at': row.created_at.isoformat() if row.created_at else '',
        } for row in timeouts],
        'repair_tasks': [{
            'id': row.id,
            'timeout_id': row.timeout_id,
            'child_name': row.child_name,
            'task_text': row.task_text,
            'completed': bool(row.completed),
            'completed_by': row.completed_by or '',
            'completed_at': row.completed_at.isoformat() if row.completed_at else '',
            'created_at': row.created_at.isoformat() if row.created_at else '',
        } for row in repairs],
        'parent_action_tasks': [{
            'id': row.id,
            'redemption_id': row.redemption_id,
            'child_name': row.child_name,
            'reward_label': row.reward_label,
            'parent_name': row.parent_name,
            'chore_id': row.chore_id,
            'done': bool(getattr(db.session.get(Chore, row.chore_id), 'done', False)) if row.chore_id else False,
            'created_at': row.created_at.isoformat() if row.created_at else '',
        } for row in parent_action_rows],
        'child_locks': {
            child_name: child_name in pending_repair_children
            for child_name in _KID_MEMBERS
        },
        'pending_repair_message': 'Complete the repair task before rewards and privilege redemptions resume.',
        'discipline_methods': list(_DISCIPLINE_METHODS),
        'can_manage': _can_manage_kid_rewards(actor_name),
    }


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
    bedtime_child_assignments = _bedtime_child_assignments_for_reader(bedtime_reader)
    completed_at = None
    if state.updated_at:
        completed_at = state.updated_at.replace(microsecond=0).isoformat() + 'Z'
    next_bath_date = _next_bath_date(state, today)
    overdue_days = max(0, (today - next_bath_date).days)

    return {
        'bedtime': {
            'for_date': today.isoformat(),
            'reader': bedtime_reader,
            'book_picker': bedtime_child_assignments['book_picker'],
            'movie_picker': bedtime_child_assignments['movie_picker'],
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
    current_member_name = _current_actor_name()
    current_member_row = FamilyMember.query.filter_by(name=current_member_name).first()
    routine_state = _get_or_create_household_routine_state(current_member_name)
    routines = _build_routine_payload(routine_state)
    rewards_state = _kid_reward_state_payload(current_member_name)
    parent_helper_summary = _parent_helper_summary(current_member_name)
    current_member_status_text = ''
    if current_member_row and (current_member_row.status or '').strip():
        current_member_status_text = current_member_row.status
    can_manage_kid_rewards = _can_manage_kid_rewards(current_member_name)
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
        rewards_summary=rewards_state,
        can_manage_kid_rewards=can_manage_kid_rewards,
        parent_helper_summary=parent_helper_summary,
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
    actor = _current_actor_name()
    row = _get_or_create_household_routine_state(actor)
    return jsonify({'ok': True, 'routines': _build_routine_payload(row)})


_GOLD_STAR_PREVIEW_SESSION_KEY = 'gold_star_preview_state'
_GOLD_STAR_PREVIEW_CHILDREN = ('Avery', 'Harley')
_GOLD_STAR_PREVIEW_MAX_COST = 50
_GOLD_STAR_PREVIEW_IMAGES = (
    'dessert',
    'game',
    'weekend',
    'star',
    'treat',
)
_GOLD_STAR_PREVIEW_CHARACTER_STATES = (
    'idle',
    'noticeNewStar',
    'tapReaction',
    'collecting',
    'celebrate',
    'rewardApproved',
)
_GOLD_STAR_PREVIEW_VIEWS = {
    'parent-overview',
    'parent-rewards',
    'parent-requests',
    'parent-history',
    'child-board',
    'child-rewards',
    'child-story',
}


def _can_access_gold_star_preview(actor_name: str) -> bool:
    clean_actor = sanitize_text(actor_name or '').strip().lower()
    if not clean_actor:
        return False
    if _can_manage_kid_rewards(actor_name):
        return True
    admin_name = sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator')).strip().lower()
    return bool(admin_name and clean_actor == admin_name)


def _gold_star_preview_utc_now_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()


def _gold_star_preview_clean_child_name(value: str | None) -> str:
    candidate = sanitize_text(value or '').strip().lower()
    for child in _GOLD_STAR_PREVIEW_CHILDREN:
        if candidate == child.lower():
            return child
    return ''


def _gold_star_preview_get_child_bucket(state: dict, child_name: str) -> dict:
    children = state.get('children')
    if not isinstance(children, dict):
        children = {}
    bucket = children.get(child_name)
    if not isinstance(bucket, dict):
        bucket = {
            'earned': 0,
            'available': 0,
            'spent': 0,
            'waiting_awards': [],
        }
    children[child_name] = bucket
    state['children'] = children
    return bucket


def _gold_star_preview_append_history(
    state: dict,
    *,
    event_type: str,
    actor: str,
    child_name: str = '',
    stars_delta: int = 0,
    message: str,
    context: str = '',
    reward_snapshot: dict | None = None,
) -> None:
    history = state.get('history')
    if not isinstance(history, list):
        history = []
    next_id = int(state.get('next_history_id') or 1)
    entry = {
        'id': next_id,
        'event_type': sanitize_text(event_type or 'event').strip()[:48] or 'event',
        'actor': sanitize_text(actor or '').strip()[:64],
        'child_name': sanitize_text(child_name or '').strip(),
        'stars_delta': int(stars_delta or 0),
        'message': sanitize_text(message or '').strip()[:220] or 'Updated demo state',
        'context': sanitize_text(context or '').strip()[:220],
        'created_at': _gold_star_preview_utc_now_iso(),
    }
    if isinstance(reward_snapshot, dict):
        entry['reward'] = {
            'id': int(reward_snapshot.get('id') or 0),
            'name': sanitize_text(reward_snapshot.get('name') or '').strip(),
            'cost': int(reward_snapshot.get('cost') or 0),
            'image_ref': sanitize_text(reward_snapshot.get('image_ref') or '').strip(),
            'active': bool(reward_snapshot.get('active')),
        }
    history.insert(0, entry)
    state['history'] = history[:240]
    state['next_history_id'] = next_id + 1


def _gold_star_preview_default_state(actor_name: str) -> dict:
    actor = sanitize_text(actor_name or '').strip() or 'Preview'
    state = {
        'preview_mode': True,
        'children': {
            'Avery': {
                'earned': 32,
                'available': 7,
                'spent': 25,
                'waiting_awards': [],
            },
            'Harley': {
                'earned': 24,
                'available': 4,
                'spent': 20,
                'waiting_awards': [],
            },
        },
        'catalog': [
            {
                'id': 1,
                'name': 'Choose dessert',
                'image_ref': 'dessert',
                'cost': 5,
                'eligible_children': ['Avery', 'Harley'],
                'active': True,
                'archived_at': '',
                'updated_at': _gold_star_preview_utc_now_iso(),
            },
            {
                'id': 2,
                'name': 'Pick family game',
                'image_ref': 'game',
                'cost': 10,
                'eligible_children': ['Avery', 'Harley'],
                'active': True,
                'archived_at': '',
                'updated_at': _gold_star_preview_utc_now_iso(),
            },
            {
                'id': 3,
                'name': 'Choose a weekend activity',
                'image_ref': 'weekend',
                'cost': 20,
                'eligible_children': ['Avery', 'Harley'],
                'active': True,
                'archived_at': '',
                'updated_at': _gold_star_preview_utc_now_iso(),
            },
        ],
        'requests': [],
        'delivery_queue': [],
        'history': [],
        'next_history_id': 1,
        'next_award_id': 1,
        'next_reward_id': 4,
        'next_request_id': 1,
        'next_delivery_id': 1,
        'motion_enabled': True,
        'sound_available': False,
        'character_asset_label': 'Temporary procedural star (replaceable)',
        'updated_by': actor,
        'updated_at': _gold_star_preview_utc_now_iso(),
    }
    _gold_star_preview_append_history(
        state,
        event_type='preview',
        actor=actor,
        message='Preview reset with Avery and Harley demo baselines.',
        context='Session-isolated prototype state',
    )
    return state


def _gold_star_preview_waiting_count_for_child(state: dict, child_name: str) -> int:
    bucket = _gold_star_preview_get_child_bucket(state, child_name)
    waiting = bucket.get('waiting_awards')
    if not isinstance(waiting, list):
        waiting = []
    return len([row for row in waiting if isinstance(row, dict) and not bool(row.get('collected'))])


def _gold_star_preview_find_reward(state: dict, reward_id: int) -> dict | None:
    catalog = state.get('catalog')
    if not isinstance(catalog, list):
        return None
    for reward in catalog:
        if not isinstance(reward, dict):
            continue
        if int(reward.get('id') or 0) == int(reward_id):
            return reward
    return None


def _gold_star_preview_reconcile_requests(state: dict, actor_name: str) -> int:
    requests = state.get('requests')
    if not isinstance(requests, list):
        requests = []
    invalidated = 0
    for req in requests:
        if not isinstance(req, dict):
            continue
        if sanitize_text(req.get('status') or '').strip() != 'pending':
            continue
        reward = _gold_star_preview_find_reward(state, int(req.get('reward_id') or 0))
        reason = ''
        if not reward:
            reason = 'Reward is no longer available.'
        elif not bool(reward.get('active')):
            reason = 'Reward was archived and is no longer requestable.'
        else:
            eligible = reward.get('eligible_children') if isinstance(reward.get('eligible_children'), list) else []
            child_name = sanitize_text(req.get('child_name') or '').strip()
            if child_name not in eligible:
                reason = 'Child is no longer eligible for this reward.'
            else:
                current_cost = int(reward.get('cost') or 0)
                requested_cost = int(req.get('requested_cost') or 0)
                if current_cost != requested_cost:
                    reason = f'Reward price changed from {requested_cost} to {current_cost}.'
        if not reason:
            continue
        invalidated += 1
        req['status'] = 'invalidated'
        req['status_message'] = reason
        req['resolved_at'] = _gold_star_preview_utc_now_iso()
        req['resolved_by'] = sanitize_text(actor_name or '').strip()
        _gold_star_preview_append_history(
            state,
            event_type='invalidated',
            actor=actor_name,
            child_name=sanitize_text(req.get('child_name') or '').strip(),
            message='Purchase request invalidated',
            context=reason,
            reward_snapshot={
                'id': req.get('reward_id'),
                'name': req.get('reward_name'),
                'cost': req.get('requested_cost'),
                'image_ref': req.get('image_ref'),
                'active': bool(reward and reward.get('active')),
            },
        )
    state['requests'] = requests
    return invalidated


def _gold_star_preview_normalize_state(raw_state: dict, actor_name: str) -> dict:
    state = raw_state if isinstance(raw_state, dict) else _gold_star_preview_default_state(actor_name)
    state['preview_mode'] = True
    actor = sanitize_text(actor_name or '').strip() or sanitize_text(state.get('updated_by') or 'Preview').strip() or 'Preview'

    children = state.get('children')
    if not isinstance(children, dict):
        children = {}
    for child_name in _GOLD_STAR_PREVIEW_CHILDREN:
        bucket = children.get(child_name)
        if not isinstance(bucket, dict):
            bucket = {}
        try:
            bucket['available'] = max(0, int(bucket.get('available') or 0))
        except (TypeError, ValueError):
            bucket['available'] = 0
        try:
            bucket['spent'] = max(0, int(bucket.get('spent') or 0))
        except (TypeError, ValueError):
            bucket['spent'] = 0
        waiting_awards = bucket.get('waiting_awards')
        if not isinstance(waiting_awards, list):
            waiting_awards = []
        normalized_awards = []
        for award in waiting_awards:
            if not isinstance(award, dict):
                continue
            award_id = int(award.get('id') or 0)
            if award_id <= 0:
                continue
            normalized_awards.append({
                'id': award_id,
                'child_name': child_name,
                'note': sanitize_text(award.get('note') or '').strip()[:140],
                'awarded_by': sanitize_text(award.get('awarded_by') or '').strip()[:64],
                'awarded_at': sanitize_text(award.get('awarded_at') or '').strip(),
                'collected': bool(award.get('collected')),
                'collected_by': sanitize_text(award.get('collected_by') or '').strip()[:64],
                'collected_at': sanitize_text(award.get('collected_at') or '').strip(),
            })
        normalized_awards.sort(key=lambda row: int(row.get('id') or 0))
        bucket['waiting_awards'] = normalized_awards
        waiting_count = len([row for row in normalized_awards if not bool(row.get('collected'))])
        invariant = int(bucket['available']) + int(bucket['spent']) + waiting_count
        try:
            earned = int(bucket.get('earned', invariant))
        except (TypeError, ValueError):
            earned = invariant
        bucket['earned'] = invariant if earned != invariant else earned
        children[child_name] = bucket
    state['children'] = children

    max_award_id = int(state.get('next_award_id') or 1)
    for child_name in _GOLD_STAR_PREVIEW_CHILDREN:
        for award in state['children'][child_name]['waiting_awards']:
            max_award_id = max(max_award_id, int(award.get('id') or 0) + 1)
    state['next_award_id'] = max(max_award_id, 1)

    catalog = state.get('catalog')
    if not isinstance(catalog, list):
        catalog = []
    normalized_catalog = []
    max_reward_id = int(state.get('next_reward_id') or 1)
    for reward in catalog:
        if not isinstance(reward, dict):
            continue
        reward_id = int(reward.get('id') or 0)
        if reward_id <= 0:
            continue
        max_reward_id = max(max_reward_id, reward_id + 1)
        raw_children = reward.get('eligible_children')
        eligible = []
        if isinstance(raw_children, list):
            for child_candidate in raw_children:
                clean = _gold_star_preview_clean_child_name(str(child_candidate or ''))
                if clean and clean not in eligible:
                    eligible.append(clean)
        try:
            reward_cost = int(reward.get('cost') or 0)
        except (TypeError, ValueError):
            reward_cost = 1
        normalized_catalog.append({
            'id': reward_id,
            'name': sanitize_text(reward.get('name') or '').strip()[:140] or 'Reward',
            'image_ref': sanitize_text(reward.get('image_ref') or 'star').strip()[:64] or 'star',
            'cost': max(1, min(_GOLD_STAR_PREVIEW_MAX_COST, reward_cost)),
            'eligible_children': eligible if eligible else list(_GOLD_STAR_PREVIEW_CHILDREN),
            'active': bool(reward.get('active', True)),
            'archived_at': sanitize_text(reward.get('archived_at') or '').strip(),
            'updated_at': sanitize_text(reward.get('updated_at') or '').strip() or _gold_star_preview_utc_now_iso(),
        })
    normalized_catalog.sort(key=lambda row: int(row.get('id') or 0))
    state['catalog'] = normalized_catalog
    state['next_reward_id'] = max(max_reward_id, 1)

    requests = state.get('requests')
    if not isinstance(requests, list):
        requests = []
    normalized_requests = []
    max_request_id = int(state.get('next_request_id') or 1)
    for req in requests:
        if not isinstance(req, dict):
            continue
        request_id = int(req.get('id') or 0)
        if request_id <= 0:
            continue
        max_request_id = max(max_request_id, request_id + 1)
        child_name = _gold_star_preview_clean_child_name(req.get('child_name') or '')
        if not child_name:
            continue
        status = sanitize_text(req.get('status') or 'pending').strip()
        if status not in {'pending', 'approved', 'declined', 'invalidated'}:
            status = 'pending'
        normalized_requests.append({
            'id': request_id,
            'child_name': child_name,
            'reward_id': int(req.get('reward_id') or 0),
            'reward_name': sanitize_text(req.get('reward_name') or '').strip()[:140],
            'image_ref': sanitize_text(req.get('image_ref') or 'star').strip()[:64] or 'star',
            'requested_cost': int(req.get('requested_cost') or 0),
            'status': status,
            'status_message': sanitize_text(req.get('status_message') or '').strip()[:220],
            'requested_by': sanitize_text(req.get('requested_by') or '').strip()[:64],
            'requested_at': sanitize_text(req.get('requested_at') or '').strip(),
            'resolved_by': sanitize_text(req.get('resolved_by') or '').strip()[:64],
            'resolved_at': sanitize_text(req.get('resolved_at') or '').strip(),
            'delivery_id': int(req.get('delivery_id') or 0),
        })
    normalized_requests.sort(key=lambda row: int(row.get('id') or 0), reverse=True)
    state['requests'] = normalized_requests
    state['next_request_id'] = max(max_request_id, 1)

    delivery_queue = state.get('delivery_queue')
    if not isinstance(delivery_queue, list):
        delivery_queue = []
    normalized_delivery = []
    max_delivery_id = int(state.get('next_delivery_id') or 1)
    for delivery in delivery_queue:
        if not isinstance(delivery, dict):
            continue
        delivery_id = int(delivery.get('id') or 0)
        if delivery_id <= 0:
            continue
        max_delivery_id = max(max_delivery_id, delivery_id + 1)
        child_name = _gold_star_preview_clean_child_name(delivery.get('child_name') or '')
        if not child_name:
            continue
        status = sanitize_text(delivery.get('status') or 'ready').strip()
        if status not in {'ready', 'delivered'}:
            status = 'ready'
        normalized_delivery.append({
            'id': delivery_id,
            'request_id': int(delivery.get('request_id') or 0),
            'child_name': child_name,
            'reward_name': sanitize_text(delivery.get('reward_name') or '').strip()[:140],
            'reward_cost': int(delivery.get('reward_cost') or 0),
            'image_ref': sanitize_text(delivery.get('image_ref') or 'star').strip()[:64] or 'star',
            'status': status,
            'approved_by': sanitize_text(delivery.get('approved_by') or '').strip()[:64],
            'approved_at': sanitize_text(delivery.get('approved_at') or '').strip(),
            'delivered_by': sanitize_text(delivery.get('delivered_by') or '').strip()[:64],
            'delivered_at': sanitize_text(delivery.get('delivered_at') or '').strip(),
        })
    normalized_delivery.sort(key=lambda row: int(row.get('id') or 0), reverse=True)
    state['delivery_queue'] = normalized_delivery
    state['next_delivery_id'] = max(max_delivery_id, 1)

    history = state.get('history')
    if not isinstance(history, list):
        history = []
    normalized_history = []
    max_history_id = int(state.get('next_history_id') or 1)
    for row in history:
        if not isinstance(row, dict):
            continue
        event_id = int(row.get('id') or 0)
        if event_id <= 0:
            continue
        max_history_id = max(max_history_id, event_id + 1)
        normalized_history.append({
            'id': event_id,
            'event_type': sanitize_text(row.get('event_type') or 'event').strip()[:48] or 'event',
            'actor': sanitize_text(row.get('actor') or '').strip()[:64],
            'child_name': sanitize_text(row.get('child_name') or '').strip(),
            'stars_delta': int(row.get('stars_delta') or 0),
            'message': sanitize_text(row.get('message') or '').strip()[:220],
            'context': sanitize_text(row.get('context') or '').strip()[:220],
            'created_at': sanitize_text(row.get('created_at') or '').strip(),
            'reward': row.get('reward') if isinstance(row.get('reward'), dict) else None,
        })
    normalized_history.sort(key=lambda row: int(row.get('id') or 0), reverse=True)
    state['history'] = normalized_history[:240]
    state['next_history_id'] = max(max_history_id, 1)

    state['motion_enabled'] = bool(state.get('motion_enabled', True))
    state['sound_available'] = False
    state['character_asset_label'] = sanitize_text(state.get('character_asset_label') or 'Temporary procedural star (replaceable)').strip()[:140]

    _gold_star_preview_reconcile_requests(state, actor)

    state['updated_by'] = actor
    state['updated_at'] = _gold_star_preview_utc_now_iso()
    return state


def _gold_star_preview_get_state(actor_name: str, *, reset: bool = False) -> dict:
    raw = session.get(_GOLD_STAR_PREVIEW_SESSION_KEY)
    if reset or not isinstance(raw, dict):
        state = _gold_star_preview_default_state(actor_name)
    else:
        state = raw
    normalized = _gold_star_preview_normalize_state(state, actor_name)
    session[_GOLD_STAR_PREVIEW_SESSION_KEY] = normalized
    session.modified = True
    return normalized


def _gold_star_preview_save_state(state: dict, actor_name: str) -> dict:
    normalized = _gold_star_preview_normalize_state(state, actor_name)
    session[_GOLD_STAR_PREVIEW_SESSION_KEY] = normalized
    session.modified = True
    return normalized


def _gold_star_preview_child_payload(state: dict, child_name: str) -> dict:
    bucket = _gold_star_preview_get_child_bucket(state, child_name)
    pending_awards = [
        award for award in bucket.get('waiting_awards', [])
        if isinstance(award, dict) and not bool(award.get('collected'))
    ]
    active_rewards = []
    for reward in state.get('catalog', []):
        if not isinstance(reward, dict) or not bool(reward.get('active')):
            continue
        eligible = reward.get('eligible_children') if isinstance(reward.get('eligible_children'), list) else []
        if child_name not in eligible:
            continue
        remaining = int(bucket.get('available') or 0) - int(reward.get('cost') or 0)
        active_rewards.append({
            'id': int(reward.get('id') or 0),
            'name': sanitize_text(reward.get('name') or '').strip(),
            'image_ref': sanitize_text(reward.get('image_ref') or 'star').strip() or 'star',
            'cost': int(reward.get('cost') or 0),
            'affordable': remaining >= 0,
            'remaining_after_request': remaining,
        })
    child_requests = [
        req for req in state.get('requests', [])
        if isinstance(req, dict) and sanitize_text(req.get('child_name') or '').strip() == child_name
    ]
    pending_request = next((req for req in child_requests if sanitize_text(req.get('status') or '').strip() == 'pending'), None)
    latest_delivery = next((
        row for row in state.get('delivery_queue', [])
        if isinstance(row, dict) and sanitize_text(row.get('child_name') or '').strip() == child_name
    ), None)
    history_rows = [
        row for row in state.get('history', [])
        if isinstance(row, dict) and sanitize_text(row.get('child_name') or '').strip() in {'', child_name}
    ][:80]
    waiting_count = len(pending_awards)
    return {
        'name': child_name,
        'earned': int(bucket.get('earned') or 0),
        'available': int(bucket.get('available') or 0),
        'spent': int(bucket.get('spent') or 0),
        'waiting_count': waiting_count,
        'waiting_awards': [
            {
                'id': int(award.get('id') or 0),
                'note': sanitize_text(award.get('note') or '').strip(),
                'awarded_by': sanitize_text(award.get('awarded_by') or '').strip(),
                'awarded_at': sanitize_text(award.get('awarded_at') or '').strip(),
            }
            for award in pending_awards
        ],
        'board_stars': int(bucket.get('available') or 0),
        'invariant_ok': int(bucket.get('earned') or 0) == (int(bucket.get('available') or 0) + waiting_count + int(bucket.get('spent') or 0)),
        'active_rewards': active_rewards,
        'pending_request': pending_request,
        'latest_delivery': latest_delivery,
        'history': history_rows,
    }


def _gold_star_preview_payload(state: dict) -> dict:
    child_payloads = {child_name: _gold_star_preview_child_payload(state, child_name) for child_name in _GOLD_STAR_PREVIEW_CHILDREN}
    requests = state.get('requests') if isinstance(state.get('requests'), list) else []
    delivery_queue = state.get('delivery_queue') if isinstance(state.get('delivery_queue'), list) else []
    pending_requests = [row for row in requests if isinstance(row, dict) and sanitize_text(row.get('status') or '').strip() == 'pending']
    ready_delivery = [row for row in delivery_queue if isinstance(row, dict) and sanitize_text(row.get('status') or '').strip() == 'ready']
    return {
        'preview_mode': True,
        'children': child_payloads,
        'catalog': [
            {
                'id': int(reward.get('id') or 0),
                'name': sanitize_text(reward.get('name') or '').strip(),
                'image_ref': sanitize_text(reward.get('image_ref') or '').strip(),
                'cost': int(reward.get('cost') or 0),
                'eligible_children': reward.get('eligible_children') if isinstance(reward.get('eligible_children'), list) else [],
                'active': bool(reward.get('active')),
                'archived_at': sanitize_text(reward.get('archived_at') or '').strip(),
                'updated_at': sanitize_text(reward.get('updated_at') or '').strip(),
            }
            for reward in state.get('catalog', [])
            if isinstance(reward, dict)
        ],
        'requests': requests,
        'delivery_queue': delivery_queue,
        'history': state.get('history', []),
        'motion_enabled': bool(state.get('motion_enabled', True)),
        'sound_available': False,
        'character': {
            'asset_label': sanitize_text(state.get('character_asset_label') or '').strip(),
            'temporary_asset': True,
            'supported_states': list(_GOLD_STAR_PREVIEW_CHARACTER_STATES),
        },
        'overview': {
            'pending_requests': len(pending_requests),
            'ready_to_deliver': len(ready_delivery),
        },
        'notes': {
            'prototype_scope': 'Session-isolated preview state is not a production concurrency guarantee.',
            'production_dependency': 'Production double-spend protection requires database transactions and shared persistence.',
            'cost_bound': _GOLD_STAR_PREVIEW_MAX_COST,
        },
    }


def _gold_star_preview_require_parent(actor_name: str):
    if _can_access_gold_star_preview(actor_name):
        return None
    return jsonify({'ok': False, 'error': 'Only Rachel, Mykal, or an admin can access this preview.'}), 403


def _gold_star_preview_resolve_request(state: dict, request_id: int) -> dict | None:
    for req in state.get('requests', []):
        if not isinstance(req, dict):
            continue
        if int(req.get('id') or 0) == int(request_id):
            return req
    return None


@main_bp.route('/gold-stars', methods=['GET'])
def gold_stars_dashboard():
    actor = _current_actor_name()
    kid_view = str(request.args.get('kid_view') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    return render_template(
        'rewards.html',
        rewards_state=_kid_reward_state_payload(actor),
        actor_name=actor,
        kid_view=kid_view,
    )


@main_bp.route('/gold-stars/preview', methods=['GET'])
def gold_stars_preview():
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    state = _gold_star_preview_get_state(actor)
    initial_view = sanitize_text(request.args.get('view') or 'parent-overview').strip()
    if initial_view not in _GOLD_STAR_PREVIEW_VIEWS:
        initial_view = 'parent-overview'
    return render_template(
        'gold_stars_preview.html',
        preview_state=_gold_star_preview_payload(state),
        actor_name=actor,
        initial_view=initial_view,
        initial_child='Avery',
    )


@main_bp.route('/gold-stars/preview/child/<child_name>', methods=['GET'])
def gold_stars_preview_child(child_name: str):
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    resolved_child = _gold_star_preview_clean_child_name(child_name)
    if not resolved_child:
        resolved_child = 'Avery'
    state = _gold_star_preview_get_state(actor)
    initial_view = sanitize_text(request.args.get('view') or 'child-board').strip()
    if initial_view not in _GOLD_STAR_PREVIEW_VIEWS:
        initial_view = 'child-board'
    if not initial_view.startswith('child-'):
        initial_view = 'child-board'
    return render_template(
        'gold_stars_preview.html',
        preview_state=_gold_star_preview_payload(state),
        actor_name=actor,
        initial_view=initial_view,
        initial_child=resolved_child,
    )


@main_bp.route('/gold-stars/preview/character-lab', methods=['GET'])
def gold_stars_preview_character_lab():
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    return render_template('gold_star_character_lab.html', actor_name=actor)


@main_bp.route('/api/gold-stars/preview/state', methods=['GET'])
def api_gold_stars_preview_state():
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    state = _gold_star_preview_get_state(actor)
    return jsonify({'ok': True, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/reset', methods=['POST'])
def api_gold_stars_preview_reset():
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    state = _gold_star_preview_get_state(actor, reset=True)
    _gold_star_preview_append_history(
        state,
        event_type='preview',
        actor=actor,
        message='Parent reset the prototype state.',
        context='Reset affects preview session state only.',
    )
    state = _gold_star_preview_save_state(state, actor)
    return jsonify({'ok': True, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/preferences', methods=['POST'])
def api_gold_stars_preview_preferences():
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    data = request.get_json(silent=True) or {}
    state = _gold_star_preview_get_state(actor)
    if 'motion_enabled' in data:
        state['motion_enabled'] = bool(data.get('motion_enabled'))
    state = _gold_star_preview_save_state(state, actor)
    return jsonify({'ok': True, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/awards', methods=['POST'])
def api_gold_stars_preview_award_star():
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    data = request.get_json(silent=True) or {}
    child_name = _gold_star_preview_clean_child_name(data.get('child_name') or '')
    if not child_name:
        return jsonify({'ok': False, 'error': 'Choose Avery or Harley.'}), 400
    note = sanitize_text(data.get('note') or '').strip()[:140]
    state = _gold_star_preview_get_state(actor)
    award_id = int(state.get('next_award_id') or 1)
    state['next_award_id'] = award_id + 1
    bucket = _gold_star_preview_get_child_bucket(state, child_name)
    waiting_awards = bucket.get('waiting_awards')
    if not isinstance(waiting_awards, list):
        waiting_awards = []
    waiting_awards.append({
        'id': award_id,
        'child_name': child_name,
        'note': note,
        'awarded_by': actor,
        'awarded_at': _gold_star_preview_utc_now_iso(),
        'collected': False,
        'collected_by': '',
        'collected_at': '',
    })
    bucket['waiting_awards'] = waiting_awards
    bucket['earned'] = int(bucket.get('earned') or 0) + 1
    state['children'][child_name] = bucket
    _gold_star_preview_append_history(
        state,
        event_type='awarded',
        actor=actor,
        child_name=child_name,
        stars_delta=1,
        message=f'{child_name} got a new waiting star.',
        context=note or 'No note',
    )
    state = _gold_star_preview_save_state(state, actor)
    return jsonify({'ok': True, 'award_id': award_id, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/awards/collect', methods=['POST'])
def api_gold_stars_preview_collect_star():
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    data = request.get_json(silent=True) or {}
    child_name = _gold_star_preview_clean_child_name(data.get('child_name') or '')
    if not child_name:
        return jsonify({'ok': False, 'error': 'Choose Avery or Harley.'}), 400
    try:
        award_id = int(data.get('award_id') or 0)
    except (TypeError, ValueError):
        award_id = 0
    if award_id <= 0:
        return jsonify({'ok': False, 'error': 'award_id is required'}), 400
    state = _gold_star_preview_get_state(actor)
    bucket = _gold_star_preview_get_child_bucket(state, child_name)
    waiting_awards = bucket.get('waiting_awards')
    if not isinstance(waiting_awards, list):
        waiting_awards = []
    target = next((award for award in waiting_awards if isinstance(award, dict) and int(award.get('id') or 0) == award_id), None)
    if not target:
        return jsonify({'ok': False, 'error': 'Waiting star not found'}), 404
    if bool(target.get('collected')):
        return jsonify({'ok': False, 'error': 'This waiting star was already collected.'}), 409
    target['collected'] = True
    target['collected_by'] = child_name
    target['collected_at'] = _gold_star_preview_utc_now_iso()
    bucket['available'] = int(bucket.get('available') or 0) + 1
    state['children'][child_name] = bucket
    _gold_star_preview_append_history(
        state,
        event_type='collected',
        actor=child_name,
        child_name=child_name,
        stars_delta=0,
        message=f'{child_name} collected a waiting star.',
        context=sanitize_text(target.get('note') or '').strip() or 'Collected',
    )
    state = _gold_star_preview_save_state(state, actor)
    return jsonify({'ok': True, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/catalog', methods=['POST'])
def api_gold_stars_preview_catalog_add():
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    data = request.get_json(silent=True) or {}
    reward_name = sanitize_text(data.get('name') or '').strip()
    if not reward_name:
        return jsonify({'ok': False, 'error': 'Reward name is required.'}), 400
    try:
        cost = int(data.get('cost'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': f'Cost must be a positive integer from 1 to {_GOLD_STAR_PREVIEW_MAX_COST}.'}), 400
    if cost < 1 or cost > _GOLD_STAR_PREVIEW_MAX_COST:
        return jsonify({'ok': False, 'error': f'Cost must be a positive integer from 1 to {_GOLD_STAR_PREVIEW_MAX_COST}.'}), 400
    image_ref = sanitize_text(data.get('image_ref') or 'star').strip() or 'star'
    if image_ref not in _GOLD_STAR_PREVIEW_IMAGES:
        image_ref = 'star'
    raw_children = data.get('eligible_children')
    eligible_children = []
    if isinstance(raw_children, list):
        for row in raw_children:
            clean = _gold_star_preview_clean_child_name(str(row or ''))
            if clean and clean not in eligible_children:
                eligible_children.append(clean)
    if not eligible_children:
        eligible_children = list(_GOLD_STAR_PREVIEW_CHILDREN)
    state = _gold_star_preview_get_state(actor)
    reward_id = int(state.get('next_reward_id') or 1)
    state['next_reward_id'] = reward_id + 1
    catalog = state.get('catalog')
    if not isinstance(catalog, list):
        catalog = []
    catalog.append({
        'id': reward_id,
        'name': reward_name[:140],
        'image_ref': image_ref,
        'cost': cost,
        'eligible_children': eligible_children,
        'active': bool(data.get('active', True)),
        'archived_at': '' if bool(data.get('active', True)) else _gold_star_preview_utc_now_iso(),
        'updated_at': _gold_star_preview_utc_now_iso(),
    })
    state['catalog'] = catalog
    _gold_star_preview_append_history(
        state,
        event_type='catalog',
        actor=actor,
        message=f'Added reward "{reward_name[:80]}".',
        context=f'Cost {cost} stars',
        reward_snapshot={'id': reward_id, 'name': reward_name, 'cost': cost, 'image_ref': image_ref, 'active': bool(data.get('active', True))},
    )
    state = _gold_star_preview_save_state(state, actor)
    return jsonify({'ok': True, 'reward_id': reward_id, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/catalog/<int:reward_id>', methods=['PUT'])
def api_gold_stars_preview_catalog_edit(reward_id: int):
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    data = request.get_json(silent=True) or {}
    state = _gold_star_preview_get_state(actor)
    reward = _gold_star_preview_find_reward(state, reward_id)
    if not reward:
        return jsonify({'ok': False, 'error': 'Reward not found'}), 404
    if 'name' in data:
        reward_name = sanitize_text(data.get('name') or '').strip()
        if not reward_name:
            return jsonify({'ok': False, 'error': 'Reward name is required.'}), 400
        reward['name'] = reward_name[:140]
    if 'cost' in data:
        try:
            cost = int(data.get('cost'))
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': f'Cost must be a positive integer from 1 to {_GOLD_STAR_PREVIEW_MAX_COST}.'}), 400
        if cost < 1 or cost > _GOLD_STAR_PREVIEW_MAX_COST:
            return jsonify({'ok': False, 'error': f'Cost must be a positive integer from 1 to {_GOLD_STAR_PREVIEW_MAX_COST}.'}), 400
        reward['cost'] = cost
    if 'image_ref' in data:
        image_ref = sanitize_text(data.get('image_ref') or '').strip()
        if image_ref in _GOLD_STAR_PREVIEW_IMAGES:
            reward['image_ref'] = image_ref
    if 'eligible_children' in data:
        raw_children = data.get('eligible_children')
        eligible = []
        if isinstance(raw_children, list):
            for row in raw_children:
                clean = _gold_star_preview_clean_child_name(str(row or ''))
                if clean and clean not in eligible:
                    eligible.append(clean)
        if not eligible:
            return jsonify({'ok': False, 'error': 'Select at least one eligible child.'}), 400
        reward['eligible_children'] = eligible
    if 'active' in data:
        reward['active'] = bool(data.get('active'))
        reward['archived_at'] = '' if reward['active'] else _gold_star_preview_utc_now_iso()
    reward['updated_at'] = _gold_star_preview_utc_now_iso()
    _gold_star_preview_append_history(
        state,
        event_type='catalog',
        actor=actor,
        message=f'Updated reward "{sanitize_text(reward.get("name") or "").strip()}".',
        context='Catalog edit',
        reward_snapshot=reward,
    )
    state = _gold_star_preview_save_state(state, actor)
    return jsonify({'ok': True, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/catalog/<int:reward_id>/archive', methods=['POST'])
def api_gold_stars_preview_catalog_archive(reward_id: int):
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    state = _gold_star_preview_get_state(actor)
    reward = _gold_star_preview_find_reward(state, reward_id)
    if not reward:
        return jsonify({'ok': False, 'error': 'Reward not found'}), 404
    if not bool(reward.get('active')):
        return jsonify({'ok': False, 'error': 'Reward is already archived.'}), 409
    reward['active'] = False
    reward['archived_at'] = _gold_star_preview_utc_now_iso()
    reward['updated_at'] = _gold_star_preview_utc_now_iso()
    _gold_star_preview_append_history(
        state,
        event_type='catalog',
        actor=actor,
        message=f'Archived reward "{sanitize_text(reward.get("name") or "").strip()}".',
        context='Reward archived',
        reward_snapshot=reward,
    )
    state = _gold_star_preview_save_state(state, actor)
    return jsonify({'ok': True, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/requests', methods=['POST'])
def api_gold_stars_preview_request_reward():
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    data = request.get_json(silent=True) or {}
    child_name = _gold_star_preview_clean_child_name(data.get('child_name') or '')
    if not child_name:
        return jsonify({'ok': False, 'error': 'Choose Avery or Harley.'}), 400
    try:
        reward_id = int(data.get('reward_id') or 0)
    except (TypeError, ValueError):
        reward_id = 0
    if reward_id <= 0:
        return jsonify({'ok': False, 'error': 'reward_id is required'}), 400
    state = _gold_star_preview_get_state(actor)
    existing_pending = next((
        req for req in state.get('requests', [])
        if isinstance(req, dict)
        and sanitize_text(req.get('child_name') or '').strip() == child_name
        and sanitize_text(req.get('status') or '').strip() == 'pending'
    ), None)
    if existing_pending:
        return jsonify({'ok': False, 'error': f'{child_name} already has a pending request.'}), 409
    reward = _gold_star_preview_find_reward(state, reward_id)
    if not reward or not bool(reward.get('active')):
        return jsonify({'ok': False, 'error': 'This reward is not currently active.'}), 400
    eligible = reward.get('eligible_children') if isinstance(reward.get('eligible_children'), list) else []
    if child_name not in eligible:
        return jsonify({'ok': False, 'error': f'{child_name} is not eligible for this reward.'}), 400
    bucket = _gold_star_preview_get_child_bucket(state, child_name)
    available = int(bucket.get('available') or 0)
    reward_cost = int(reward.get('cost') or 0)
    if available < reward_cost:
        return jsonify({'ok': False, 'error': 'Not enough available stars for this reward.'}), 400
    request_id = int(state.get('next_request_id') or 1)
    state['next_request_id'] = request_id + 1
    requests = state.get('requests')
    if not isinstance(requests, list):
        requests = []
    requests.append({
        'id': request_id,
        'child_name': child_name,
        'reward_id': int(reward.get('id') or 0),
        'reward_name': sanitize_text(reward.get('name') or '').strip(),
        'image_ref': sanitize_text(reward.get('image_ref') or 'star').strip() or 'star',
        'requested_cost': reward_cost,
        'status': 'pending',
        'status_message': 'Waiting for parent approval.',
        'requested_by': child_name,
        'requested_at': _gold_star_preview_utc_now_iso(),
        'resolved_by': '',
        'resolved_at': '',
        'delivery_id': 0,
    })
    state['requests'] = requests
    _gold_star_preview_append_history(
        state,
        event_type='requested',
        actor=child_name,
        child_name=child_name,
        message=f'{child_name} requested "{sanitize_text(reward.get("name") or "").strip()}".',
        context='Waiting for parent approval',
        reward_snapshot=reward,
    )
    state = _gold_star_preview_save_state(state, actor)
    return jsonify({'ok': True, 'request_id': request_id, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/requests/<int:request_id>/approve', methods=['POST'])
def api_gold_stars_preview_request_approve(request_id: int):
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    state = _gold_star_preview_get_state(actor)
    req = _gold_star_preview_resolve_request(state, request_id)
    if not req:
        return jsonify({'ok': False, 'error': 'Request not found'}), 404
    if sanitize_text(req.get('status') or '').strip() != 'pending':
        return jsonify({'ok': False, 'error': 'Only pending requests can be approved.'}), 409
    child_name = sanitize_text(req.get('child_name') or '').strip()
    reward = _gold_star_preview_find_reward(state, int(req.get('reward_id') or 0))
    if not reward or not bool(reward.get('active')):
        req['status'] = 'invalidated'
        req['status_message'] = 'Reward was archived before approval.'
        req['resolved_by'] = actor
        req['resolved_at'] = _gold_star_preview_utc_now_iso()
        _gold_star_preview_append_history(
            state,
            event_type='invalidated',
            actor=actor,
            child_name=child_name,
            message='Purchase request invalidated',
            context=req['status_message'],
            reward_snapshot={
                'id': req.get('reward_id'),
                'name': req.get('reward_name'),
                'cost': req.get('requested_cost'),
                'image_ref': req.get('image_ref'),
                'active': False,
            },
        )
        state = _gold_star_preview_save_state(state, actor)
        return jsonify({'ok': False, 'error': req['status_message'], 'state': _gold_star_preview_payload(state)}), 409
    eligible = reward.get('eligible_children') if isinstance(reward.get('eligible_children'), list) else []
    if child_name not in eligible:
        req['status'] = 'invalidated'
        req['status_message'] = 'Child is no longer eligible for this reward.'
        req['resolved_by'] = actor
        req['resolved_at'] = _gold_star_preview_utc_now_iso()
        _gold_star_preview_append_history(
            state,
            event_type='invalidated',
            actor=actor,
            child_name=child_name,
            message='Purchase request invalidated',
            context=req['status_message'],
            reward_snapshot={
                'id': req.get('reward_id'),
                'name': req.get('reward_name'),
                'cost': req.get('requested_cost'),
                'image_ref': req.get('image_ref'),
                'active': True,
            },
        )
        state = _gold_star_preview_save_state(state, actor)
        return jsonify({'ok': False, 'error': req['status_message'], 'state': _gold_star_preview_payload(state)}), 409
    current_cost = int(reward.get('cost') or 0)
    requested_cost = int(req.get('requested_cost') or 0)
    if current_cost != requested_cost:
        req['status'] = 'invalidated'
        req['status_message'] = f'Reward price changed from {requested_cost} to {current_cost}.'
        req['resolved_by'] = actor
        req['resolved_at'] = _gold_star_preview_utc_now_iso()
        _gold_star_preview_append_history(
            state,
            event_type='invalidated',
            actor=actor,
            child_name=child_name,
            message='Purchase request invalidated',
            context=req['status_message'],
            reward_snapshot={
                'id': req.get('reward_id'),
                'name': req.get('reward_name'),
                'cost': req.get('requested_cost'),
                'image_ref': req.get('image_ref'),
                'active': True,
            },
        )
        state = _gold_star_preview_save_state(state, actor)
        return jsonify({'ok': False, 'error': req['status_message'], 'state': _gold_star_preview_payload(state)}), 409
    bucket = _gold_star_preview_get_child_bucket(state, child_name)
    available = int(bucket.get('available') or 0)
    if available < requested_cost:
        return jsonify({'ok': False, 'error': 'Not enough available stars at approval time.'}), 400
    bucket['available'] = available - requested_cost
    bucket['spent'] = int(bucket.get('spent') or 0) + requested_cost
    state['children'][child_name] = bucket
    req['status'] = 'approved'
    req['status_message'] = 'Approved and ready for delivery.'
    req['resolved_by'] = actor
    req['resolved_at'] = _gold_star_preview_utc_now_iso()
    delivery_id = int(state.get('next_delivery_id') or 1)
    state['next_delivery_id'] = delivery_id + 1
    delivery_queue = state.get('delivery_queue')
    if not isinstance(delivery_queue, list):
        delivery_queue = []
    delivery_queue.append({
        'id': delivery_id,
        'request_id': int(req.get('id') or 0),
        'child_name': child_name,
        'reward_name': sanitize_text(req.get('reward_name') or '').strip(),
        'reward_cost': requested_cost,
        'image_ref': sanitize_text(req.get('image_ref') or 'star').strip() or 'star',
        'status': 'ready',
        'approved_by': actor,
        'approved_at': _gold_star_preview_utc_now_iso(),
        'delivered_by': '',
        'delivered_at': '',
    })
    state['delivery_queue'] = delivery_queue
    req['delivery_id'] = delivery_id
    _gold_star_preview_append_history(
        state,
        event_type='approved',
        actor=actor,
        child_name=child_name,
        stars_delta=-requested_cost,
        message=f'Parent approved "{sanitize_text(req.get("reward_name") or "").strip()}".',
        context='Stars deducted once at approval.',
        reward_snapshot=reward,
    )
    state = _gold_star_preview_save_state(state, actor)
    return jsonify({'ok': True, 'delivery_id': delivery_id, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/requests/<int:request_id>/decline', methods=['POST'])
def api_gold_stars_preview_request_decline(request_id: int):
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    state = _gold_star_preview_get_state(actor)
    req = _gold_star_preview_resolve_request(state, request_id)
    if not req:
        return jsonify({'ok': False, 'error': 'Request not found'}), 404
    if sanitize_text(req.get('status') or '').strip() != 'pending':
        return jsonify({'ok': False, 'error': 'Only pending requests can be declined.'}), 409
    req['status'] = 'declined'
    req['status_message'] = 'Request declined by parent.'
    req['resolved_by'] = actor
    req['resolved_at'] = _gold_star_preview_utc_now_iso()
    _gold_star_preview_append_history(
        state,
        event_type='declined',
        actor=actor,
        child_name=sanitize_text(req.get('child_name') or '').strip(),
        message=f'Parent declined "{sanitize_text(req.get("reward_name") or "").strip()}".',
        context='No star balance change.',
        reward_snapshot={
            'id': req.get('reward_id'),
            'name': req.get('reward_name'),
            'cost': req.get('requested_cost'),
            'image_ref': req.get('image_ref'),
            'active': True,
        },
    )
    state = _gold_star_preview_save_state(state, actor)
    return jsonify({'ok': True, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/delivery/<int:delivery_id>/deliver', methods=['POST'])
def api_gold_stars_preview_delivery_complete(delivery_id: int):
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    state = _gold_star_preview_get_state(actor)
    delivery = next((
        row for row in state.get('delivery_queue', [])
        if isinstance(row, dict) and int(row.get('id') or 0) == delivery_id
    ), None)
    if not delivery:
        return jsonify({'ok': False, 'error': 'Delivery record not found'}), 404
    if sanitize_text(delivery.get('status') or '').strip() != 'ready':
        return jsonify({'ok': False, 'error': 'Delivery already completed.'}), 409
    delivery['status'] = 'delivered'
    delivery['delivered_by'] = actor
    delivery['delivered_at'] = _gold_star_preview_utc_now_iso()
    _gold_star_preview_append_history(
        state,
        event_type='delivered',
        actor=actor,
        child_name=sanitize_text(delivery.get('child_name') or '').strip(),
        message=f'Delivery marked complete: {sanitize_text(delivery.get("reward_name") or "").strip()}.',
        context='Delivered by parent',
        reward_snapshot={
            'id': 0,
            'name': delivery.get('reward_name'),
            'cost': delivery.get('reward_cost'),
            'image_ref': delivery.get('image_ref'),
            'active': True,
        },
    )
    state = _gold_star_preview_save_state(state, actor)
    return jsonify({'ok': True, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/demo/give-star', methods=['POST'])
def api_gold_stars_preview_demo_give_star_compat():
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    data = request.get_json(silent=True) or {}
    child_name = _gold_star_preview_clean_child_name(data.get('child_name') or 'Avery') or 'Avery'
    note = sanitize_text(data.get('note') or '').strip()[:140]
    state = _gold_star_preview_get_state(actor)
    award_id = int(state.get('next_award_id') or 1)
    state['next_award_id'] = award_id + 1
    bucket = _gold_star_preview_get_child_bucket(state, child_name)
    waiting_awards = bucket.get('waiting_awards')
    if not isinstance(waiting_awards, list):
        waiting_awards = []
    waiting_awards.append({
        'id': award_id,
        'child_name': child_name,
        'note': note,
        'awarded_by': actor,
        'awarded_at': _gold_star_preview_utc_now_iso(),
        'collected': False,
        'collected_by': '',
        'collected_at': '',
    })
    bucket['waiting_awards'] = waiting_awards
    bucket['earned'] = int(bucket.get('earned') or 0) + 1
    state['children'][child_name] = bucket
    _gold_star_preview_append_history(
        state,
        event_type='awarded',
        actor=actor,
        child_name=child_name,
        stars_delta=1,
        message=f'{child_name} got a new waiting star.',
        context=note or 'No note',
    )
    state = _gold_star_preview_save_state(state, actor)
    return jsonify({'ok': True, 'waiting_id': award_id, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/demo/collect', methods=['POST'])
def api_gold_stars_preview_demo_collect_compat():
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    data = request.get_json(silent=True) or {}
    child_name = _gold_star_preview_clean_child_name(data.get('child_name') or 'Avery') or 'Avery'
    try:
        award_id = int(data.get('waiting_id') or data.get('award_id') or 0)
    except (TypeError, ValueError):
        award_id = 0
    if award_id <= 0:
        return jsonify({'ok': False, 'error': 'waiting_id is required'}), 400
    state = _gold_star_preview_get_state(actor)
    bucket = _gold_star_preview_get_child_bucket(state, child_name)
    waiting_awards = bucket.get('waiting_awards')
    if not isinstance(waiting_awards, list):
        waiting_awards = []
    target = next((award for award in waiting_awards if isinstance(award, dict) and int(award.get('id') or 0) == award_id), None)
    if not target:
        return jsonify({'ok': False, 'error': 'Waiting star not found'}), 404
    if bool(target.get('collected')):
        return jsonify({'ok': False, 'error': 'This waiting star was already collected.'}), 409
    target['collected'] = True
    target['collected_by'] = child_name
    target['collected_at'] = _gold_star_preview_utc_now_iso()
    bucket['available'] = int(bucket.get('available') or 0) + 1
    state['children'][child_name] = bucket
    _gold_star_preview_append_history(
        state,
        event_type='collected',
        actor=child_name,
        child_name=child_name,
        stars_delta=0,
        message=f'{child_name} collected a waiting star.',
        context=sanitize_text(target.get('note') or '').strip() or 'Collected',
    )
    state = _gold_star_preview_save_state(state, actor)
    return jsonify({'ok': True, 'state': _gold_star_preview_payload(state)})


@main_bp.route('/api/gold-stars/preview/demo/approve-reward', methods=['POST'])
def api_gold_stars_preview_demo_approve_compat():
    actor = _current_actor_name()
    denied = _gold_star_preview_require_parent(actor)
    if denied is not None:
        return denied
    state = _gold_star_preview_get_state(actor)
    pending = next((
        req for req in state.get('requests', [])
        if isinstance(req, dict)
        and sanitize_text(req.get('child_name') or '').strip() == 'Avery'
        and sanitize_text(req.get('status') or '').strip() == 'pending'
    ), None)
    if not pending:
        return jsonify({'ok': False, 'error': 'No pending Avery request to approve.'}), 409
    return api_gold_stars_preview_request_approve(int(pending.get('id') or 0))


@main_bp.route('/kids-stars', methods=['GET'])
def kids_stars_tablet():
    actor = _current_actor_name()
    kiosk_requested = str(request.args.get('kiosk') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    if kiosk_requested:
        session['kid_kiosk_mode'] = True
        session['kid_kiosk_started_by'] = actor
    return render_template(
        'kids_stars.html',
        rewards_state=_kid_reward_state_payload(actor),
        actor_name=actor,
    )


@main_bp.route('/api/rewards/kiosk-mode', methods=['POST'])
def api_rewards_set_kiosk_mode():
    actor = _current_actor_name()
    if not _can_manage_kid_rewards(actor):
        return jsonify({'ok': False, 'error': 'Only Rachel or Mykal can change kiosk mode.'}), 403
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get('enabled'))
    if enabled:
        session['kid_kiosk_mode'] = True
        session['kid_kiosk_started_by'] = actor
        return jsonify({'ok': True, 'enabled': True})

    parent_name = sanitize_text(data.get('parent_name') or actor).strip()
    if sanitize_text(parent_name).strip().lower() not in _KID_REWARD_ADMIN_NAMES:
        return jsonify({'ok': False, 'error': 'Only Rachel or Mykal can unlock kiosk mode.'}), 403
    unlock_pin = str(data.get('unlock_pin') or '').strip()
    if not unlock_pin:
        return jsonify({'ok': False, 'error': 'Parent PIN is required to unlock kiosk mode.'}), 400
    parent_member = FamilyMember.query.filter(db.func.lower(FamilyMember.name) == parent_name.lower()).first()
    if not parent_member or not sanitize_text(parent_member.pin or '').strip():
        return jsonify({'ok': False, 'error': f'{parent_name} must set a PIN in Settings before kiosk unlock is available.'}), 400
    if unlock_pin != str(parent_member.pin).strip():
        return jsonify({'ok': False, 'error': 'Incorrect parent PIN.'}), 403

    session['kid_kiosk_mode'] = False
    session.pop('kid_kiosk_started_by', None)
    return jsonify({'ok': True, 'enabled': enabled})


@main_bp.route('/api/rewards/state', methods=['GET'])
def api_rewards_state():
    actor = _current_actor_name()
    return jsonify({'ok': True, 'state': _kid_reward_state_payload(actor)})


@main_bp.route('/api/rewards/stars', methods=['POST'])
def api_rewards_adjust_stars():
    actor = _current_actor_name()
    if not _can_manage_kid_rewards(actor):
        return jsonify({'ok': False, 'error': 'Only Rachel or Mykal can change rewards right now.'}), 403

    data = request.get_json(silent=True) or {}
    child_name = sanitize_text(data.get('child_name') or '').strip()
    if child_name not in _KID_MEMBERS:
        return jsonify({'ok': False, 'error': 'Choose Avery or Harley'}), 400
    try:
        delta = int(data.get('delta'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'delta must be an integer'}), 400
    if delta == 0:
        return jsonify({'ok': False, 'error': 'delta must not be 0'}), 400
    if delta < -10 or delta > 10:
        return jsonify({'ok': False, 'error': 'delta must be between -10 and 10'}), 400
    if delta > 0 and sanitize_text(child_name).strip().lower() == sanitize_text(actor).strip().lower():
        return jsonify({'ok': False, 'error': 'You cannot give yourself stars.'}), 403
    if delta > 0 and _child_has_pending_repair(child_name):
        return jsonify({'ok': False, 'error': 'Repair task must be completed before adding stars.'}), 409
    reason = sanitize_text(data.get('reason') or '').strip()[:256] or None
    ok, error, milestones_reached = _apply_star_delta(
        child_name=child_name,
        delta=delta,
        actor=actor,
        reason=reason,
    )
    if not ok:
        return jsonify({'ok': False, 'error': error}), 500
    db.session.commit()
    return jsonify({
        'ok': True,
        'state': _kid_reward_state_payload(actor),
        'effects': {
            'child_name': child_name,
            'delta': delta,
            'milestones_reached': milestones_reached,
        },
    })


@main_bp.route('/api/rewards/dispense', methods=['POST'])
def api_rewards_dispense_stars():
    actor = _current_actor_name()
    if not _can_manage_kid_rewards(actor):
        return jsonify({'ok': False, 'error': 'Only Rachel or Mykal can dispense stars right now.'}), 403

    data = request.get_json(silent=True) or {}
    child_name = sanitize_text(data.get('child_name') or '').strip()
    if child_name not in _KID_MEMBERS:
        return jsonify({'ok': False, 'error': 'Choose a valid family member'}), 400
    try:
        stars_awarded = int(data.get('stars_awarded'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'stars_awarded must be an integer'}), 400
    stars_awarded = max(1, min(10, stars_awarded))
    if _child_has_pending_repair(child_name):
        return jsonify({'ok': False, 'error': 'Repair task must be completed before dispensing stars.'}), 409

    reason = sanitize_text(data.get('reason') or '').strip()[:256] or None
    db.session.add(KidRewardPendingGrant(
        child_name=child_name,
        stars_awarded=stars_awarded,
        reason=reason,
        dispensed_by=actor,
        claimed=False,
    ))
    db.session.commit()
    return jsonify({'ok': True, 'state': _kid_reward_state_payload(actor)})


@main_bp.route('/api/rewards/claim', methods=['POST'])
def api_rewards_claim_dispensed_stars():
    actor = _current_actor_name()
    data = request.get_json(silent=True) or {}
    grant_id = data.get('grant_id')
    child_name = sanitize_text(data.get('child_name') or '').strip()

    grant = None
    if grant_id is not None:
        try:
            grant = db.session.get(KidRewardPendingGrant, int(grant_id))
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'grant_id must be an integer'}), 400
        if not grant or bool(grant.claimed):
            return jsonify({'ok': False, 'error': 'Dispensed stars were already claimed or removed.'}), 404
        child_name = sanitize_text(grant.child_name or '').strip()
    else:
        if child_name not in _KID_MEMBERS:
            return jsonify({'ok': False, 'error': 'Choose a valid family member'}), 400
        grant = (
            KidRewardPendingGrant.query
            .filter_by(child_name=child_name, claimed=False)
            .order_by(KidRewardPendingGrant.created_at.asc(), KidRewardPendingGrant.id.asc())
            .first()
        )
        if not grant:
            return jsonify({'ok': False, 'error': 'No dispensed stars are waiting to be claimed.'}), 404

    is_manager = _can_manage_kid_rewards(actor)
    if not is_manager and sanitize_text(actor).strip().lower() != child_name.lower():
        return jsonify({'ok': False, 'error': 'You can only claim stars for your own profile.'}), 403
    if _child_has_pending_repair(child_name):
        return jsonify({'ok': False, 'error': 'Repair task must be completed before claiming stars.'}), 409

    reason = sanitize_text(grant.reason or '').strip()[:256] or None
    ok, error, milestones_reached = _apply_star_delta(
        child_name=child_name,
        delta=max(1, min(10, int(grant.stars_awarded or 1))),
        actor=actor,
        reason=reason,
    )
    if not ok:
        return jsonify({'ok': False, 'error': error}), 500

    grant.claimed = True
    grant.claimed_by = actor
    grant.claimed_at = datetime.utcnow()
    db.session.add(grant)
    db.session.commit()
    return jsonify({
        'ok': True,
        'state': _kid_reward_state_payload(actor),
        'effects': {
            'child_name': child_name,
            'delta': max(1, int(grant.stars_awarded or 1)),
            'milestones_reached': milestones_reached,
        },
    })


@main_bp.route('/api/rewards/rules', methods=['PUT'])
def api_rewards_update_rules():
    actor = _current_actor_name()
    if not _can_manage_kid_rewards(actor):
        return jsonify({'ok': False, 'error': 'Only Rachel or Mykal can change rewards right now.'}), 403

    data = request.get_json(silent=True) or {}
    incoming_rules = data.get('rules')
    if not isinstance(incoming_rules, list) or not incoming_rules:
        return jsonify({'ok': False, 'error': 'rules must be a non-empty list'}), 400

    sanitized_rules = []
    for index, raw in enumerate(incoming_rules):
        if not isinstance(raw, dict):
            return jsonify({'ok': False, 'error': 'Each rule must be an object'}), 400
        behavior_kind = sanitize_text(raw.get('behavior_kind') or '').strip().lower()
        if behavior_kind not in {'do', 'dont'}:
            return jsonify({'ok': False, 'error': 'behavior_kind must be do or dont'}), 400
        title = sanitize_text(raw.get('title') or '').strip()
        if not title:
            return jsonify({'ok': False, 'error': 'Each rule needs a title'}), 400
        try:
            points = int(raw.get('points'))
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'Each rule needs numeric points'}), 400
        points = max(-10, min(10, points))
        emoji = sanitize_text(raw.get('emoji') or '').strip()[:16] or ('⭐' if behavior_kind == 'do' else '🚫')
        sanitized_rules.append({
            'behavior_kind': behavior_kind,
            'title': title[:256],
            'points': points,
            'emoji': emoji,
            'sort_order': index,
        })

    (
        KidRewardRule.query
        .filter(KidRewardRule.behavior_kind.in_(['do', 'dont']))
        .delete(synchronize_session=False)
    )
    for rule in sanitized_rules:
        db.session.add(KidRewardRule(
            behavior_kind=rule['behavior_kind'],
            title=rule['title'],
            points=rule['points'],
            emoji=rule['emoji'],
            sort_order=rule['sort_order'],
            active=True,
            created_by=actor,
            updated_by=actor,
        ))
    db.session.commit()
    return jsonify({'ok': True, 'state': _kid_reward_state_payload(actor)})


@main_bp.route('/api/rewards/settings', methods=['PUT'])
def api_rewards_update_settings():
    actor = _current_actor_name()
    if not _can_manage_kid_rewards(actor):
        return jsonify({'ok': False, 'error': 'Only Rachel or Mykal can change rewards right now.'}), 403
    data = request.get_json(silent=True) or {}
    row = _get_or_create_kid_reward_settings(actor)
    row.confetti_enabled = bool(data.get('confetti_enabled', row.confetti_enabled))
    row.sounds_enabled = bool(data.get('sounds_enabled', row.sounds_enabled))
    try:
        timeout_minutes = int(data.get('timeout_default_minutes', row.timeout_default_minutes))
        timeout_star_loss = int(data.get('timeout_default_star_loss', row.timeout_default_star_loss))
        timeout_reward_minutes_loss = int(data.get('timeout_default_reward_minutes_loss', row.timeout_default_reward_minutes_loss))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Timeout defaults must be numbers'}), 400
    row.timeout_default_minutes = max(1, min(30, timeout_minutes))
    row.timeout_default_star_loss = max(0, min(5, timeout_star_loss))
    row.timeout_default_reward_minutes_loss = max(0, min(120, timeout_reward_minutes_loss))
    row.updated_by = actor
    db.session.add(row)
    db.session.commit()
    return jsonify({'ok': True, 'state': _kid_reward_state_payload(actor)})


@main_bp.route('/api/rewards/reward-options', methods=['PUT'])
def api_rewards_update_reward_options():
    actor = _current_actor_name()
    if not _can_manage_kid_rewards(actor):
        return jsonify({'ok': False, 'error': 'Only Rachel or Mykal can change rewards right now.'}), 403
    data = request.get_json(silent=True) or {}
    incoming_rewards = data.get('rewards')
    incoming_options = data.get('options')
    if incoming_rewards is None and incoming_options is None:
        return jsonify({'ok': False, 'error': 'Provide rewards (preferred) or options payload'}), 400

    normalized_rewards: list[dict] = []
    if isinstance(incoming_rewards, list):
        for raw in incoming_rewards:
            if not isinstance(raw, dict):
                return jsonify({'ok': False, 'error': 'Each reward must be an object'}), 400
            reward_label = sanitize_text(raw.get('reward_label') or '').strip()[:256]
            if not reward_label:
                return jsonify({'ok': False, 'error': 'Each reward needs a name'}), 400
            try:
                stars_required = int(raw.get('stars_required'))
            except (TypeError, ValueError):
                return jsonify({'ok': False, 'error': f'Stars required for "{reward_label}" must be a number'}), 400
            applies_to = raw.get('applies_to')
            if not isinstance(applies_to, list) or not applies_to:
                return jsonify({'ok': False, 'error': f'Select at least one family member for "{reward_label}"'}), 400
            members = []
            seen = set()
            for member_name in applies_to:
                clean_member = sanitize_text(member_name or '').strip()
                if clean_member not in _KID_MEMBERS:
                    continue
                if clean_member in seen:
                    continue
                seen.add(clean_member)
                members.append(clean_member)
            if not members:
                return jsonify({'ok': False, 'error': f'No valid family members selected for "{reward_label}"'}), 400
            normalized_rewards.append({
                'reward_label': reward_label,
                'stars_required': max(1, min(100, stars_required)),
                'applies_to': members,
            })
    elif isinstance(incoming_options, dict):
        for child_name in _KID_MEMBERS:
            raw_options = incoming_options.get(child_name, [])
            if not isinstance(raw_options, list):
                return jsonify({'ok': False, 'error': f'options for {child_name} must be a list'}), 400
            for raw in raw_options:
                if not isinstance(raw, dict):
                    return jsonify({'ok': False, 'error': f'Each reward option for {child_name} must be an object'}), 400
                reward_label = sanitize_text(raw.get('reward_label') or '').strip()[:256]
                if not reward_label:
                    continue
                try:
                    stars_required = int(raw.get('stars_required'))
                except (TypeError, ValueError):
                    return jsonify({'ok': False, 'error': f'Stars required for {child_name} must be a number'}), 400
                normalized_rewards.append({
                    'reward_label': reward_label,
                    'stars_required': max(1, min(100, stars_required)),
                    'applies_to': [child_name],
                })
    else:
        return jsonify({'ok': False, 'error': 'Invalid payload format for reward options'}), 400

    if not normalized_rewards:
        return jsonify({'ok': False, 'error': 'Add at least one reward option'}), 400

    (
        KidRewardRule.query
        .filter(KidRewardRule.behavior_kind == 'reward_option')
        .delete(synchronize_session=False)
    )
    row_index = 0
    for option in normalized_rewards:
        for child_name in option['applies_to']:
            db.session.add(KidRewardRule(
                behavior_kind='reward_option',
                title=option['reward_label'],
                points=option['stars_required'],
                emoji=child_name,
                sort_order=row_index,
                active=True,
                created_by=actor,
                updated_by=actor,
            ))
            row_index += 1
    db.session.commit()
    return jsonify({'ok': True, 'state': _kid_reward_state_payload(actor)})


@main_bp.route('/api/rewards/redeem', methods=['POST'])
def api_rewards_redeem():
    actor = _current_actor_name()
    if not _can_manage_kid_rewards(actor):
        return jsonify({'ok': False, 'error': 'Only Rachel or Mykal can change rewards right now.'}), 403
    data = request.get_json(silent=True) or {}
    child_name = sanitize_text(data.get('child_name') or '').strip()
    if child_name not in _KID_MEMBERS:
        return jsonify({'ok': False, 'error': 'Choose Avery or Harley'}), 400
    requires_parent_action = bool(data.get('requires_parent_action', True))
    reward_option_id = data.get('reward_option_id')
    reward_label = ''
    stars_spent = 0
    if reward_option_id is not None:
        try:
            option_id = int(reward_option_id)
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'reward_option_id must be an integer'}), 400
        option = db.session.get(KidRewardRule, option_id)
        if not option or (not bool(option.active)) or sanitize_text(option.behavior_kind or '').strip().lower() != 'reward_option':
            return jsonify({'ok': False, 'error': 'Reward option not found'}), 404
        if sanitize_text(option.emoji or '').strip() != child_name:
            return jsonify({'ok': False, 'error': f'Reward option does not belong to {child_name}'}), 400
        reward_label = sanitize_text(option.title or '').strip()[:256]
        stars_spent = max(1, min(100, int(option.points or 1)))
    else:
        reward_label = sanitize_text(data.get('reward_label') or '').strip()
        if not reward_label:
            return jsonify({'ok': False, 'error': 'Reward label is required'}), 400
        try:
            stars_spent = int(data.get('stars_spent'))
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'stars_spent must be an integer'}), 400
        if stars_spent <= 0:
            return jsonify({'ok': False, 'error': 'stars_spent must be greater than 0'}), 400
    if _child_has_pending_repair(child_name):
        return jsonify({'ok': False, 'error': 'Repair task must be completed before redeeming rewards.'}), 409

    profile = KidRewardProfile.query.filter_by(child_name=child_name).first()
    if not profile:
        _ensure_kid_reward_profiles(actor)
        profile = KidRewardProfile.query.filter_by(child_name=child_name).first()
    if not profile:
        return jsonify({'ok': False, 'error': 'Could not load reward profile'}), 500
    available = max(0, int(profile.stars or 0))
    if stars_spent > available:
        return jsonify({'ok': False, 'error': f'{child_name} only has {available} stars available'}), 400
    profile.stars = available - stars_spent
    profile.updated_by = actor
    db.session.add(profile)
    redemption = KidRewardRedemption(
        child_name=child_name,
        reward_label=reward_label[:256],
        stars_spent=stars_spent,
        redeemed_by=actor,
    )
    db.session.add(redemption)
    db.session.flush()
    if requires_parent_action:
        _create_parent_reward_tasks(redemption=redemption, actor_name=actor)
    db.session.add(KidRewardEvent(
        child_name=child_name,
        delta=-stars_spent,
        reason=f"Redeemed: {reward_label[:200]}",
        created_by=actor,
    ))
    db.session.commit()
    return jsonify({'ok': True, 'state': _kid_reward_state_payload(actor)})


@main_bp.route('/api/rewards/timeout', methods=['POST'])
def api_rewards_timeout():
    actor = _current_actor_name()
    if not _can_manage_kid_rewards(actor):
        return jsonify({'ok': False, 'error': 'Only Rachel or Mykal can change rewards right now.'}), 403
    data = request.get_json(silent=True) or {}
    child_name = sanitize_text(data.get('child_name') or '').strip()
    if child_name not in _KID_MEMBERS:
        return jsonify({'ok': False, 'error': 'Choose Avery or Harley'}), 400
    reason = sanitize_text(data.get('reason') or '').strip()
    if not reason:
        return jsonify({'ok': False, 'error': 'Reason is required for timeout'}), 400
    repair_task_text = sanitize_text(data.get('repair_task') or '').strip()[:256] or _DEFAULT_REPAIR_TASK

    settings = _get_or_create_kid_reward_settings(actor)
    try:
        timeout_minutes = int(data.get('timeout_minutes', settings.timeout_default_minutes))
        stars_deducted = int(data.get('stars_deducted', settings.timeout_default_star_loss))
        reward_minutes_lost = int(data.get('reward_minutes_lost', settings.timeout_default_reward_minutes_loss))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Timeout values must be numbers'}), 400
    timeout_minutes = max(1, min(30, timeout_minutes))
    stars_deducted = max(0, min(5, stars_deducted))
    reward_minutes_lost = max(0, min(120, reward_minutes_lost))

    profile = KidRewardProfile.query.filter_by(child_name=child_name).first()
    if not profile:
        _ensure_kid_reward_profiles(actor)
        profile = KidRewardProfile.query.filter_by(child_name=child_name).first()
    if not profile:
        return jsonify({'ok': False, 'error': 'Could not load reward profile'}), 500

    profile.stars = max(0, int(profile.stars or 0) - stars_deducted)
    profile.updated_by = actor
    db.session.add(profile)
    timeout_row = KidRewardTimeout(
        child_name=child_name,
        reason=reason[:256],
        timeout_minutes=timeout_minutes,
        stars_deducted=stars_deducted,
        reward_minutes_lost=reward_minutes_lost,
        created_by=actor,
    )
    db.session.add(timeout_row)
    db.session.flush()
    db.session.add(KidRewardRepairTask(
        timeout_id=timeout_row.id,
        child_name=child_name,
        task_text=repair_task_text,
        completed=False,
    ))
    if stars_deducted > 0:
        db.session.add(KidRewardEvent(
            child_name=child_name,
            delta=-stars_deducted,
            reason=f"Timeout: {reason[:200]}",
            created_by=actor,
        ))
    db.session.commit()
    return jsonify({'ok': True, 'state': _kid_reward_state_payload(actor)})


@main_bp.route('/api/rewards/repair/complete', methods=['POST'])
def api_rewards_complete_repair():
    actor = _current_actor_name()
    if not _can_manage_kid_rewards(actor):
        return jsonify({'ok': False, 'error': 'Only Rachel or Mykal can change rewards right now.'}), 403
    data = request.get_json(silent=True) or {}
    try:
        repair_id = int(data.get('repair_id'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'repair_id must be an integer'}), 400
    repair = db.session.get(KidRewardRepairTask, repair_id)
    if not repair:
        return jsonify({'ok': False, 'error': 'Repair task not found'}), 404
    if repair.completed:
        return jsonify({'ok': True, 'state': _kid_reward_state_payload(actor)})
    repair.completed = True
    repair.completed_at = datetime.utcnow()
    repair.completed_by = actor
    db.session.add(repair)
    db.session.commit()
    return jsonify({'ok': True, 'state': _kid_reward_state_payload(actor)})


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
