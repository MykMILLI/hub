import json
from datetime import date, datetime, timedelta

from flask import current_app, jsonify, render_template, request, session

from ..activity import log_activity
from ..blueprints import main_bp
from ..models import FamilyMember, MealPlanEntry, Recipe, db
from ..security import sanitize_text
from ..services.notification_service import publish_notification


MEAL_TYPES = ['breakfast', 'morning_snack', 'lunch', 'afternoon_snack', 'dinner']
VALID_VISIBILITY = {'family', 'restricted', 'private'}
VALID_AUDIENCE = {'family', 'adults', 'kids', 'custom'}


def _current_member_name() -> str:
    member = sanitize_text(session.get('current_member') or '').strip()
    if member:
        return member
    return sanitize_text(current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator'))


def _is_admin(user: str) -> bool:
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    return sanitize_text(user or '') in {admin_name, 'Administrator', 'admin'}


def _parse_allowed_members(raw_value) -> list[str]:
    if not raw_value:
        return []
    if isinstance(raw_value, list):
        values = raw_value
    else:
        text = str(raw_value or '').strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            values = parsed if isinstance(parsed, list) else text.split(',')
        except Exception:
            values = text.split(',')
    cleaned = [sanitize_text(str(v)).strip() for v in values]
    return sorted({v for v in cleaned if v})


def _can_view_entry(entry: MealPlanEntry, member: str) -> bool:
    if _is_admin(member):
        return True
    visibility = (entry.visibility or 'family').strip().lower()
    if visibility == 'family':
        return True
    if visibility == 'private':
        return member == entry.creator
    allowed = _parse_allowed_members(entry.allowed_members_json)
    return member == entry.creator or member in allowed


def _can_edit_entry(entry: MealPlanEntry, member: str) -> bool:
    return _is_admin(member) or member == entry.creator


def _member_pin_matches(member: str, pin: str) -> bool:
    clean_member = sanitize_text(member or '').strip()
    clean_pin = sanitize_text(pin or '').strip()
    if not clean_member or not clean_pin:
        return False
    row = FamilyMember.query.filter_by(name=clean_member).first()
    return bool(row and (row.pin or '') == clean_pin)


def _parse_date(value: str | None) -> date | None:
    raw = sanitize_text(value or '').strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except Exception:
        return None


def _month_range(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


@main_bp.route('/meal-planner')
def meal_planner_page():
    config = current_app.config['HOMEHUB_CONFIG']
    return render_template('meal_planner.html', config=config, meal_types=MEAL_TYPES, current_member_name=_current_member_name())


@main_bp.route('/api/meal-planner/entries', methods=['GET'])
def api_meal_planner_entries():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    start = _parse_date(request.args.get('start'))
    end = _parse_date(request.args.get('end'))
    today = date.today()
    year = int(request.args.get('year') or today.year)
    month = int(request.args.get('month') or today.month)

    if not (start and end):
        start, end = _month_range(year, month)
    else:
        if start > end:
            start, end = end, start
        year = start.year
        month = start.month

    unlocked_ids = set(session.get('unlocked_meal_entries', []))
    rows = (
        MealPlanEntry.query
        .filter(MealPlanEntry.date >= start, MealPlanEntry.date <= end)
        .order_by(MealPlanEntry.date.asc(), MealPlanEntry.meal_type.asc(), MealPlanEntry.id.asc())
        .all()
    )

    entries = []
    for row in rows:
        if not _can_view_entry(row, member):
            continue
        locked = bool(row.require_pin) and (member != row.creator) and (row.id not in unlocked_ids)
        entries.append({
            'id': row.id,
            'date': row.date.isoformat() if row.date else None,
            'meal_type': row.meal_type,
            'recipe_id': row.recipe_id,
            'title': row.title,
            'description': row.description,
            'creator': row.creator,
            'visibility': row.visibility or 'family',
            'allowed_members': _parse_allowed_members(row.allowed_members_json),
            'require_pin': bool(row.require_pin),
            'audience': sanitize_text(row.audience or 'family').strip().lower() or 'family',
            'timestamp': row.timestamp.isoformat() if row.timestamp else None,
            'updated_at': row.updated_at.isoformat() if row.updated_at else None,
            'is_locked': locked,
            'can_edit': _can_edit_entry(row, member),
        })

    return jsonify({
        'ok': True,
        'entries': entries,
        'meal_types': MEAL_TYPES,
        'year': year,
        'month': month,
        'start': start.isoformat() if start else None,
        'end': end.isoformat() if end else None,
    })


@main_bp.route('/api/meal-planner/library', methods=['GET'])
def api_meal_planner_library():
    member = sanitize_text(request.args.get('member') or '').strip() or _current_member_name()
    rows = (
        MealPlanEntry.query
        .order_by(MealPlanEntry.updated_at.desc(), MealPlanEntry.timestamp.desc(), MealPlanEntry.id.desc())
        .all()
    )

    seen = set()
    library = []
    for row in rows:
        if not _can_view_entry(row, member):
            continue
        title = sanitize_text(row.title or '').strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        library.append({
            'title': title,
            'meal_type': sanitize_text(row.meal_type or '').strip().lower(),
            'description': sanitize_text(row.description or '').strip(),
            'last_used_date': row.date.isoformat() if row.date else None,
        })
        if len(library) >= 300:
            break

    return jsonify({'ok': True, 'items': library})


@main_bp.route('/api/meal-planner/entries', methods=['POST'])
def api_upsert_meal_planner_entry():
    data = request.get_json(silent=True) or {}
    member = sanitize_text(data.get('member') or '').strip() or _current_member_name()

    entry_id = data.get('id')
    meal_date = _parse_date(data.get('date'))
    meal_type = sanitize_text(data.get('meal_type') or '').strip().lower()
    raw_recipe_id = data.get('recipe_id')
    recipe_id = None
    if raw_recipe_id not in (None, '', 0, '0'):
        try:
            recipe_id = int(raw_recipe_id)
        except Exception:
            return jsonify({'ok': False, 'error': 'Invalid recipe reference'}), 400
        if recipe_id and not Recipe.query.get(recipe_id):
            return jsonify({'ok': False, 'error': 'Recipe not found'}), 400
    title = sanitize_text(data.get('title') or '').strip()
    description = sanitize_text(data.get('description') or '').strip()
    visibility = sanitize_text(data.get('visibility') or 'family').strip().lower()
    require_pin = str(data.get('require_pin') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    audience = sanitize_text(data.get('audience') or 'family').strip().lower()
    allowed_members = _parse_allowed_members(data.get('allowed_members'))

    if not meal_date:
        return jsonify({'ok': False, 'error': 'Date is required'}), 400
    if meal_type not in MEAL_TYPES:
        return jsonify({'ok': False, 'error': 'Invalid meal type'}), 400
    if not title:
        return jsonify({'ok': False, 'error': 'Title is required'}), 400
    if visibility not in VALID_VISIBILITY:
        visibility = 'family'
    if audience not in VALID_AUDIENCE:
        audience = 'family'
    if visibility == 'private':
        allowed_members = []
    elif visibility == 'restricted' and not allowed_members:
        visibility = 'private'

    if entry_id:
        entry = MealPlanEntry.query.get_or_404(int(entry_id))
        if not _can_edit_entry(entry, member):
            return jsonify({'ok': False, 'error': 'Not allowed'}), 403

        entry.date = meal_date
        entry.meal_type = meal_type
        entry.recipe_id = recipe_id
        entry.title = title
        entry.description = description
        entry.visibility = visibility
        entry.allowed_members_json = json.dumps(allowed_members)
        entry.require_pin = bool(require_pin)
        entry.audience = audience
        db.session.commit()

        if visibility == 'family':
            plan_when = meal_date.strftime('%A, %b %d, %Y')
            log_activity(
                actor=member,
                action='meal.plan.update',
                summary=f"updated meal plan for {plan_when}: {title} ({meal_type.replace('_', ' ')}, {audience})",
                entity_type='meal_plan_entry',
                entity_id=entry.id,
                visibility='family',
                metadata={'date': meal_date.isoformat(), 'meal_type': meal_type},
            )
            if meal_date == date.today():
                targets = [m.name for m in FamilyMember.query.order_by(FamilyMember.name.asc()).all() if m.name and m.name != member]
                publish_notification(
                    event_type='meal_today',
                    title='Meal plan updated for today',
                    message=f"{member} updated today's {meal_type.replace('_', ' ')}: {title}",
                    target_members=targets,
                    created_by=member,
                    payload={'meal_plan_entry_id': entry.id, 'date': meal_date.isoformat()},
                    channels=['in_app'],
                )
        return jsonify({'ok': True, 'message': 'Meal plan updated', 'id': entry.id})

    entry = MealPlanEntry(
        date=meal_date,
        meal_type=meal_type,
        recipe_id=recipe_id,
        title=title,
        description=description,
        creator=member,
        visibility=visibility,
        allowed_members_json=json.dumps(allowed_members),
        require_pin=bool(require_pin),
            audience=audience,
    )
    db.session.add(entry)
    db.session.commit()

    if visibility == 'family':
        plan_when = meal_date.strftime('%A, %b %d, %Y')
        log_activity(
            actor=member,
            action='meal.plan.add',
            summary=f"planned meal for {plan_when}: {title} ({meal_type.replace('_', ' ')}, {audience})",
            entity_type='meal_plan_entry',
            entity_id=entry.id,
            visibility='family',
            metadata={'date': meal_date.isoformat(), 'meal_type': meal_type},
        )
        if meal_date == date.today():
            targets = [m.name for m in FamilyMember.query.order_by(FamilyMember.name.asc()).all() if m.name and m.name != member]
            publish_notification(
                event_type='meal_today',
                title='New meal plan for today',
                message=f"{member} added today's {meal_type.replace('_', ' ')}: {title}",
                target_members=targets,
                created_by=member,
                payload={'meal_plan_entry_id': entry.id, 'date': meal_date.isoformat()},
                channels=['in_app'],
            )

    return jsonify({'ok': True, 'message': 'Meal plan added', 'id': entry.id})


@main_bp.route('/api/meal-planner/entries/<int:entry_id>', methods=['DELETE'])
def api_delete_meal_planner_entry(entry_id: int):
    data = request.get_json(silent=True) or {}
    member = sanitize_text(data.get('member') or '').strip() or _current_member_name()
    entry = MealPlanEntry.query.get_or_404(entry_id)

    if not _can_edit_entry(entry, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    title = entry.title
    meal_type = entry.meal_type
    meal_date = entry.date
    visibility = entry.visibility or 'family'

    db.session.delete(entry)
    db.session.commit()

    if visibility == 'family':
        log_activity(
            actor=member,
            action='meal.plan.delete',
            summary=f"removed meal plan: {title} ({meal_type.replace('_', ' ')})",
            entity_type='meal_plan_entry',
            entity_id=entry_id,
            visibility='family',
            metadata={'date': meal_date.isoformat() if meal_date else None, 'meal_type': meal_type},
        )

    return jsonify({'ok': True, 'message': 'Meal plan removed'})


@main_bp.route('/api/meal-planner/entries/<int:entry_id>/unlock', methods=['POST'])
def api_unlock_meal_planner_entry(entry_id: int):
    entry = MealPlanEntry.query.get_or_404(entry_id)
    data = request.get_json(silent=True) or {}
    member = sanitize_text(data.get('member') or '').strip() or _current_member_name()
    pin = sanitize_text(data.get('pin') or '').strip()

    if not _can_view_entry(entry, member):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403
    if not entry.require_pin:
        return jsonify({'ok': True, 'message': 'Entry is not PIN-protected'})
    if member == entry.creator:
        return jsonify({'ok': True, 'message': 'Creator access granted'})
    if not _member_pin_matches(member, pin):
        return jsonify({'ok': False, 'error': 'Invalid PIN'}), 403

    unlocked = set(session.get('unlocked_meal_entries', []))
    unlocked.add(int(entry.id))
    session['unlocked_meal_entries'] = sorted(unlocked)
    return jsonify({'ok': True, 'message': 'Meal entry unlocked'})
