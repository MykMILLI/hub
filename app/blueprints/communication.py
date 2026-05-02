import json
import os
import base64
import hashlib
import secrets
from datetime import datetime, timedelta

from flask import current_app, jsonify, render_template, request, session
from sqlalchemy import and_, not_, or_
from cryptography.fernet import Fernet, InvalidToken
from werkzeug.utils import secure_filename

from ..models import (
    AppNotification,
    FamilyCallSignal,
    FamilyChatMessage,
    FamilyMember,
    FamilyPoll,
    FamilyPollResponse,
    db,
)
from ..blueprints import main_bp
from ..security import sanitize_text
from ..services.notification_service import publish_notification


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
CHAT_UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads', 'chat')
os.makedirs(CHAT_UPLOAD_DIR, exist_ok=True)


ALLOWED_NOTIFICATION_CHANNELS = {'in_app', 'email', 'mobile'}
_CHAT_LEGACY_MIGRATED = False
CALL_SIGNAL_RETENTION_HOURS = 12
CALL_OFFER_TTL_SECONDS = 90


def _chat_cipher() -> Fernet:
    raw = os.environ.get('CHAT_ENCRYPTION_KEY')
    if raw:
        try:
            return Fernet(raw.encode('utf-8'))
        except ValueError:
            pass

    secret = str(current_app.config.get('SECRET_KEY') or '')
    digest = hashlib.sha256(secret.encode('utf-8')).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def _encrypt_chat_text(value: str) -> str:
    plain = sanitize_text(value or '').strip()
    if not plain:
        return ''
    token = _chat_cipher().encrypt(plain.encode('utf-8')).decode('utf-8')
    return f"enc:v1:{token}"


def _decrypt_chat_text(value: str) -> str:
    raw = str(value or '')
    if not raw:
        return ''
    if not raw.startswith('enc:v1:'):
        return ''
    token = raw[len('enc:v1:'):]
    try:
        return _chat_cipher().decrypt(token.encode('utf-8')).decode('utf-8')
    except (InvalidToken, ValueError):
        return ''


def _migrate_legacy_plaintext_messages() -> None:
    global _CHAT_LEGACY_MIGRATED
    if _CHAT_LEGACY_MIGRATED:
        return

    rows = (
        FamilyChatMessage.query
        .filter(FamilyChatMessage.message.isnot(None))
        .filter(FamilyChatMessage.message != '')
        .filter(not_(FamilyChatMessage.message.like('enc:v1:%')))
        .all()
    )

    if not rows:
        _CHAT_LEGACY_MIGRATED = True
        return

    for row in rows:
        row.message = _encrypt_chat_text(row.message)
    db.session.commit()
    _CHAT_LEGACY_MIGRATED = True


