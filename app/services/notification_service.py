import json
import os
import smtplib
from email.mime.text import MIMEText
from typing import Iterable

from flask import current_app

from ..models import AppNotification, FamilyMember, NotificationDeliveryLog, SmtpSettings, db
from ..security import sanitize_text


CHAT_NOTIFICATION_EVENTS = {'chat_message'}
MEDIA_NOTIFICATION_EVENTS = {'shared_cloud_add'}


def _cfg() -> dict:
    return current_app.config.get('HOMEHUB_CONFIG', {}) or {}


def _notifications_cfg() -> dict:
    return _cfg().get('notifications', {}) or {}


def _smtp_cfg() -> dict:
    row = SmtpSettings.query.order_by(SmtpSettings.id.asc()).first()
    if row:
        return {
            'enabled': bool(row.enabled),
            'host': row.host or '',
            'port': int(row.port or 587),
            'secure': bool(row.secure),
            'username': row.username or '',
            'password': row.password or '',
            'sender': row.sender or '',
        }

    cfg = _cfg().get('smtp', {}) or {}

    def _to_bool(value, default=False):
        if value is None:
            return default
        return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}

    return {
        'enabled': _to_bool(os.environ.get('SMTP_ENABLED'), bool(cfg.get('enabled', False))),
        'host': str(os.environ.get('SMTP_HOST') or cfg.get('host', '')).strip(),
        'port': int(os.environ.get('SMTP_PORT') or cfg.get('port', 587) or 587),
        'secure': _to_bool(os.environ.get('SMTP_SECURE'), bool(cfg.get('secure', False))),
        'username': str(os.environ.get('SMTP_USER') or cfg.get('username', '')).strip(),
        'password': str(os.environ.get('SMTP_PASS') or cfg.get('password', '')).strip(),
        'sender': str(os.environ.get('SMTP_FROM') or cfg.get('sender_email', '')).strip(),
    }


def notifications_enabled_for(event_type: str) -> bool:
    categories = _notifications_cfg().get('categories', {}) or {}
    return bool(categories.get(event_type, True))


def resolve_member_email(member_name: str) -> str:
    member = sanitize_text(member_name or '').strip()
    if not member:
        return ''
    row = FamilyMember.query.filter_by(name=member).first()
    profile_email = sanitize_text(getattr(row, 'email', '') or '').strip() if row else ''
    if profile_email:
        return profile_email
    mapping = _notifications_cfg().get('member_emails', {}) or {}
    email = sanitize_text(mapping.get(member, '') or '').strip()
    return email


def _bool_or_default(value, default: bool = True) -> bool:
    if value is None:
        return bool(default)
    return bool(value)


def member_notification_preferences(member_name: str) -> dict[str, bool]:
    member = sanitize_text(member_name or '').strip()
    if not member:
        return {
            'notifications_enabled': True,
            'chat_notifications_enabled': True,
            'media_notifications_enabled': True,
            'browser_notifications_enabled': True,
        }

    row = FamilyMember.query.filter_by(name=member).first()
    if not row:
        return {
            'notifications_enabled': True,
            'chat_notifications_enabled': True,
            'media_notifications_enabled': True,
            'browser_notifications_enabled': True,
        }

    return {
        'notifications_enabled': _bool_or_default(getattr(row, 'notifications_enabled', None), True),
        'chat_notifications_enabled': _bool_or_default(getattr(row, 'chat_notifications_enabled', None), True),
        'media_notifications_enabled': _bool_or_default(getattr(row, 'media_notifications_enabled', None), True),
        'browser_notifications_enabled': _bool_or_default(getattr(row, 'browser_notifications_enabled', None), True),
    }


def is_event_enabled_for_member(member_name: str, event_type: str) -> bool:
    prefs = member_notification_preferences(member_name)
    if not prefs.get('notifications_enabled', True):
        return False

    event_key = sanitize_text(event_type or '').strip().lower()
    if event_key in CHAT_NOTIFICATION_EVENTS and not prefs.get('chat_notifications_enabled', True):
        return False
    if event_key in MEDIA_NOTIFICATION_EVENTS and not prefs.get('media_notifications_enabled', True):
        return False
    return True


def _send_smtp_email(to_email: str, subject: str, body: str, *, force: bool = False) -> bool:
    smtp = _smtp_cfg()
    if not smtp.get('enabled'):
        return False
    if not force and not _notifications_cfg().get('email_enabled'):
        return False

    host = str(smtp.get('host') or '').strip()
    port = int(smtp.get('port') or 587)
    username = str(smtp.get('username') or '').strip()
    password = str(smtp.get('password') or '').strip()
    sender = str(smtp.get('sender') or username).strip()
    use_tls = not bool(smtp.get('secure', False))

    if not host or not sender or not to_email:
        return False

    msg = MIMEText(body or '', 'plain', 'utf-8')
    msg['Subject'] = subject or 'HomeHub Notification'
    msg['From'] = sender
    msg['To'] = to_email

    try:
        server = smtplib.SMTP(host, port, timeout=10)
        try:
            if use_tls:
                server.starttls()
            if username and password:
                server.login(username, password)
            server.sendmail(sender, [to_email], msg.as_string())
        finally:
            server.quit()
        return True
    except Exception:
        return False


