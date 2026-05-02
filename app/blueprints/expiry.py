import json
from datetime import datetime, date, timedelta

from flask import render_template, request, redirect, url_for, current_app, jsonify, session

from ..models import db, ExpiryItem, ExpiryReminderNotification, FamilyMember
from ..blueprints import main_bp
from ..security import sanitize_text
from ..services.notification_service import publish_notification


def _current_member_name() -> str:
    member = sanitize_text(session.get('current_member') or '').strip()
    if member:
        return member
    return sanitize_text(current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')).strip() or 'Administrator'


def _admin_aliases() -> set[str]:
    admin_name = sanitize_text(current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')).strip() or 'Administrator'
    return {admin_name, 'Administrator', 'admin'}


def _parse_json_list(raw_value, cast=None):
    if not raw_value:
        return []
    if isinstance(raw_value, list):
        values = raw_value
    else:
        text = str(raw_value).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            values = parsed if isinstance(parsed, list) else []
        except Exception:
            values = []
    out = []
    for value in values:
        try:
            out.append(cast(value) if cast else value)
        except Exception:
            continue
    return out


def _parse_offsets(values) -> list[int]:
    offsets = []
    for value in values or []:
        try:
            parsed = int(str(value).strip())
            if 0 <= parsed <= 365:
                offsets.append(parsed)
        except Exception:
            continue
    if not offsets:
        offsets = [30, 14, 7, 3, 1, 0]
    return sorted(set(offsets), reverse=True)


def _parse_channels(values) -> list[str]:
    allowed = {'in_app', 'email', 'mobile'}
    channels = []
    for value in values or []:
        key = sanitize_text(value or '').strip().lower()
        if key in allowed:
            channels.append(key)
    if not channels:
        channels = ['in_app']
    return sorted(set(channels))


def _targets_for_item(item: ExpiryItem) -> list[str]:
    names = [sanitize_text(item.creator or '').strip()]
    names.extend(_parse_json_list(item.invited_members_json, cast=lambda v: sanitize_text(v or '').strip()))
    return sorted({n for n in names if n})


def _serialize_expiry_item(item: ExpiryItem) -> dict:
    today = date.today()
    days_left = (item.expiry_date - today).days if item.expiry_date else None
    invited = _parse_json_list(item.invited_members_json, cast=lambda v: sanitize_text(v or '').strip())
    offsets = _parse_offsets(_parse_json_list(item.reminder_offsets_json, cast=int))
    channels = _parse_channels(_parse_json_list(item.notify_channels_json, cast=lambda v: sanitize_text(v or '').strip().lower()))
    return {
        'id': item.id,
        'name': item.name,
        'expiry_date': item.expiry_date.isoformat() if item.expiry_date else None,
        'creator': item.creator,
        'timestamp': item.timestamp.isoformat() if item.timestamp else None,
        'days_left': days_left,
        'invited_members': invited,
        'reminder_offsets': offsets,
        'notify_channels': channels,
    }


def _ensure_due_reminders_for_member(member_name: str):
    today = date.today()
    rows = ExpiryItem.query.order_by(ExpiryItem.expiry_date.asc(), ExpiryItem.id.asc()).all()

    for item in rows:
        if not item.expiry_date:
            continue
        if member_name not in _targets_for_item(item):
            continue

        offsets = _parse_offsets(_parse_json_list(item.reminder_offsets_json, cast=int))
        channels = _parse_channels(_parse_json_list(item.notify_channels_json, cast=lambda v: sanitize_text(v or '').strip().lower()))
        days_left = (item.expiry_date - today).days

        for offset in offsets:
            trigger = days_left == offset if offset > 0 else days_left <= 0
            if not trigger:
                continue

            existing_q = ExpiryReminderNotification.query.filter_by(
                expiry_item_id=item.id,
                member_name=member_name,
                reminder_offset_days=offset,
                acknowledged=False,
            )
            if offset > 0:
                existing_q = existing_q.filter_by(due_date=today)
            if existing_q.first():
                continue

            row = ExpiryReminderNotification(
                expiry_item_id=item.id,
                member_name=member_name,
                reminder_offset_days=offset,
                due_date=today,
                acknowledged=False,
            )
            db.session.add(row)
            db.session.flush()

            label = 'expired' if days_left < 0 else f'expires in {days_left} day{"s" if days_left != 1 else ""}'
            publish_notification(
                event_type='expiry',
                title=f"Expiry reminder: {item.name}",
                message=f"{item.name} {label} (expiry date: {item.expiry_date.isoformat()}).",
                target_members=[member_name],
                created_by=item.creator,
                payload={'expiry_item_id': item.id, 'reminder_notification_id': row.id, 'days_left': days_left},
                channels=channels,
            )

    db.session.commit()


@main_bp.route('/expiry', methods=['GET', 'POST'])
def expiry():
    if request.method == 'POST':
        name = sanitize_text(request.form['name'])
        expiry_date = request.form['expiry_date']
        creator = sanitize_text(request.form.get('creator') or '').strip() or _current_member_name()
        invited_members = [sanitize_text(v).strip() for v in request.form.getlist('invited_members')]
        invited_members = sorted({v for v in invited_members if v and v != creator})
        reminder_offsets = _parse_offsets(request.form.getlist('reminder_offsets'))
        channels = _parse_channels(request.form.getlist('notify_channels'))

        expiry_item = ExpiryItem(
            name=name,
            expiry_date=datetime.strptime(expiry_date, '%Y-%m-%d').date(),
            creator=creator,
            invited_members_json=json.dumps(invited_members),
            reminder_offsets_json=json.dumps(reminder_offsets),
            notify_channels_json=json.dumps(channels),
        )
        db.session.add(expiry_item)
        db.session.commit()

        if invited_members:
            publish_notification(
                event_type='expiry',
                title='New expiry item shared',
                message=f"{creator} added '{name}' to Expiry Tracker and invited you.",
                target_members=invited_members,
                created_by=creator,
                payload={'expiry_item_id': expiry_item.id},
                channels=['in_app'] + (['email'] if 'email' in channels else []),
            )
        return redirect(url_for('main.expiry'))

    items = ExpiryItem.query.order_by(ExpiryItem.expiry_date.asc()).all()
    family_members = [m.name for m in FamilyMember.query.order_by(FamilyMember.name.asc()).all()]
    today = date.today()
    # Annotate with days_left for template logic
    enriched = []
    for it in items:
        try:
            days_left = (it.expiry_date - today).days
        except Exception:
            days_left = None
        invited = _parse_json_list(it.invited_members_json, cast=lambda v: sanitize_text(v or '').strip())
        offsets = _parse_offsets(_parse_json_list(it.reminder_offsets_json, cast=int))
        channels = _parse_channels(_parse_json_list(it.notify_channels_json, cast=lambda v: sanitize_text(v or '').strip().lower()))
        enriched.append((it, days_left, invited, offsets, channels))

    config = current_app.config['HOMEHUB_CONFIG']
    return render_template(
        'expiry.html',
        items=enriched,
        today=today,
        config=config,
        family_members=family_members,
        current_member_name=_current_member_name(),
    )


@main_bp.route('/expiry/delete/<int:item_id>', methods=['POST'])
def delete_expiry(item_id):
    it = ExpiryItem.query.get_or_404(item_id)
    user = sanitize_text(request.form['user'])
    if user in _admin_aliases() or user == it.creator:
        db.session.delete(it)
        db.session.commit()
    return redirect(url_for('main.expiry'))


@main_bp.route('/api/expiry/items', methods=['GET'])
def api_get_expiry_items():
    rows = ExpiryItem.query.order_by(ExpiryItem.expiry_date.asc(), ExpiryItem.id.desc()).all()
    return jsonify({'ok': True, 'items': [_serialize_expiry_item(row) for row in rows]})


@main_bp.route('/api/expiry/items', methods=['POST'])
def api_upsert_expiry_item():
    data = request.get_json(silent=True) or {}
    actor = sanitize_text(data.get('member') or '').strip() or _current_member_name()
    item_id = data.get('id')
    name = sanitize_text(data.get('name') or '').strip()
    expiry_raw = sanitize_text(data.get('expiry_date') or '').strip()

    if not name:
        return jsonify({'ok': False, 'error': 'Name is required'}), 400
    if not expiry_raw:
        return jsonify({'ok': False, 'error': 'Expiry date is required'}), 400
    try:
        expiry_date = datetime.strptime(expiry_raw, '%Y-%m-%d').date()
    except Exception:
        return jsonify({'ok': False, 'error': 'Expiry date must be YYYY-MM-DD'}), 400

    invited_members = [sanitize_text(v).strip() for v in (data.get('invited_members') or [])]
    invited_members = sorted({v for v in invited_members if v and v != actor})
    reminder_offsets = _parse_offsets(data.get('reminder_offsets') or [])
    channels = _parse_channels(data.get('notify_channels') or [])

    if item_id:
        item = ExpiryItem.query.get_or_404(int(item_id))
        if actor not in _admin_aliases() and actor != item.creator:
            return jsonify({'ok': False, 'error': 'Not allowed'}), 403

        item.name = name
        item.expiry_date = expiry_date
        item.invited_members_json = json.dumps(invited_members)
        item.reminder_offsets_json = json.dumps(reminder_offsets)
        item.notify_channels_json = json.dumps(channels)
        db.session.commit()
        return jsonify({'ok': True, 'item': _serialize_expiry_item(item), 'message': 'Expiry item updated'})

    item = ExpiryItem(
        name=name,
        expiry_date=expiry_date,
        creator=actor,
        invited_members_json=json.dumps(invited_members),
        reminder_offsets_json=json.dumps(reminder_offsets),
        notify_channels_json=json.dumps(channels),
    )
    db.session.add(item)
    db.session.commit()

    if invited_members:
        publish_notification(
            event_type='expiry',
            title='New expiry item shared',
            message=f"{actor} added '{name}' to Expiry Tracker and invited you.",
            target_members=invited_members,
            created_by=actor,
            payload={'expiry_item_id': item.id},
            channels=['in_app'] + (['email'] if 'email' in channels else []),
        )

    return jsonify({'ok': True, 'item': _serialize_expiry_item(item), 'message': 'Expiry item created'})


@main_bp.route('/api/expiry/items/<int:item_id>', methods=['DELETE'])
def api_delete_expiry_item(item_id: int):
    data = request.get_json(silent=True) or {}
    actor = sanitize_text(data.get('member') or '').strip() or _current_member_name()
    item = ExpiryItem.query.get_or_404(item_id)
    if actor not in _admin_aliases() and actor != item.creator:
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    db.session.delete(item)
    db.session.commit()
    return jsonify({'ok': True, 'message': 'Expiry item deleted'})


@main_bp.route('/api/expiry/reminders/pending', methods=['GET'])
def api_expiry_pending_reminders():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    _ensure_due_reminders_for_member(member)

    rows = (
        ExpiryReminderNotification.query
        .filter_by(member_name=member, acknowledged=False)
        .order_by(ExpiryReminderNotification.created_at.asc(), ExpiryReminderNotification.id.asc())
        .all()
    )

    items = []
    today = date.today()
    for row in rows:
        item = ExpiryItem.query.get(row.expiry_item_id)
        if not item:
            continue
        days_left = (item.expiry_date - today).days if item.expiry_date else None
        items.append({
            'id': row.id,
            'expiry_item_id': item.id,
            'name': item.name,
            'expiry_date': item.expiry_date.isoformat() if item.expiry_date else None,
            'days_left': days_left,
            'can_snooze': bool(days_left is not None and days_left < 0),
            'reminder_offset_days': row.reminder_offset_days,
            'creator': item.creator,
            'created_at': row.created_at.isoformat() if row.created_at else None,
        })

    return jsonify({'ok': True, 'items': items})


@main_bp.route('/api/expiry/reminders/<int:notification_id>/ack', methods=['POST'])
def api_expiry_ack_reminder(notification_id: int):
    row = ExpiryReminderNotification.query.get_or_404(notification_id)
    data = request.get_json(silent=True) or {}
    member = sanitize_text(data.get('member') or '').strip() or _current_member_name()

    if member != row.member_name and member not in _admin_aliases():
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    row.acknowledged = True
    row.acknowledged_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'message': 'Reminder acknowledged'})