def _current_member_name() -> str:
    member = sanitize_text(session.get('current_member') or '').strip()
    if member:
        return member
    return sanitize_text(current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator'))


def _all_member_names() -> list[str]:
    rows = FamilyMember.query.order_by(FamilyMember.name.asc()).all()
    names = [sanitize_text((row.name or '')).strip() for row in rows]
    return [name for name in names if name]


def _member_exists(member_name: str) -> bool:
    return sanitize_text(member_name or '').strip() in set(_all_member_names())


def _safe_json_list(raw, fallback=None):
    if fallback is None:
        fallback = []
    if not raw:
        return list(fallback)
    if isinstance(raw, list):
        values = raw
    else:
        try:
            parsed = json.loads(raw)
            values = parsed if isinstance(parsed, list) else fallback
        except Exception:
            values = fallback
    return [sanitize_text(str(item or '')).strip() for item in values if sanitize_text(str(item or '')).strip()]


def _cleanup_old_call_signals() -> None:
    cutoff = datetime.utcnow() - timedelta(hours=CALL_SIGNAL_RETENTION_HOURS)
    (
        FamilyCallSignal.query
        .filter(FamilyCallSignal.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.session.commit()


def _webrtc_ice_servers() -> list[dict]:
    servers: list[dict] = []

    stun_urls_raw = sanitize_text(os.environ.get('WEBRTC_STUN_URLS') or '').strip()
    if stun_urls_raw:
        stun_urls = [sanitize_text(part).strip() for part in stun_urls_raw.split(',')]
        stun_urls = [url for url in stun_urls if url]
        if stun_urls:
            servers.append({'urls': stun_urls})

    turn_urls_raw = sanitize_text(os.environ.get('WEBRTC_TURN_URLS') or '').strip()
    turn_username = sanitize_text(os.environ.get('WEBRTC_TURN_USERNAME') or '').strip()
    turn_password = os.environ.get('WEBRTC_TURN_PASSWORD') or ''
    if turn_urls_raw and turn_username and turn_password:
        turn_urls = [sanitize_text(part).strip() for part in turn_urls_raw.split(',')]
        turn_urls = [url for url in turn_urls if url]
        if turn_urls:
            servers.append({
                'urls': turn_urls,
                'username': turn_username,
                'credential': turn_password,
            })

    return servers


def _message_to_dict(row: FamilyChatMessage) -> dict:
    message = _decrypt_chat_text(row.message)
    return {
        'id': row.id,
        'sender': row.sender,
        'recipient': row.recipient,
        'message': message,
        'attachment_filename': row.attachment_filename,
        'attachment_mime': row.attachment_mime,
        'attachment_url': f"/uploads/preview/{row.attachment_filename}" if row.attachment_filename else None,
        'is_read': bool(row.is_read),
        'created_at': row.created_at.isoformat() if row.created_at else None,
    }


@main_bp.route('/messages', methods=['GET'])
def messages_page():
    _migrate_legacy_plaintext_messages()
    config = current_app.config['HOMEHUB_CONFIG']
    return render_template(
        'messages.html',
        config=config,
        current_member_name=_current_member_name(),
        webrtc_ice_servers=_webrtc_ice_servers(),
    )


@main_bp.route('/polls', methods=['GET'])
def polls_page():
    config = current_app.config['HOMEHUB_CONFIG']
    return render_template('polls.html', config=config, current_member_name=_current_member_name())


@main_bp.route('/api/messages/unread-count', methods=['GET'])
def api_messages_unread_count():
    _migrate_legacy_plaintext_messages()
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    count = FamilyChatMessage.query.filter_by(recipient=member, is_read=False).count()
    return jsonify({'ok': True, 'count': int(count)})


@main_bp.route('/api/messages/threads', methods=['GET'])
def api_message_threads():
    _migrate_legacy_plaintext_messages()
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    if not member:
        return jsonify({'ok': True, 'threads': []})

    names = [name for name in _all_member_names() if name != member]
    rows = (
        FamilyChatMessage.query
        .filter(or_(FamilyChatMessage.sender == member, FamilyChatMessage.recipient == member))
        .order_by(FamilyChatMessage.created_at.desc(), FamilyChatMessage.id.desc())
        .all()
    )

    by_other = {}
    for row in rows:
        other = row.recipient if row.sender == member else row.sender
        if other == member:
            continue
        if other not in by_other:
            preview = _decrypt_chat_text(row.message)
            by_other[other] = {
                'member': other,
                'last_message': preview or ('📎 Attachment' if row.attachment_filename else ''),
                'last_at': row.created_at.isoformat() if row.created_at else None,
                'unread': 0,
            }
        if row.recipient == member and not row.is_read:
            by_other[other]['unread'] += 1

    # include empty threads for family members with no chat history yet
    for name in names:
        by_other.setdefault(name, {'member': name, 'last_message': '', 'last_at': None, 'unread': 0})

    threads = list(by_other.values())
    threads.sort(key=lambda item: item.get('last_at') or '', reverse=True)
    return jsonify({'ok': True, 'threads': threads})


@main_bp.route('/api/messages/thread/<other_member>', methods=['GET'])
def api_message_thread(other_member: str):
    _migrate_legacy_plaintext_messages()
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    other = sanitize_text(other_member or '').strip()
    if not member or not other:
        return jsonify({'ok': False, 'error': 'Missing member'}), 400

    rows = (
        FamilyChatMessage.query
        .filter(
            or_(
                and_(FamilyChatMessage.sender == member, FamilyChatMessage.recipient == other),
                and_(FamilyChatMessage.sender == other, FamilyChatMessage.recipient == member),
            )
        )
        .order_by(FamilyChatMessage.created_at.asc(), FamilyChatMessage.id.asc())
        .limit(300)
        .all()
    )
    return jsonify({'ok': True, 'messages': [_message_to_dict(row) for row in rows]})


@main_bp.route('/api/messages/thread/<other_member>/read', methods=['POST'])
def api_message_thread_mark_read(other_member: str):
    member = sanitize_text((request.get_json(silent=True) or {}).get('member') or '').strip() or _current_member_name()
    other = sanitize_text(other_member or '').strip()
    if not member or not other:
        return jsonify({'ok': False, 'error': 'Missing member'}), 400

    rows = (
        FamilyChatMessage.query
        .filter_by(sender=other, recipient=member, is_read=False)
        .all()
    )
    for row in rows:
        row.is_read = True
    db.session.commit()
    return jsonify({'ok': True, 'count': len(rows)})


@main_bp.route('/api/messages/send', methods=['POST'])
def api_send_message():
    sender = sanitize_text(request.form.get('sender') or '').strip() or _current_member_name()
    recipient = sanitize_text(request.form.get('recipient') or '').strip()
    message = sanitize_text(request.form.get('message') or '').strip()
    attachment = request.files.get('attachment')

    if not sender:
        return jsonify({'ok': False, 'error': 'Sender is required'}), 400
    if not recipient:
        return jsonify({'ok': False, 'error': 'Recipient is required'}), 400
    if sender == recipient:
        return jsonify({'ok': False, 'error': 'Select another family member'}), 400
    if not message and not attachment:
        return jsonify({'ok': False, 'error': 'Message or attachment is required'}), 400

    member_names = set(_all_member_names())
    if recipient not in member_names:
        return jsonify({'ok': False, 'error': 'Recipient not found'}), 404

    attachment_filename = None
    attachment_mime = None
    if attachment and getattr(attachment, 'filename', ''):
        safe_name = secure_filename(attachment.filename)
        stamp = datetime.utcnow().strftime('%Y%m%d%H%M%S%f')
        attachment_filename = f"chat_{stamp}_{safe_name}"
        attachment_path = os.path.join(CHAT_UPLOAD_DIR, attachment_filename)
        attachment.save(attachment_path)
        attachment_mime = sanitize_text(getattr(attachment, 'mimetype', '') or '').strip() or None

    row = FamilyChatMessage(
        sender=sender,
        recipient=recipient,
        message=_encrypt_chat_text(message),
        attachment_filename=attachment_filename,
        attachment_mime=attachment_mime,
        is_read=False,
    )
    db.session.add(row)
    db.session.commit()

    publish_notification(
        event_type='chat_message',
        title=f'New message from {sender}',
        message='New secure family message',
        target_members=[recipient],
        created_by=sender,
        payload={'chat_message_id': row.id, 'sender': sender},
        channels=['in_app'],
    )

    return jsonify({'ok': True, 'message': _message_to_dict(row)})


@main_bp.route('/api/calls/session/start', methods=['POST'])
def api_start_call_session():
    data = request.get_json(silent=True) or {}
    caller = sanitize_text(data.get('caller') or '').strip() or _current_member_name()
    callee = sanitize_text(data.get('callee') or '').strip()

    if not caller:
        return jsonify({'ok': False, 'error': 'Caller is required'}), 400
    if not callee:
        return jsonify({'ok': False, 'error': 'Callee is required'}), 400
    if caller == callee:
        return jsonify({'ok': False, 'error': 'Select another family member'}), 400
    if not _member_exists(callee):
        return jsonify({'ok': False, 'error': 'Callee not found'}), 404

    session_id = f"call_{secrets.token_urlsafe(18)}"
    return jsonify({'ok': True, 'session_id': session_id})


@main_bp.route('/api/calls/signal', methods=['POST'])
def api_send_call_signal():
    data = request.get_json(silent=True) or {}
    session_id = sanitize_text(data.get('session_id') or '').strip()
    sender = sanitize_text(data.get('sender') or '').strip() or _current_member_name()
    recipient = sanitize_text(data.get('recipient') or '').strip()
    signal_type = sanitize_text(data.get('signal_type') or '').strip().lower()
    payload = data.get('payload') if isinstance(data.get('payload'), dict) else {}

    allowed_types = {'offer', 'answer', 'ice', 'end', 'reject'}
    if not session_id:
        return jsonify({'ok': False, 'error': 'Session id is required'}), 400
    if not sender:
        return jsonify({'ok': False, 'error': 'Sender is required'}), 400
    if not recipient:
        return jsonify({'ok': False, 'error': 'Recipient is required'}), 400
    if signal_type not in allowed_types:
        return jsonify({'ok': False, 'error': 'Invalid signal type'}), 400
    if not _member_exists(recipient):
        return jsonify({'ok': False, 'error': 'Recipient not found'}), 404

    row = FamilyCallSignal(
        session_id=session_id,
        sender=sender,
        recipient=recipient,
        signal_type=signal_type,
        payload_json=json.dumps(payload),
    )
    db.session.add(row)
    db.session.commit()
    _cleanup_old_call_signals()
    return jsonify({'ok': True, 'signal_id': row.id})


@main_bp.route('/api/calls/signals', methods=['GET'])
def api_get_call_signals():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    after_id = request.args.get('after_id', type=int) or 0

    if not member:
        return jsonify({'ok': True, 'items': []})

    _cleanup_old_call_signals()

    now = datetime.utcnow()

    member_resolved_sessions = {
        row[0]
        for row in (
            db.session.query(FamilyCallSignal.session_id)
            .filter(FamilyCallSignal.sender == member)
            .filter(FamilyCallSignal.signal_type.in_(['answer', 'reject', 'end']))
            .distinct()
            .all()
        )
        if row and row[0]
    }

    rows = (
        FamilyCallSignal.query
        .filter(FamilyCallSignal.recipient == member, FamilyCallSignal.id > after_id)
        .order_by(FamilyCallSignal.id.asc())
        .limit(200)
        .all()
    )

    items = []
    for row in rows:
        if row.signal_type == 'offer':
            age_seconds = (now - (row.created_at or now)).total_seconds()
            if age_seconds > CALL_OFFER_TTL_SECONDS:
                continue
            if row.session_id in member_resolved_sessions:
                continue

        try:
            payload = json.loads(row.payload_json or '{}')
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        items.append({
            'id': row.id,
            'session_id': row.session_id,
            'sender': row.sender,
            'recipient': row.recipient,
            'signal_type': row.signal_type,
            'payload': payload,
            'created_at': row.created_at.isoformat() if row.created_at else None,
        })

    return jsonify({'ok': True, 'items': items})


@main_bp.route('/api/polls/create', methods=['POST'])
def api_create_poll():
    data = request.get_json(silent=True) or {}
    creator = sanitize_text(data.get('creator') or '').strip() or _current_member_name()
    question = sanitize_text(data.get('question') or '').strip()
    options_raw = data.get('options') if isinstance(data.get('options'), list) else []
    recipients_raw = data.get('recipients') if isinstance(data.get('recipients'), list) else []
    channels_raw = data.get('channels') if isinstance(data.get('channels'), list) else ['in_app']

    normalized_options = []
    for option in options_raw:
        if isinstance(option, dict):
            option_value = option.get('value') or option.get('label') or option.get('text') or ''
        else:
            option_value = option
        normalized_options.append(option_value)

    options = [sanitize_text(str(option or '')).strip() for option in normalized_options]
    options = [option for option in options if option]

    if not creator:
        return jsonify({'ok': False, 'error': 'Creator is required'}), 400
    if not question:
        return jsonify({'ok': False, 'error': 'Poll question is required'}), 400
    if len(options) < 2:
        return jsonify({'ok': False, 'error': 'At least two options are required'}), 400

    all_members = _all_member_names()
    if recipients_raw:
        recipients = [sanitize_text(str(name or '')).strip() for name in recipients_raw]
        recipients = [name for name in recipients if name and name in all_members and name != creator]
    else:
        recipients = [name for name in all_members if name != creator]

    if not recipients:
        return jsonify({'ok': False, 'error': 'No recipients selected'}), 400

    channels = []
    for channel in channels_raw:
        c = sanitize_text(str(channel or '')).strip().lower()
        if c in ALLOWED_NOTIFICATION_CHANNELS and c not in channels:
            channels.append(c)
    if not channels:
        channels = ['in_app']

    poll = FamilyPoll(
        creator=creator,
        question=question,
        options_json=json.dumps(options),
        recipients_json=json.dumps(recipients),
        channels_json=json.dumps(channels),
        is_active=True,
    )
    db.session.add(poll)
    db.session.commit()

    publish_notification(
        event_type='poll_created',
        title='New family poll',
        message=f"{creator}: {question}",
        target_members=recipients,
        created_by=creator,
        payload={'poll_id': poll.id},
        channels=channels,
    )

    return jsonify({'ok': True, 'poll_id': poll.id})


@main_bp.route('/api/polls/pending', methods=['GET'])
def api_pending_polls():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    if not member:
        return jsonify({'ok': True, 'items': []})

    rows = FamilyPoll.query.filter_by(is_active=True).order_by(FamilyPoll.created_at.asc(), FamilyPoll.id.asc()).all()
    items = []
    for row in rows:
        recipients = _safe_json_list(row.recipients_json)
        if member not in recipients:
            continue
        existing = FamilyPollResponse.query.filter_by(poll_id=row.id, member_name=member).first()
        if existing:
            continue
        items.append({
            'id': row.id,
            'creator': row.creator,
            'question': row.question,
            'options': _safe_json_list(row.options_json),
            'created_at': row.created_at.isoformat() if row.created_at else None,
        })

    return jsonify({'ok': True, 'items': items})


@main_bp.route('/api/polls/pending-count', methods=['GET'])
def api_pending_polls_count():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    if not member:
        return jsonify({'ok': True, 'count': 0})

    rows = FamilyPoll.query.filter_by(is_active=True).all()
    count = 0
    for row in rows:
        recipients = _safe_json_list(row.recipients_json)
        if member not in recipients:
            continue
        existing = FamilyPollResponse.query.filter_by(poll_id=row.id, member_name=member).first()
        if existing:
            continue
        count += 1
    return jsonify({'ok': True, 'count': count})


@main_bp.route('/api/polls/<int:poll_id>/vote', methods=['POST'])
def api_vote_poll(poll_id: int):
    poll = FamilyPoll.query.get_or_404(poll_id)
    if not poll.is_active:
        return jsonify({'ok': False, 'error': 'Poll is closed'}), 400

    data = request.get_json(silent=True) or {}
    member = sanitize_text(data.get('member') or '').strip() or _current_member_name()
    selected_option = sanitize_text(data.get('selected_option') or '').strip()
    response_text = sanitize_text(data.get('response_text') or '').strip()

    recipients = _safe_json_list(poll.recipients_json)
    options = _safe_json_list(poll.options_json)
    if member not in recipients:
        return jsonify({'ok': False, 'error': 'Not allowed to vote on this poll'}), 403
    if selected_option and selected_option not in options:
        return jsonify({'ok': False, 'error': 'Invalid option'}), 400
    if not selected_option and not response_text:
        return jsonify({'ok': False, 'error': 'Choose an option or enter a response'}), 400

    row = FamilyPollResponse.query.filter_by(poll_id=poll.id, member_name=member).first()
    if not row:
        row = FamilyPollResponse(poll_id=poll.id, member_name=member)
        db.session.add(row)
    row.selected_option = selected_option or None
    row.response_text = response_text or None
    db.session.commit()

    response_summary = selected_option or response_text or 'Responded'
    publish_notification(
        event_type='poll_response',
        title=f'Poll response from {member}',
        message=f"{member} responded to your poll: {response_summary}",
        target_members=[poll.creator],
        created_by=member,
        payload={'poll_id': poll.id, 'responder': member, 'selected_option': selected_option, 'response_text': response_text},
        channels=['in_app', 'email', 'mobile'],
    )

    # Auto-close poll when all recipients responded
    recipient_count = len(recipients)
    response_count = FamilyPollResponse.query.filter_by(poll_id=poll.id).count()
    if recipient_count > 0 and response_count >= recipient_count:
        poll.is_active = False
        poll.closed_at = datetime.utcnow()
        db.session.commit()

    return jsonify({'ok': True})


@main_bp.route('/api/polls/mine', methods=['GET'])
def api_my_polls():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    if not member:
        return jsonify({'ok': True, 'items': []})

    polls = FamilyPoll.query.filter_by(creator=member).order_by(FamilyPoll.created_at.desc(), FamilyPoll.id.desc()).limit(50).all()
    items = []
    for poll in polls:
        responses = FamilyPollResponse.query.filter_by(poll_id=poll.id).order_by(FamilyPollResponse.created_at.asc()).all()
        response_items = [
            {
                'member_name': row.member_name,
                'selected_option': row.selected_option,
                'response_text': row.response_text,
                'created_at': row.created_at.isoformat() if row.created_at else None,
            }
            for row in responses
        ]
        recipients = _safe_json_list(poll.recipients_json)
        items.append({
            'id': poll.id,
            'question': poll.question,
            'options': _safe_json_list(poll.options_json),
            'recipients': recipients,
            'responses': response_items,
            'response_count': len(response_items),
            'recipient_count': len(recipients),
            'is_active': bool(poll.is_active),
            'created_at': poll.created_at.isoformat() if poll.created_at else None,
        })

    return jsonify({'ok': True, 'items': items})


@main_bp.route('/api/polls/response-alerts', methods=['GET'])
def api_poll_response_alerts():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    if not member:
        return jsonify({'ok': True, 'items': []})

    rows = (
        AppNotification.query
        .filter_by(event_type='poll_response', target_member=member, is_read=False)
        .order_by(AppNotification.created_at.asc(), AppNotification.id.asc())
        .all()
    )

    return jsonify({'ok': True, 'items': [
        {
            'id': row.id,
            'title': row.title,
            'message': row.message,
            'created_at': row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]})
