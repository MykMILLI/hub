from datetime import datetime
import json
import time

from flask import Response, current_app, jsonify, render_template, request, session, stream_with_context
from sqlalchemy import or_

from ..blueprints import main_bp
from ..models import ActivityLog, AppNotification, MobilePushToken, NotificationDeliveryLog, db
from ..security import sanitize_text


def _current_member_name() -> str:
    member = sanitize_text(session.get('current_member') or '').strip()
    if member:
        return member
    return sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator')).strip() or 'Administrator'


@main_bp.route('/notifications', methods=['GET'])
def notifications_page():
    config = current_app.config['HOMEHUB_CONFIG']
    return render_template('notifications.html', config=config, current_member_name=_current_member_name())


@main_bp.route('/api/notifications/mobile/register', methods=['POST'])
def api_register_mobile_token():
    data = request.get_json(silent=True) or {}
    member_name = sanitize_text(data.get('member') or '').strip()
    token = sanitize_text(data.get('token') or '').strip()
    platform = sanitize_text(data.get('platform') or 'unknown').strip().lower()
    provider = sanitize_text(data.get('provider') or 'fcm').strip().lower()

    if not member_name or not token:
        return jsonify({'ok': False, 'error': 'member and token are required'}), 400

    if provider not in {'fcm', 'apns'}:
        provider = 'fcm'

    # Keep metadata compact because the existing db column is short.
    normalized_platform = f"{platform}-{provider}" if platform else provider
    if len(normalized_platform) > 32:
        normalized_platform = normalized_platform[:32]

    row = MobilePushToken.query.filter_by(device_token=token).first()
    if row:
        row.member_name = member_name
        row.platform = normalized_platform or row.platform
        row.enabled = True
    else:
        row = MobilePushToken(member_name=member_name, device_token=token, platform=normalized_platform or 'unknown-fcm', enabled=True)
        db.session.add(row)

    db.session.commit()
    return jsonify({'ok': True, 'message': 'Mobile token registered', 'provider': provider, 'platform': row.platform})


@main_bp.route('/api/notifications/mobile/unregister', methods=['POST'])
def api_unregister_mobile_token():
    data = request.get_json(silent=True) or {}
    token = sanitize_text(data.get('token') or '').strip()
    if not token:
        return jsonify({'ok': False, 'error': 'token is required'}), 400

    row = MobilePushToken.query.filter_by(device_token=token).first()
    if row:
        row.enabled = False
        db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/notifications', methods=['GET'])
