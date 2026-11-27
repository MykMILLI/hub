"""Family member invites: one-time links + a clear, plain-language email."""
from __future__ import annotations

import hashlib
import html
import secrets
from datetime import datetime, timedelta

from flask import current_app, request

from .. import db
from ..models import FamilyMember, HubSettings, MemberInvite
from ..permissions import (
    ACCESS_TIER_LABELS,
    DISABLED_APP_MODULES,
    MODULE_PLAIN_DESCRIPTIONS,
    can_access_app_module,
    normalize_tier,
)
from ..security import sanitize_text
from .member_accounts import is_placeholder_email

INVITE_DAYS = 7


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def hub_name() -> str:
    try:
        row = HubSettings.query.first()
        if row and (row.hub_name or '').strip() and row.hub_name.strip() != 'HomeHub':
            return row.hub_name.strip()
    except Exception:
        pass
    cfg = current_app.config.get('HOMEHUB_CONFIG', {}) or {}
    return str(cfg.get('instance_name') or 'Home Hub').strip() or 'Home Hub'


def public_base_url() -> str:
    """Where family members open Home Hub. Set `public_url` in config.yml (or
    HOMEHUB_PUBLIC_URL) when the hub is reached through a different address
    than the one the admin is using, e.g. a Tailscale or reverse-proxy URL."""
    import os

    cfg = current_app.config.get('HOMEHUB_CONFIG', {}) or {}
    configured = str(os.environ.get('HOMEHUB_PUBLIC_URL') or cfg.get('public_url') or '').strip().rstrip('/')
    if configured:
        return configured
    try:
        return request.host_url.rstrip('/')
    except Exception:
        return ''


def visible_areas_for(member: FamilyMember) -> list[str]:
    """Plain-language list of what this member will be able to open."""
    from ..permissions import APP_MODULES

    areas = []
    for key in APP_MODULES:
        if key in DISABLED_APP_MODULES or key in {'welcome', 'settings'}:
            continue
        try:
            if can_access_app_module(key, member.name):
                desc = MODULE_PLAIN_DESCRIPTIONS.get(key)
                if desc and desc not in areas:
                    areas.append(desc)
        except Exception:
            continue
    return areas


def sign_in_mode() -> str:
    cfg = current_app.config.get('HOMEHUB_CONFIG', {}) or {}
    supa = cfg.get('supabase') if isinstance(cfg.get('supabase'), dict) else {}
    return 'password' if supa.get('auth_enabled') else 'pin'


def create_invite(member: FamilyMember, invited_by: str) -> tuple[MemberInvite, str]:
    email = sanitize_text(member.email or '').strip()
    if not email or is_placeholder_email(email):
        raise ValueError('Add a real email address for this person first.')
    now = datetime.utcnow()
    # Only the newest link works.
    for old in MemberInvite.query.filter_by(member_id=member.id, accepted_at=None, revoked_at=None).all():
        old.revoked_at = now
    token = secrets.token_urlsafe(32)
    invite = MemberInvite(
        member_id=member.id,
        email=email,
        token_hash=_hash(token),
        invited_by=sanitize_text(invited_by or '').strip() or 'Your family',
        expires_at=now + timedelta(days=INVITE_DAYS),
    )
    db.session.add(invite)
    db.session.commit()
    return invite, token


def find_valid_invite(token: str) -> tuple[MemberInvite | None, FamilyMember | None, str]:
    token = (token or '').strip()
    if not token or len(token) > 200:
        return None, None, 'This invite link is not valid.'
    invite = MemberInvite.query.filter_by(token_hash=_hash(token)).first()
    if not invite:
        return None, None, 'This invite link is not valid.'
    if invite.revoked_at:
        return invite, None, 'A newer invite was sent. Please use the most recent email.'
    if invite.accepted_at:
        return invite, None, 'This invite was already used. Sign in normally instead.'
    if invite.expires_at < datetime.utcnow():
        return invite, None, 'This invite has expired. Ask the person who invited you to send a new one.'
    member = db.session.get(FamilyMember, invite.member_id)
    if not member:
        return invite, None, 'This invite is no longer valid.'
    return invite, member, ''


