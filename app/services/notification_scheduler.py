import threading
import time

_started = False
_lock = threading.Lock()
INTERVAL_SECONDS = 60


def run_due_notifications(app) -> None:
    """Emit every time-based notification for every member, without any client polling."""
    with app.app_context():
        from ..models import FamilyMember, db
        from ..blueprints.family_calendar import emit_due_calendar_reminders_for_member
        from ..blueprints.dashboard import emit_due_bath_day_notification, emit_due_bedtime_reader_notification_for_member, emit_due_parent_helper_notifications_for_member
        from ..blueprints.expiry import _ensure_due_reminders_for_member

        try:
            names = [m.name for m in FamilyMember.query.all()]
        except Exception:
            db.session.rollback()
            return

        for name in names:
            for fn in (emit_due_calendar_reminders_for_member,
                       emit_due_bedtime_reader_notification_for_member,
                       _ensure_due_reminders_for_member,
                       emit_due_parent_helper_notifications_for_member):
                try:
                    fn(name)
                except Exception as err:
                    db.session.rollback()
                    print(f'[NOTIFY SCHEDULER] {fn.__name__}({name}) failed: {err}')
        try:
            emit_due_bath_day_notification()
        except Exception as err:
            db.session.rollback()
            print(f'[NOTIFY SCHEDULER] bath day failed: {err}')
        db.session.remove()


def start_notification_scheduler(app) -> bool:
    global _started
    with _lock:
        if _started:
            return False
        _started = True

    def _loop():
        time.sleep(5)
        while True:
            try:
                run_due_notifications(app)
            except Exception as err:
                print(f'[NOTIFY SCHEDULER] loop error: {err}')
            time.sleep(INTERVAL_SECONDS)

    threading.Thread(target=_loop, name='notification-scheduler', daemon=True).start()
    return True
