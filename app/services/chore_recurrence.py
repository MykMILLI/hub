from __future__ import annotations

from datetime import date, datetime, timedelta
import json

from ..models import Chore, ChoreWeeklyCompletion


WEEKDAY_LABELS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']


def parse_reference_date(raw_value=None) -> date:
    if isinstance(raw_value, date):
        return raw_value
    text = str(raw_value or '').strip()
    if text:
        try:
            return datetime.strptime(text, '%Y-%m-%d').date()
        except ValueError:
            pass
    return date.today()


def sunday_week_start(reference_date: date) -> date:
    days_since_sunday = (reference_date.weekday() + 1) % 7
    return reference_date - timedelta(days=days_since_sunday)


def stored_schedule_days(item: Chore) -> list[str]:
    try:
        values = json.loads(item.days_of_week_json or '[]')
    except (TypeError, ValueError):
        values = []
    if not isinstance(values, list):
        return []
    selected = {str(value) for value in values}
    return [label for label in WEEKDAY_LABELS if label in selected]


def day_label(reference_date: date) -> str:
    return WEEKDAY_LABELS[(reference_date.weekday() + 1) % 7]


def scheduled_date_for(week_start: date, label: str) -> date:
    return week_start + timedelta(days=WEEKDAY_LABELS.index(label))


def _utc_isoformat(value) -> str | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(microsecond=0).isoformat() + 'Z'


def resolve_completion_day(days_of_week: list[str], completed_days: list[str], reference_date: date) -> str | None:
    """Pick which scheduled occurrence a check-off on ``reference_date`` credits.

    Completing on a scheduled day always credits that day. Completing on any other
    day credits the next outstanding scheduled day of the week (so finishing a
    Thursday chore on Wednesday counts), falling back to the latest outstanding
    day when the week has no upcoming occurrences left.
    """
    remaining = [label for label in days_of_week if label not in completed_days]
    if not remaining:
        return None
    current_day = day_label(reference_date)
    if current_day in remaining:
        return current_day
    current_index = WEEKDAY_LABELS.index(current_day)
    upcoming = [label for label in remaining if WEEKDAY_LABELS.index(label) > current_index]
    if upcoming:
        return upcoming[0]
    return remaining[-1]


def resolve_reopen_day(completed_records: list, reference_date: date) -> object | None:
    """Pick which completion a un-check on ``reference_date`` should remove."""
    if not completed_records:
        return None
    current_day = day_label(reference_date)
    for record in completed_records:
        if record.scheduled_day == current_day:
            return record
    return sorted(
        completed_records,
        key=lambda record: (record.completed_at or datetime.min, WEEKDAY_LABELS.index(record.scheduled_day)),
    )[-1]


def weekly_progress(item: Chore, days_of_week: list[str], reference_date: date) -> dict | None:
    if not days_of_week:
        return None
    week_start = sunday_week_start(reference_date)
    records = ChoreWeeklyCompletion.query.filter_by(
        chore_id=item.id,
        week_start=week_start,
    ).all()
    scheduled = set(days_of_week)
    relevant = sorted(
        (record for record in records if record.scheduled_day in scheduled),
        key=lambda record: WEEKDAY_LABELS.index(record.scheduled_day),
    )
    completed_days = []
    completions = []
    for record in relevant:
        if record.scheduled_day in completed_days:
            continue
        completed_days.append(record.scheduled_day)
        due_on = scheduled_date_for(week_start, record.scheduled_day)
        completed_on = record.completed_on or (record.completed_at.date() if record.completed_at else None)
        completions.append({
            'day': record.scheduled_day,
            'due_on': due_on.isoformat(),
            'completed_on': completed_on.isoformat() if completed_on else None,
            'completed_at': _utc_isoformat(record.completed_at),
            'completed_by': record.completed_by or '',
            'early': bool(completed_on and completed_on < due_on),
            'late': bool(completed_on and completed_on > due_on),
        })
    current_day = day_label(reference_date)
    last_completed_at = max(
        (record.completed_at for record in relevant if record.completed_at),
        default=None,
    )
    return {
        'week_start': week_start.isoformat(),
        'week_end': (week_start + timedelta(days=6)).isoformat(),
        'completed_days': completed_days,
        'completed_count': len(completed_days),
        'required_count': len(days_of_week),
        'current_day': current_day,
        'current_occurrence_scheduled': current_day in scheduled,
        'current_occurrence_completed': current_day in completed_days,
        'complete': len(completed_days) >= len(days_of_week),
        'completions': completions,
        'last_completed_at': _utc_isoformat(last_completed_at),
        'next_open_day': resolve_completion_day(days_of_week, completed_days, reference_date),
    }


def is_effectively_done(item: Chore, reference_date: date | None = None) -> bool:
    if str(item.task_type or '').strip().lower() != 'chore':
        return bool(item.done)
    days_of_week = stored_schedule_days(item)
    if not days_of_week:
        return bool(item.done)
    progress = weekly_progress(item, days_of_week, reference_date or date.today())
    return bool(progress and progress['complete'])
