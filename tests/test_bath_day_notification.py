from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import create_app, db
from app.blueprints import dashboard
from app.models import AppNotification, HouseholdRoutineState

EASTERN = ZoneInfo('America/New_York')
# A winter date, so 5:00 PM Eastern is exactly 22:00 UTC.
FROZEN_DATE = date(2026, 1, 14)


@pytest.fixture()
def app():
    app = create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'HOMEHUB_CONFIG': {
            'admin_name': 'Administrator',
            'family_members': ['Mykal', 'Rachel'],
            'notifications': {'mobile_enabled': True, 'categories': {}},
        },
        'SECRET_KEY': 'test',
    })
    with app.app_context():
        db.create_all()
    return app


def freeze_eastern(monkeypatch, hour: int, minute: int = 0) -> None:
    """Pin the dashboard wall clock to a known Eastern moment."""
    target = datetime(FROZEN_DATE.year, FROZEN_DATE.month, FROZEN_DATE.day, hour, minute, tzinfo=EASTERN)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return target.astimezone(tz) if tz else target.replace(tzinfo=None)

    monkeypatch.setattr(dashboard, 'datetime', FrozenDatetime)


def seed_routine_state(last_completed_days_ago: int) -> HouseholdRoutineState:
    """Record the last completed bath relative to the frozen date."""
    completed_on = datetime.combine(FROZEN_DATE - timedelta(days=last_completed_days_ago), datetime.min.time()).replace(hour=12)
    state = HouseholdRoutineState(
        bedtime_anchor_date=FROZEN_DATE,
        bedtime_anchor_reader='Rachel',
        bath_mode=0,
        updated_by='Mykal',
        updated_at=completed_on,
    )
    db.session.add(state)
    db.session.commit()
    assert state.updated_at.date() == completed_on.date()
    return state


def bath_notifications():
    return AppNotification.query.filter_by(event_type='bath_day').all()


def test_bath_day_notifies_mykal_and_rachel_at_five_pm_eastern(app, monkeypatch):
    with app.app_context():
        seed_routine_state(last_completed_days_ago=2)
        freeze_eastern(monkeypatch, 17, 0)

        assert dashboard.emit_due_bath_day_notification() == 2

        rows = bath_notifications()
        assert sorted(row.target_member for row in rows) == ['Mykal', 'Rachel']
        assert all(row.title == 'Bath Day' for row in rows)


def test_bath_day_notification_is_not_sent_before_five_pm_eastern(app, monkeypatch):
    with app.app_context():
        seed_routine_state(last_completed_days_ago=2)
        freeze_eastern(monkeypatch, 16, 59)

        assert dashboard.emit_due_bath_day_notification() == 0
        assert bath_notifications() == []


def test_bath_day_notification_is_skipped_when_no_bath_is_due(app, monkeypatch):
    with app.app_context():
        seed_routine_state(last_completed_days_ago=0)
        freeze_eastern(monkeypatch, 18, 30)

        assert dashboard.emit_due_bath_day_notification() == 0
        assert bath_notifications() == []


def test_bath_day_notification_is_sent_once_per_day(app, monkeypatch):
    with app.app_context():
        seed_routine_state(last_completed_days_ago=2)
        freeze_eastern(monkeypatch, 17, 0)
        assert dashboard.emit_due_bath_day_notification() == 2

        freeze_eastern(monkeypatch, 21, 45)
        assert dashboard.emit_due_bath_day_notification() == 0
        assert len(bath_notifications()) == 2


def test_overdue_bath_still_notifies(app, monkeypatch):
    with app.app_context():
        seed_routine_state(last_completed_days_ago=5)
        freeze_eastern(monkeypatch, 17, 30)

        assert dashboard.emit_due_bath_day_notification() == 2


def test_bath_day_notification_targets_mobile_and_in_app(app, monkeypatch):
    with app.app_context():
        seed_routine_state(last_completed_days_ago=3)
        freeze_eastern(monkeypatch, 17, 30)
        dashboard.emit_due_bath_day_notification()

        row = bath_notifications()[0]
        assert 'mobile' in row.channels_json
        assert 'in_app' in row.channels_json
        assert f'bath-day:{FROZEN_DATE.isoformat()}' in row.payload_json
        assert row.message == 'Bath time tonight: Avery with Mommy, Harley with Daddy.'
        assert '&' not in row.message


def test_five_pm_eastern_matches_twenty_two_hundred_utc_in_winter(app, monkeypatch):
    """The requested 5:00 PM EST trigger is 22:00 UTC during standard time."""
    with app.app_context():
        seed_routine_state(last_completed_days_ago=2)
        freeze_eastern(monkeypatch, 17, 0)
        fired_at = dashboard.datetime.now(timezone.utc)

        assert (fired_at.hour, fired_at.minute) == (22, 0)
        assert dashboard.emit_due_bath_day_notification() == 2