def send_welcome_email(
    to_email: str,
    member_name: str,
    *,
    login_url: str,
    temporary_password: str | None = None,
    existing_account: bool = False,
) -> bool:
    target = sanitize_text(to_email or '').strip()
    if not target:
        return False
    name = sanitize_text(member_name or '').strip() or 'there'
    lines = [
        f"Hi {name},",
        "",
        "Welcome to HomeHub, your family's shared home for chores, calendar, photos, shopping lists and more!",
        "",
        "How to log in:",
        f"  1. Open HomeHub: {login_url}",
        f"  2. Enter your email: {target}",
    ]
    if temporary_password:
        lines += [
            f"  3. Enter this temporary password: {temporary_password}",
            "  4. After signing in, change your password from your profile settings.",
        ]
    else:
        lines += ["  3. Enter the password you already use for your HomeHub account."]
    lines += [
        "",
        "Once you're in you can optionally set a 4-digit PIN from your profile for quick sign-in on shared devices.",
        "",
        "If you weren't expecting this email, please let your family admin know.",
        "",
        "- HomeHub",
    ]
    return _send_smtp_email(target, 'Welcome to HomeHub - how to log in', "\n".join(lines), force=True)


def send_test_email(to_email: str, member_name: str | None = None) -> tuple[bool, str]:
    target = sanitize_text(to_email or '').strip()
    if not target:
        return False, 'Email is required'
    name = sanitize_text(member_name or '').strip() or 'HomeHub member'
    subject = 'HomeHub Email Verification'
    body = (
        f"Hi {name},\n\n"
        "This is a test email from HomeHub to confirm your profile email notifications are configured correctly.\n\n"
        "If you received this message, your email notifications are working."
    )
    ok = _send_smtp_email(target, subject, body)
    if ok:
        return True, 'Test email sent successfully'
    return False, 'Failed to send test email. Check SMTP settings.'


def publish_notification(
    *,
    event_type: str,
    title: str,
    message: str,
    target_members: Iterable[str] | None = None,
    created_by: str | None = None,
    payload: dict | None = None,
    channels: Iterable[str] | None = None,
) -> list[AppNotification]:
    if not notifications_enabled_for(event_type):
        return []

    normalized_channels = [sanitize_text(c).strip().lower() for c in (channels or ['in_app'])]
    normalized_channels = [c for c in normalized_channels if c in {'in_app', 'email', 'mobile'}]
    if not normalized_channels:
        normalized_channels = ['in_app']

    members = [sanitize_text(m).strip() for m in (target_members or [])]
    members = [m for m in members if m]
    if not members:
        members = ['']

    created = []
    payload_json = json.dumps(payload or {})
    channels_json = json.dumps(sorted(set(normalized_channels)))

    for member in members:
        if member and not is_event_enabled_for_member(member, event_type):
            continue

        row = AppNotification(
            event_type=sanitize_text(event_type).strip() or 'general',
            title=sanitize_text(title).strip() or 'HomeHub Notification',
            message=sanitize_text(message).strip(),
            target_member=(member or None),
            payload_json=payload_json,
            channels_json=channels_json,
            created_by=sanitize_text(created_by or '').strip() or None,
        )
        db.session.add(row)
        db.session.flush()
        created.append(row)

        if 'in_app' in normalized_channels:
            db.session.add(NotificationDeliveryLog(
                notification_id=row.id,
                member_name=member or '',
                channel='in_app',
                status='sent',
                detail='Stored in in-app feed',
            ))

        if 'email' in normalized_channels:
            email = resolve_member_email(member)
            if email:
                ok = _send_smtp_email(email, row.title, row.message)
                db.session.add(NotificationDeliveryLog(
                    notification_id=row.id,
                    member_name=member or '',
                    channel='email',
                    status='sent' if ok else 'failed',
                    detail=f'Target: {email}',
                ))
            else:
                db.session.add(NotificationDeliveryLog(
                    notification_id=row.id,
                    member_name=member or '',
                    channel='email',
                    status='failed',
                    detail='No member email configured',
                ))

        if 'mobile' in normalized_channels:
            enabled = bool(_notifications_cfg().get('mobile_enabled'))
            if not enabled:
                status = 'failed'
                detail = 'Mobile notifications disabled'
            else:
                # HomeHub custom delivery path (no Firebase): clients receive via SSE + periodic polling.
                status = 'sent'
                detail = 'Custom realtime channel (SSE/poll)'
            db.session.add(NotificationDeliveryLog(
                notification_id=row.id,
                member_name=member or '',
                channel='mobile',
                status=status,
                detail=detail,
            ))

    db.session.commit()
    return created
