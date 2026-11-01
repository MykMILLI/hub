import base64
import json
import os
import threading

from flask import current_app

from ..models import FamilyMember, WebPushSubscription, db

_key_lock = threading.Lock()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _keys_path() -> str:
    base_dir = os.path.abspath(os.path.join(current_app.root_path, '..'))
    data_dir = os.path.join(base_dir, 'data')
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, 'vapid_keys.json')


def get_vapid_keys() -> dict | None:
    """Return {'private': <b64url raw key>, 'public': <b64url uncompressed point>, 'subject': str}.

    Keys come from VAPID_PRIVATE_KEY/VAPID_PUBLIC_KEY env vars or are generated once and persisted in data/.
    """
    subject = os.environ.get('VAPID_SUBJECT') or 'mailto:admin@homehub.local'
    env_private = (os.environ.get('VAPID_PRIVATE_KEY') or '').strip()
    env_public = (os.environ.get('VAPID_PUBLIC_KEY') or '').strip()
    if env_private and env_public:
        return {'private': env_private, 'public': env_public, 'subject': subject}

    with _key_lock:
        path = _keys_path()
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    stored = json.load(fh)
                if stored.get('private') and stored.get('public'):
                    stored['subject'] = subject
                    return stored
            except Exception:
                pass
        try:
            from cryptography.hazmat.primitives import serialization
            from py_vapid import Vapid

            vapid = Vapid()
            vapid.generate_keys()
            private_number = vapid.private_key.private_numbers().private_value
            private_raw = private_number.to_bytes(32, 'big')
            public_raw = vapid.public_key.public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
            keys = {'private': _b64url(private_raw), 'public': _b64url(public_raw)}
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(keys, fh)
            keys['subject'] = subject
            return keys
        except Exception as err:
            print(f'[WEBPUSH] key generation failed: {err}')
            return None


def _target_members(member_name: str) -> list[str]:
    if member_name:
        return [member_name]
    return [m.name for m in FamilyMember.query.all()]


def _push_allowed(member_name: str, event_type: str) -> bool:
    from .notification_service import is_event_enabled_for_member
    member = FamilyMember.query.filter_by(name=member_name).first()
    if member is None:
        return False
    if getattr(member, 'browser_notifications_enabled', True) is False:
        return False
    return bool(is_event_enabled_for_member(member_name, event_type))


def send_push_for_notification(notification) -> int:
    """Deliver a Web Push message for an AppNotification to the target member's devices."""
    try:
        from pywebpush import WebPushException, webpush
    except Exception:
        return 0

    keys = get_vapid_keys()
    if not keys:
        return 0

    url = '/messages' if notification.event_type == 'chat_message' else '/notifications'
    body = json.dumps({
        'id': notification.id,
        'title': notification.title,
        'body': notification.message or '',
        'tag': f'homehub-{notification.id}',
        'url': url,
        'event_type': notification.event_type,
    })

    sent = 0
    for member_name in _target_members(notification.target_member or ''):
        if not _push_allowed(member_name, notification.event_type):
            continue
        subs = WebPushSubscription.query.filter_by(member_name=member_name, enabled=True).all()
        for sub in subs:
            try:
                webpush(
                    subscription_info={'endpoint': sub.endpoint, 'keys': {'p256dh': sub.p256dh, 'auth': sub.auth}},
                    data=body,
                    vapid_private_key=keys['private'],
                    vapid_claims={'sub': keys['subject']},
                    ttl=86400,
                    timeout=10,
                )
                sent += 1
            except WebPushException as err:
                status = getattr(getattr(err, 'response', None), 'status_code', None)
                if status in (404, 410):
                    db.session.delete(sub)
                else:
                    print(f'[WEBPUSH] send failed ({status}): {err}')
            except Exception as err:
                print(f'[WEBPUSH] send error: {err}')
    db.session.commit()
    return sent


def dispatch_push_async(notification_ids: list[int]) -> None:
    """Send pushes off the request thread so slow push services never block the caller."""
    if not notification_ids:
        return
    app = current_app._get_current_object()
    if app.config.get('TESTING'):
        return

    def _run():
        with app.app_context():
            from ..models import AppNotification
            for nid in notification_ids:
                try:
                    row = db.session.get(AppNotification, nid)
                    if row:
                        send_push_for_notification(row)
                except Exception as err:
                    print(f'[WEBPUSH] dispatch error: {err}')
                    db.session.rollback()

    threading.Thread(target=_run, name='webpush-dispatch', daemon=True).start()