@main_bp.route('/api/expiry/reminders/<int:notification_id>/snooze', methods=['POST'])
def api_expiry_snooze_reminder(notification_id: int):
    row = ExpiryReminderNotification.query.get_or_404(notification_id)
    data = request.get_json(silent=True) or {}
    member = sanitize_text(data.get('member') or '').strip() or _current_member_name()
    try:
        snooze_days = max(1, min(30, int(data.get('days') or 1)))
    except Exception:
        snooze_days = 1

    if member != row.member_name and member not in _admin_aliases():
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    item = ExpiryItem.query.get(row.expiry_item_id)
    if not item:
        return jsonify({'ok': False, 'error': 'Expiry item not found'}), 404

    today = date.today()
    days_left = (item.expiry_date - today).days if item.expiry_date else None
    if days_left is None or days_left >= 0:
        return jsonify({'ok': False, 'error': 'Snooze is only available for passed reminders'}), 400

    row.acknowledged = True
    row.acknowledged_at = datetime.utcnow()
    row.snooze_count = int(row.snooze_count or 0) + 1
    due_date = today + timedelta(days=snooze_days)
    row.snoozed_until = due_date
    follow_up = ExpiryReminderNotification(
        expiry_item_id=row.expiry_item_id,
        member_name=row.member_name,
        reminder_offset_days=row.reminder_offset_days,
        due_date=due_date,
        acknowledged=False,
        snooze_count=row.snooze_count,
        snoozed_until=due_date,
    )
    db.session.add(follow_up)
    db.session.commit()

    return jsonify({'ok': True, 'message': f'Reminder snoozed for {snooze_days} day(s)'})