def api_get_notifications():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    event_type = sanitize_text(request.args.get('event_type') or '').strip()
    unread_only = str(request.args.get('unread_only') or '').strip().lower() in {'1', 'true', 'yes'}

    q = AppNotification.query.order_by(AppNotification.created_at.desc(), AppNotification.id.desc())
    if member:
        q = q.filter(or_(AppNotification.target_member == member, AppNotification.target_member.is_(None)))
    if event_type:
        q = q.filter(AppNotification.event_type == event_type)
    if unread_only:
        q = q.filter(AppNotification.is_read.is_(False))

    rows = q.limit(100).all()

    categories = [r[0] for r in db.session.query(AppNotification.event_type).distinct().order_by(AppNotification.event_type.asc()).all() if r and r[0]]

    return jsonify({
        'ok': True,
        'categories': categories,
        'items': [
            {
                'id': r.id,
                'event_type': r.event_type,
                'title': r.title,
                'message': r.message,
                'target_member': r.target_member,
                'payload_json': r.payload_json,
                'channels_json': r.channels_json,
                'created_by': r.created_by,
                'is_read': bool(r.is_read),
                'read_at': r.read_at.isoformat() if r.read_at else None,
                'created_at': r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    })


@main_bp.route('/api/notifications/<int:notification_id>/read', methods=['POST'])
def api_mark_notification_read(notification_id: int):
    data = request.get_json(silent=True) or {}
    member = sanitize_text(data.get('member') or '').strip() or _current_member_name()
    row = AppNotification.query.get_or_404(notification_id)
    if row.target_member and row.target_member != member:
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    row.is_read = True
    row.read_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/notifications/mark-read', methods=['POST'])
def api_mark_notifications_read_bulk():
    data = request.get_json(silent=True) or {}
    member = sanitize_text(data.get('member') or '').strip() or _current_member_name()
    event_type = sanitize_text(data.get('event_type') or '').strip()

    q = AppNotification.query.filter(or_(AppNotification.target_member == member, AppNotification.target_member.is_(None)))
    if event_type:
        q = q.filter(AppNotification.event_type == event_type)
    rows = q.all()
    now = datetime.utcnow()
    for row in rows:
        row.is_read = True
        row.read_at = now
    db.session.commit()
    return jsonify({'ok': True, 'count': len(rows)})


@main_bp.route('/api/notifications/history', methods=['GET'])
def api_notifications_delivery_history():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    event_type = sanitize_text(request.args.get('event_type') or '').strip()

    base = db.session.query(NotificationDeliveryLog, AppNotification).join(
        AppNotification,
        AppNotification.id == NotificationDeliveryLog.notification_id,
    )

    base = base.filter(NotificationDeliveryLog.member_name == member)
    if event_type:
        base = base.filter(AppNotification.event_type == event_type)

    rows = base.order_by(NotificationDeliveryLog.created_at.desc(), NotificationDeliveryLog.id.desc()).limit(200).all()
    return jsonify({
        'ok': True,
        'items': [
            {
                'id': log.id,
                'notification_id': notif.id,
                'event_type': notif.event_type,
                'title': notif.title,
                'channel': log.channel,
                'status': log.status,
                'detail': log.detail,
                'created_at': log.created_at.isoformat() if log.created_at else None,
            }
            for log, notif in rows
        ],
    })


@main_bp.route('/api/stream/events', methods=['GET'])
def api_stream_events():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()

    def _unread_count() -> int:
        q = AppNotification.query
        if member:
            q = q.filter(or_(AppNotification.target_member == member, AppNotification.target_member.is_(None)))
        q = q.filter(AppNotification.is_read.is_(False))
        return int(q.count())

    def _latest_notification_id() -> int:
        q = AppNotification.query
        if member:
            q = q.filter(or_(AppNotification.target_member == member, AppNotification.target_member.is_(None)))
        row = q.order_by(AppNotification.id.desc()).first()
        return int(row.id) if row else 0

    def _latest_timeline_id() -> int:
        row = ActivityLog.query.filter(ActivityLog.visibility == 'family').order_by(ActivityLog.id.desc()).first()
        return int(row.id) if row else 0

    @stream_with_context
    def event_stream():
        last_timeline_id = _latest_timeline_id()
        last_notification_id = _latest_notification_id()
        last_unread = _unread_count()

        boot_payload = {
            'timeline_latest_id': last_timeline_id,
            'notification_latest_id': last_notification_id,
            'notification_unread': last_unread,
            'member': member,
        }
        yield f"event: sync\\ndata: {json.dumps(boot_payload)}\\n\\n"

        while True:
            try:
                timeline_id = _latest_timeline_id()
                notification_id = _latest_notification_id()
                unread_count = _unread_count()

                if timeline_id != last_timeline_id:
                    last_timeline_id = timeline_id
                    yield f"event: timeline\\ndata: {json.dumps({'latest_id': timeline_id})}\\n\\n"

                if notification_id != last_notification_id or unread_count != last_unread:
                    last_notification_id = notification_id
                    last_unread = unread_count
                    payload = {
                        'latest_id': notification_id,
                        'unread_count': unread_count,
                    }
                    yield f"event: notifications\\ndata: {json.dumps(payload)}\\n\\n"

                yield "event: heartbeat\\ndata: {}\\n\\n"
                time.sleep(2)
            except GeneratorExit:
                break
            except Exception:
                time.sleep(2)

    headers = {
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
    }
    return Response(event_stream(), mimetype='text/event-stream', headers=headers)