def build_invite_email(member: FamilyMember, invite_url: str, invited_by: str, login_url: str) -> tuple[str, str, str]:
    """Returns (subject, plain_text, html)."""
    name = sanitize_text(member.name or '').strip() or 'there'
    hub = hub_name()
    tier = normalize_tier(getattr(member, 'access_tier', 'immediate'))
    tier_label = ACCESS_TIER_LABELS[tier]
    areas = visible_areas_for(member)
    mode = sign_in_mode()
    secret_word = 'password' if mode == 'password' else '4-digit PIN'

    subject = f'{invited_by} invited you to {hub}'
    steps = [
        f'Open your invite: tap the "Join {hub}" button (or copy the invite link into your browser). It works once, for {INVITE_DAYS} days.',
        f'Check that the name shown is yours ({name}), then choose a {secret_word} you will remember.',
        f'You are in. Next time, open {login_url}, tap your name and enter your {secret_word}.',
        'Tip: on a phone, use "Add to Home Screen" from your browser menu so Home Hub opens like an app.',
    ]

    text_lines = [
        f'Hi {name},',
        '',
        f'{invited_by} added you to {hub}, our private family hub for calendars, lists and updates.',
        '',
        'GET STARTED (about 1 minute)',
    ]
    text_lines += [f'  {i}. {step}' for i, step in enumerate(steps, 1)]
    text_lines += ['', f'Your invite link: {invite_url}', '', f'WHAT YOU CAN SEE ({tier_label})']
    text_lines += [f'  - {a}' for a in areas] or ['  - the family home page']
    text_lines += [
        '',
        'Some things are kept just for certain people (for example private events or household-only lists), so you will not see those.',
        f'If something looks missing, or you need more access, let {invited_by} know.',
        '',
        "Didn't expect this email? You can ignore it; nothing happens unless the link is used.",
        '',
        f'- {hub}',
    ]
    text = '\n'.join(text_lines)

    e = html.escape
    areas_html = ''.join(f'<li style="margin:4px 0">{e(a[:1].upper() + a[1:])}</li>' for a in areas) or '<li>The family home page</li>'
    steps_html = ''.join(
        f'<tr><td style="vertical-align:top;padding:6px 10px 6px 0"><span style="display:inline-block;width:26px;height:26px;border-radius:13px;background:#7c3aed;color:#fff;text-align:center;line-height:26px;font-weight:700">{i}</span></td>'
        f'<td style="padding:6px 0;color:#3a2758;font-size:15px;line-height:1.45">{e(step)}</td></tr>'
        for i, step in enumerate(steps, 1)
    )
    html_body = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f5f3ff;font-family:Segoe UI,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f3ff;padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:18px;overflow:hidden;border:1px solid #e9d5ff">
<tr><td style="background:linear-gradient(135deg,#fde68a,#e9d5ff,#c7d2fe);padding:26px 28px">
  <div style="font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:#6d28d9;font-weight:700">{e(hub)}</div>
  <div style="font-size:24px;font-weight:800;color:#2e1065;margin-top:6px">Hi {e(name)}, you're invited!</div>
  <div style="font-size:15px;color:#4c1d95;margin-top:6px">{e(invited_by)} added you to our private family hub.</div>
</td></tr>
<tr><td style="padding:24px 28px 8px">
  <div style="font-size:16px;font-weight:700;color:#2e1065;margin-bottom:6px">Get started (about 1 minute)</div>
  <table role="presentation" cellpadding="0" cellspacing="0">{steps_html}</table>
  <div style="text-align:center;margin:22px 0 8px">
    <a href="{e(invite_url)}" style="display:inline-block;background:#7c3aed;color:#ffffff;text-decoration:none;font-weight:700;font-size:16px;padding:14px 26px;border-radius:12px">Join {e(hub)}</a>
  </div>
  <div style="font-size:12px;color:#6b7280;text-align:center;word-break:break-all">Or copy this link: {e(invite_url)}</div>
</td></tr>
<tr><td style="padding:16px 28px">
  <div style="background:#faf5ff;border:1px solid #e9d5ff;border-radius:14px;padding:14px 16px">
    <div style="font-size:14px;font-weight:700;color:#5b21b6">What you can see · {e(tier_label)}</div>
    <ul style="margin:8px 0 0 18px;padding:0;color:#3a2758;font-size:14px">{areas_html}</ul>
    <div style="font-size:12px;color:#6b7280;margin-top:8px">Some things are kept just for certain people (like private events or household-only lists), so you won't see those.</div>
  </div>
</td></tr>
<tr><td style="padding:4px 28px 26px;font-size:13px;color:#6b7280;line-height:1.5">
  Need help or more access? Just let {e(invited_by)} know.<br>
  Didn't expect this? You can ignore this email; nothing happens unless the link is used.
</td></tr>
</table>
</td></tr></table>
</body></html>"""
    return subject, text, html_body


def send_invite(member: FamilyMember, invited_by: str) -> dict:
    from .notification_service import send_html_email

    invite, token = create_invite(member, invited_by)
    base = public_base_url()
    invite_url = f'{base}/invite/{token}'
    subject, text, html_body = build_invite_email(member, invite_url, invite.invited_by, f'{base}/')
    sent = False
    try:
        sent = send_html_email(invite.email, subject, text, html_body)
    except Exception:
        sent = False
    invite.email_sent = bool(sent)
    db.session.commit()
    return {
        'sent': bool(sent),
        'invite_id': invite.id,
        'expires_at': invite.expires_at.isoformat() + 'Z',
        # Shown once to the admin so they can text it if email isn't set up.
        'invite_url': invite_url,
        'email_configured': _smtp_ready(),
    }


def _smtp_ready() -> bool:
    try:
        from .notification_service import _smtp_cfg

        smtp = _smtp_cfg()
        return bool(smtp.get('enabled') and smtp.get('host'))
    except Exception:
        return False
