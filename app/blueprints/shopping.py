from flask import render_template, request, redirect, url_for, current_app, jsonify, session, send_from_directory
from datetime import datetime, timedelta
from ..models import (
    db,
    ShoppingItem,
    ShoppingList,
    ShoppingRepoItem,
    GroceryHistory,
    CommonItem,
    ExpenseEntry,
    FamilyMember,
)
from ..blueprints import main_bp
from ..security import sanitize_text
from ..activity import log_activity
from ..services.notification_service import publish_notification
import json
import os
from uuid import uuid4
from werkzeug.utils import secure_filename


_SHOPPING_PHOTO_EXTS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}


def _normalize_list_type(raw) -> str:
    value = sanitize_text(str(raw or 'standard')).strip().lower()
    return 'big_purchase' if value == 'big_purchase' else 'standard'


def _shopping_purchase_upload_dir() -> str:
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    folder = os.path.join(base_dir, 'uploads', 'shopping-purchases')
    os.makedirs(folder, exist_ok=True)
    return folder


def _purchase_photo_url(filename: str | None) -> str | None:
    if not filename:
        return None
    return url_for('main.uploaded_shopping_purchase_photo', filename=filename)


def _json_tags(raw):
    tags_list = []
    if not raw:
        return tags_list
    try:
        tags_list = json.loads(raw)
        if not isinstance(tags_list, list):
            tags_list = []
    except Exception:
        tags_list = [t.strip() for t in str(raw).split(',') if t.strip()]
    return [sanitize_text(t) for t in tags_list if isinstance(t, str) and t.strip()]


def _to_float(value, default=None):
    try:
        f = float(value)
        return f
    except Exception:
        return default


def _serialize_item(item: ShoppingItem):
    try:
        tags = json.loads(item.tags or '[]')
        if not isinstance(tags, list):
            tags = []
    except Exception:
        tags = []
    return {
        'id': item.id,
        'item': item.item,
        'checked': bool(item.checked),
        'creator': item.creator,
        'timestamp': item.timestamp.isoformat() if item.timestamp else None,
        'tags': tags,
        'price': item.price,
        'quantity': item.quantity or 1.0,
        'list_id': item.list_id,
        'repo_item_id': item.repo_item_id,
        'purchase_photo_filename': item.purchase_photo_filename,
        'purchase_photo_url': _purchase_photo_url(item.purchase_photo_filename),
        'size_requirements': item.size_requirements,
        'target_price': item.target_price,
        'budget_ceiling': item.budget_ceiling,
        'planning_notes': item.planning_notes,
        'purchase_category': item.purchase_category,
    }


def _serialize_repo_item(repo: ShoppingRepoItem):
    return {
        'id': repo.id,
        'name': repo.name,
        'store': repo.store,
        'category': repo.category,
        'price': repo.default_price,
        'last_used': repo.last_used.isoformat() if repo.last_used else None,
        'created_by': repo.created_by,
    }


def _ensure_default_shopping_list() -> ShoppingList:
    default_list = ShoppingList.query.order_by(ShoppingList.created_at.asc(), ShoppingList.id.asc()).first()
    if not default_list:
        default_list = ShoppingList(name='General', color='#3B82F6', list_type='standard', creator='System')
        db.session.add(default_list)
        db.session.flush()
    ShoppingItem.query.filter(ShoppingItem.list_id.is_(None)).update({'list_id': default_list.id}, synchronize_session=False)
    db.session.commit()
    return default_list


def _upsert_repo_item(name: str, store: str | None, category: str | None, price: float | None, creator: str | None):
    item_name = sanitize_text((name or '').strip())
    if not item_name:
        return None
    item_store = sanitize_text((store or '').strip()) or None
    item_category = sanitize_text((category or '').strip()) or None

    repo_item = ShoppingRepoItem.query.filter(
        db.func.lower(ShoppingRepoItem.name) == item_name.lower(),
        db.func.lower(db.func.coalesce(ShoppingRepoItem.store, '')) == (item_store or '').lower(),
    ).first()

    if not repo_item:
        repo_item = ShoppingRepoItem(
            name=item_name,
            store=item_store,
            category=item_category,
            default_price=price,
            created_by=creator,
            last_used=datetime.utcnow(),
        )
        db.session.add(repo_item)
    else:
        if price is not None:
            repo_item.default_price = price
        if item_category:
            repo_item.category = item_category
        repo_item.last_used = datetime.utcnow()
    return repo_item


@main_bp.route('/shopping', methods=['GET', 'POST'])
def shopping():
    default_list = _ensure_default_shopping_list()

    if request.method == 'POST':
        item = sanitize_text(request.form.get('item', ''))
        creator = sanitize_text(request.form.get('creator', '') or session.get('current_member') or '')
        list_id = request.form.get('list_id')
        tags_list = _json_tags(request.form.get('tags', '').strip())

        price = _to_float(request.form.get('price'), None)
        quantity = _to_float(request.form.get('quantity'), 1.0) or 1.0
        store = sanitize_text(request.form.get('store', '').strip()) or None

        selected_list = None
        if list_id:
            selected_list = ShoppingList.query.get(int(list_id))
        if not selected_list:
            selected_list = default_list

        if item:
            shopping_item = ShoppingItem(
                item=item,
                creator=creator,
                tags=json.dumps(tags_list),
                price=price,
                quantity=quantity,
                list_id=selected_list.id,
            )
            db.session.add(shopping_item)
            db.session.add(GroceryHistory(item=item, creator=creator))

            if price is not None:
                common = CommonItem.query.filter_by(name=item.lower()).first()
                if common:
                    common.default_price = price
                    common.last_used = datetime.utcnow()
                else:
                    db.session.add(CommonItem(name=item.lower(), default_price=price, last_used=datetime.utcnow()))

                repo_item = _upsert_repo_item(item, store, None, price, creator)
                if repo_item:
                    db.session.flush()
                    shopping_item.repo_item_id = repo_item.id

            db.session.commit()

            log_activity(
                actor=creator,
                action='shopping.add',
                summary=f"added {item} to shopping list {selected_list.name}",
                entity_type='shopping_item',
                entity_id=shopping_item.id,
                visibility='family',
            )

            targets = [m.name for m in FamilyMember.query.order_by(FamilyMember.name.asc()).all() if m.name and m.name != creator]
            publish_notification(
                event_type='shopping_add',
                title='Shopping list updated',
                message=f"{creator} added {item} to {selected_list.name}.",
                target_members=targets,
                created_by=creator,
                payload={'shopping_item_id': shopping_item.id, 'item': item, 'list_id': selected_list.id},
                channels=['in_app'],
            )

        return redirect(url_for('main.shopping', list_id=selected_list.id))

    selected_list_id = request.args.get('list_id', type=int)
    shopping_lists = ShoppingList.query.order_by(ShoppingList.created_at.asc(), ShoppingList.id.asc()).all()
    selected_list = next((lst for lst in shopping_lists if lst.id == selected_list_id), None) if selected_list_id else None
    if not selected_list:
        selected_list = default_list

    items = ShoppingItem.query.filter_by(list_id=selected_list.id).order_by(ShoppingItem.checked.asc(), ShoppingItem.timestamp.desc()).all()

    filter_tags = request.args.get('tags')
    if filter_tags:
        try:
            selected = json.loads(filter_tags)
            if isinstance(selected, list) and selected:
                def match(item_tags):
                    try:
                        arr = json.loads(item_tags or '[]')
                    except Exception:
                        arr = []
                    return any(t in arr for t in selected)
                items = [i for i in items if match(i.tags)]
        except Exception:
            pass

    cutoff = datetime.utcnow() - timedelta(days=90)
    existing = {i.item.lower() for i in items}
    rows = db.session.execute(db.text("""
        SELECT item, COUNT(*) as cnt
        FROM grocery_history
        WHERE timestamp >= :cutoff
        GROUP BY item
        ORDER BY cnt DESC
        LIMIT 20
    """), {'cutoff': cutoff}).fetchall()
    suggestions = [r[0] for r in rows if r[0].lower() not in existing][:10]

    repo_items = ShoppingRepoItem.query.order_by(ShoppingRepoItem.last_used.desc().nullslast(), ShoppingRepoItem.name.asc()).limit(100).all()

    config = current_app.config['HOMEHUB_CONFIG']

    groceries_budget = 0
    if any(not i.checked for i in items):
        today = datetime.utcnow()
        month_start = datetime(today.year, today.month, 1)
        groceries_expenses = db.session.query(db.func.sum(ExpenseEntry.amount)).filter(
            ExpenseEntry.category == 'Groceries',
            ExpenseEntry.date >= month_start
        ).scalar() or 0
        groceries_budget = float(groceries_expenses)

    return render_template(
        'shopping.html',
        items=items,
        shopping_lists=shopping_lists,
        selected_list_id=selected_list.id,
        suggestions=suggestions,
        repo_items=repo_items,
        config=config,
        groceries_budget=groceries_budget,
    )


@main_bp.route('/shopping/check/<int:item_id>', methods=['POST'])
def check_shopping(item_id):
    item = ShoppingItem.query.get_or_404(item_id)
    item.checked = not item.checked
    db.session.commit()

    actor = sanitize_text(session.get('current_member') or '') or sanitize_text(item.creator or '') or 'Family'
    log_activity(
        actor=actor,
        action='shopping.check' if item.checked else 'shopping.uncheck',
        summary=f"{'marked' if item.checked else 'unmarked'} {item.item} as purchased",
        entity_type='shopping_item',
        entity_id=item.id,
        visibility='family',
    )
    return redirect(url_for('main.shopping', list_id=item.list_id))


@main_bp.route('/shopping/delete/<int:item_id>', methods=['POST'])
def delete_shopping(item_id):
    item = ShoppingItem.query.get_or_404(item_id)
    requested_user = sanitize_text(request.form.get('user', '').strip())
    current_user = sanitize_text(session.get('current_member') or '').strip()
    user = requested_user or current_user or sanitize_text(item.creator or '')

    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}

    if user in admin_aliases or user == item.creator:
        item_name = item.item
        list_id = item.list_id
        db.session.delete(item)
        db.session.commit()

        log_activity(
            actor=user,
            action='shopping.delete',
            summary=f"removed {item_name} from the shopping list",
            entity_type='shopping_item',
            entity_id=item_id,
            visibility='family',
        )
        return redirect(url_for('main.shopping', list_id=list_id))

    return redirect(url_for('main.shopping', list_id=item.list_id))


@main_bp.route('/api/shopping/lists', methods=['GET'])
def api_get_shopping_lists():
    default_list = _ensure_default_shopping_list()
    lists = ShoppingList.query.order_by(ShoppingList.created_at.asc(), ShoppingList.id.asc()).all()
    payload = []
    for lst in lists:
        active_count = ShoppingItem.query.filter_by(list_id=lst.id, checked=False).count()
        total = db.session.query(db.func.sum((db.func.coalesce(ShoppingItem.price, 0) * db.func.coalesce(ShoppingItem.quantity, 1)))).filter_by(list_id=lst.id, checked=False).scalar() or 0
        payload.append({
            'id': lst.id,
            'name': lst.name,
            'color': lst.color or '#3B82F6',
            'list_type': lst.list_type or 'standard',
            'creator': lst.creator,
            'active_count': active_count,
            'estimated_total': float(total),
            'is_default': lst.id == default_list.id,
        })
    return jsonify({'ok': True, 'lists': payload})


@main_bp.route('/api/shopping/lists', methods=['POST'])
def api_create_shopping_list():
    try:
        data = request.get_json(force=True) or {}
        name = sanitize_text(str(data.get('name', '')).strip())
        color = sanitize_text(str(data.get('color', '#3B82F6')).strip()) or '#3B82F6'
        creator = sanitize_text(str(data.get('creator') or session.get('current_member') or '').strip())

        if not name:
            return jsonify({'ok': False, 'error': 'List name is required'}), 400

        list_type = _normalize_list_type(data.get('list_type'))
        row = ShoppingList(name=name, color=color, list_type=list_type, creator=creator)
        db.session.add(row)
        db.session.commit()
        return jsonify({'ok': True, 'list': {'id': row.id, 'name': row.name, 'color': row.color, 'list_type': row.list_type or 'standard', 'creator': row.creator}})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@main_bp.route('/api/shopping/lists/<int:list_id>', methods=['PUT'])
def api_update_shopping_list(list_id):
    row = ShoppingList.query.get_or_404(list_id)
    try:
        data = request.get_json(force=True) or {}
        if 'name' in data:
            name = sanitize_text(str(data.get('name', '')).strip())
            if not name:
                return jsonify({'ok': False, 'error': 'List name is required'}), 400
            row.name = name
        if 'color' in data:
            color = sanitize_text(str(data.get('color', '#3B82F6')).strip()) or '#3B82F6'
            row.color = color
        if 'list_type' in data:
            row.list_type = _normalize_list_type(data.get('list_type'))
        db.session.commit()
        return jsonify({'ok': True, 'list': {'id': row.id, 'name': row.name, 'color': row.color, 'list_type': row.list_type or 'standard', 'creator': row.creator}})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@main_bp.route('/api/shopping/lists/<int:list_id>', methods=['DELETE'])
def api_delete_shopping_list(list_id):
    row = ShoppingList.query.get_or_404(list_id)
    try:
        data = request.get_json(silent=True) or {}
        strategy = sanitize_text(str(data.get('strategy', 'move')).strip().lower())
        target_list_id = data.get('target_list_id')

        all_lists = ShoppingList.query.order_by(ShoppingList.created_at.asc(), ShoppingList.id.asc()).all()
        if len(all_lists) <= 1:
            replacement = ShoppingList(name='General', color='#3B82F6', list_type='standard', creator=row.creator or 'System')
            db.session.add(replacement)
            db.session.flush()
            all_lists = [row, replacement]

        if strategy not in {'move', 'delete_all'}:
            strategy = 'move'

        moved_to_list_id = None
        removed_items = 0

        if strategy == 'delete_all':
            removed_items = ShoppingItem.query.filter_by(list_id=row.id).delete(synchronize_session=False)
        else:
            target_list = None
            if target_list_id is not None:
                try:
                    target_list = ShoppingList.query.get(int(target_list_id))
                except Exception:
                    target_list = None

            if not target_list or target_list.id == row.id:
                target_list = next((lst for lst in all_lists if lst.id != row.id), None)

            if not target_list:
                return jsonify({'ok': False, 'error': 'No destination list available'}), 400

            ShoppingItem.query.filter_by(list_id=row.id).update({'list_id': target_list.id}, synchronize_session=False)
            moved_to_list_id = target_list.id

        db.session.delete(row)
        db.session.commit()

        return jsonify({
            'ok': True,
            'strategy': strategy,
            'moved_to_list_id': moved_to_list_id,
            'removed_items': removed_items,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@main_bp.route('/api/shopping/items', methods=['GET'])
def api_get_shopping_items():
    default_list = _ensure_default_shopping_list()
    list_id = request.args.get('list_id', type=int) or default_list.id
    tags = request.args.get('tags')

    items = ShoppingItem.query.filter_by(list_id=list_id).order_by(ShoppingItem.checked.asc(), ShoppingItem.timestamp.desc()).all()
    if tags:
        try:
            selected = json.loads(tags)
            if isinstance(selected, list) and selected:
                def match(item_tags):
                    try:
                        arr = json.loads(item_tags or '[]')
                    except Exception:
                        arr = []
                    return any(t in arr for t in selected)
                items = [i for i in items if match(i.tags)]
        except Exception:
            pass

    return jsonify({'ok': True, 'items': [_serialize_item(i) for i in items], 'list_id': list_id})


@main_bp.route('/api/shopping/items', methods=['POST'])
def api_create_shopping_item():
    try:
        default_list = _ensure_default_shopping_list()
        data = request.get_json(force=True) or {}

        item_name = sanitize_text(str(data.get('item', '')).strip())
        if not item_name:
            return jsonify({'ok': False, 'error': 'Item name is required'}), 400

        creator = sanitize_text(str(data.get('creator') or session.get('current_member') or '').strip())
        list_id = int(data.get('list_id') or default_list.id)
        list_row = ShoppingList.query.get(list_id) or default_list

        tags = data.get('tags', [])
        if not isinstance(tags, list):
            tags = []
        clean_tags = [sanitize_text(t) for t in tags if isinstance(t, str) and t.strip()]

        price = _to_float(data.get('price'), None)
        quantity = _to_float(data.get('quantity'), 1.0) or 1.0
        checked = bool(data.get('checked', False))
        purchase_photo_filename = sanitize_text(str(data.get('purchase_photo_filename') or '').strip()) or None
        size_requirements = sanitize_text(str(data.get('size_requirements') or '').strip()) or None
        purchase_category = sanitize_text(str(data.get('purchase_category') or '').strip()) or None
        planning_notes = sanitize_text(str(data.get('planning_notes') or '').strip()) or None
        target_price = _to_float(data.get('target_price'), None)
        budget_ceiling = _to_float(data.get('budget_ceiling'), None)

        item = ShoppingItem(
            item=item_name,
            creator=creator,
            list_id=list_row.id,
            tags=json.dumps(clean_tags),
            price=price,
            quantity=quantity,
            checked=checked,
            purchase_photo_filename=purchase_photo_filename,
            size_requirements=size_requirements,
            target_price=target_price,
            budget_ceiling=budget_ceiling,
            planning_notes=planning_notes,
            purchase_category=purchase_category,
        )
        db.session.add(item)
        db.session.add(GroceryHistory(item=item_name, creator=creator))

        repo_item_id = data.get('repo_item_id')
        if repo_item_id:
            repo_item = ShoppingRepoItem.query.get(int(repo_item_id))
            if repo_item:
                item.repo_item_id = repo_item.id
                repo_item.last_used = datetime.utcnow()
                if item.price is None and repo_item.default_price is not None:
                    item.price = repo_item.default_price
        elif price is not None:
            store = sanitize_text(str(data.get('store', '')).strip()) or None
            category = sanitize_text(str(data.get('category', '')).strip()) or None
            repo_item = _upsert_repo_item(item_name, store, category, price, creator)
            if repo_item:
                db.session.flush()
                item.repo_item_id = repo_item.id

        if price is not None:
            common = CommonItem.query.filter_by(name=item_name.lower()).first()
            if common:
                common.default_price = price
                common.last_used = datetime.utcnow()
            else:
                db.session.add(CommonItem(name=item_name.lower(), default_price=price, last_used=datetime.utcnow()))

        db.session.commit()
        return jsonify({'ok': True, 'item': _serialize_item(item)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@main_bp.route('/api/shopping/items/<int:item_id>', methods=['DELETE'])
def api_delete_shopping_item(item_id):
    item = ShoppingItem.query.get_or_404(item_id)
    user = sanitize_text(session.get('current_member') or request.args.get('user') or request.form.get('user') or '').strip()
    user = user or sanitize_text(item.creator or '').strip()

    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if user not in admin_aliases and user != item.creator:
        return jsonify({'ok': False, 'error': 'Permission denied'}), 403

    db.session.delete(item)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/shopping/<int:item_id>/tags', methods=['POST'])
def update_shopping_tags(item_id):
    item = ShoppingItem.query.get_or_404(item_id)
    try:
        data = request.get_json(force=True) or {}
        tags = data.get('tags', [])
        if not isinstance(tags, list):
            tags = []
        cleaned = []
        for t in tags:
            if isinstance(t, str):
                cleaned.append(sanitize_text(t))
        item.tags = json.dumps(cleaned)
        db.session.commit()
        return jsonify({'ok': True, 'id': item.id, 'tags': cleaned})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@main_bp.route('/api/shopping', methods=['GET'])
def api_get_shopping():
    tags = request.args.get('tags')
    list_id = request.args.get('list_id', type=int)

    q = ShoppingItem.query
    if list_id:
        q = q.filter_by(list_id=list_id)

    items = q.order_by(ShoppingItem.checked.asc(), ShoppingItem.timestamp.desc()).all()
    if tags:
        try:
            sel = json.loads(tags)
            if isinstance(sel, list) and sel:
                def match(item_tags):
                    try:
                        arr = json.loads(item_tags or '[]')
                    except Exception:
                        arr = []
                    return any(t in arr for t in sel)
                items = [i for i in items if match(i.tags)]
        except Exception:
            pass
    return jsonify([_serialize_item(i) for i in items])


@main_bp.route('/api/shopping/<int:item_id>', methods=['PUT'])
def api_update_shopping(item_id):
    item = ShoppingItem.query.get_or_404(item_id)
    try:
        data = request.get_json(force=True) or {}

        if 'item' in data and isinstance(data.get('item'), str):
            name = sanitize_text(data.get('item') or '').strip()
            if not name:
                return jsonify({'ok': False, 'error': 'Item name is required'}), 400
            item.item = name

        if 'tags' in data:
            raw_tags = data.get('tags', [])
            tags = []
            if isinstance(raw_tags, list):
                for t in raw_tags:
                    if isinstance(t, str):
                        tags.append(sanitize_text(t))
            item.tags = json.dumps(tags)

        if 'price' in data:
            item.price = _to_float(data.get('price'), None)

        if 'quantity' in data:
            quantity = _to_float(data.get('quantity'), 1.0)
            item.quantity = quantity if quantity and quantity > 0 else 1.0

        if 'purchase_photo_filename' in data:
            item.purchase_photo_filename = sanitize_text(str(data.get('purchase_photo_filename') or '').strip()) or None

        if 'size_requirements' in data:
            item.size_requirements = sanitize_text(str(data.get('size_requirements') or '').strip()) or None

        if 'purchase_category' in data:
            item.purchase_category = sanitize_text(str(data.get('purchase_category') or '').strip()) or None

        if 'planning_notes' in data:
            item.planning_notes = sanitize_text(str(data.get('planning_notes') or '').strip()) or None

        if 'target_price' in data:
            item.target_price = _to_float(data.get('target_price'), None)

        if 'budget_ceiling' in data:
            item.budget_ceiling = _to_float(data.get('budget_ceiling'), None)

        if 'checked' in data:
            item.checked = bool(data.get('checked'))

        if 'list_id' in data:
            new_list_id = int(data.get('list_id') or 0)
            list_row = ShoppingList.query.get(new_list_id)
            if list_row:
                item.list_id = list_row.id

        db.session.commit()
        return jsonify({'ok': True, 'item': _serialize_item(item)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@main_bp.route('/api/shopping/history', methods=['DELETE'])
def api_delete_shopping_history_item():
    try:
        data = request.get_json(force=True) or {}
        it = sanitize_text(str(data.get('item', '')).strip())
        if not it:
            return jsonify({'ok': False, 'error': 'missing item'}), 400
        row = db.session.execute(db.text("""
            SELECT id FROM grocery_history WHERE item = :item ORDER BY timestamp DESC LIMIT 1
        """), {'item': it}).fetchone()
        if row:
            db.session.execute(db.text('DELETE FROM grocery_history WHERE id = :id'), {'id': row[0]})
            db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@main_bp.route('/api/shopping/repo', methods=['GET'])
def api_get_shopping_repo():
    query = sanitize_text(str(request.args.get('q', '')).strip())
    store = sanitize_text(str(request.args.get('store', '')).strip())

    q = ShoppingRepoItem.query
    if query:
        q = q.filter(ShoppingRepoItem.name.ilike(f'%{query}%'))
    if store:
        q = q.filter(ShoppingRepoItem.store.ilike(f'%{store}%'))

    items = q.order_by(ShoppingRepoItem.last_used.desc().nullslast(), ShoppingRepoItem.name.asc()).limit(100).all()
    return jsonify({'ok': True, 'items': [_serialize_repo_item(i) for i in items]})


@main_bp.route('/api/shopping/repo', methods=['POST'])
def api_create_repo_item():
    try:
        data = request.get_json(force=True) or {}
        name = sanitize_text(str(data.get('name', '')).strip())
        store = sanitize_text(str(data.get('store', '')).strip()) or None
        category = sanitize_text(str(data.get('category', '')).strip()) or None
        price = _to_float(data.get('price'), None)
        creator = sanitize_text(str(data.get('created_by') or session.get('current_member') or '').strip())

        if not name:
            return jsonify({'ok': False, 'error': 'Name is required'}), 400

        row = _upsert_repo_item(name, store, category, price, creator)
        db.session.commit()
        return jsonify({'ok': True, 'item': _serialize_repo_item(row)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@main_bp.route('/api/shopping/repo/<int:repo_id>', methods=['PUT'])
def api_update_repo_item(repo_id):
    row = ShoppingRepoItem.query.get_or_404(repo_id)
    try:
        data = request.get_json(force=True) or {}
        if 'name' in data:
            name = sanitize_text(str(data.get('name', '')).strip())
            if not name:
                return jsonify({'ok': False, 'error': 'Name is required'}), 400
            row.name = name
        if 'store' in data:
            row.store = sanitize_text(str(data.get('store', '')).strip()) or None
        if 'category' in data:
            row.category = sanitize_text(str(data.get('category', '')).strip()) or None
        if 'price' in data:
            row.default_price = _to_float(data.get('price'), None)
        row.last_used = datetime.utcnow()
        db.session.commit()
        return jsonify({'ok': True, 'item': _serialize_repo_item(row)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@main_bp.route('/api/shopping/repo/<int:repo_id>', methods=['DELETE'])
def api_delete_repo_item(repo_id):
    row = ShoppingRepoItem.query.get_or_404(repo_id)
    ShoppingItem.query.filter_by(repo_item_id=row.id).update({'repo_item_id': None}, synchronize_session=False)
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True})


@main_bp.route('/api/common-items', methods=['GET'])
def api_get_common_items():
    repo_items = ShoppingRepoItem.query.order_by(ShoppingRepoItem.last_used.desc().nullslast(), ShoppingRepoItem.name.asc()).all()
    result = [{'id': i.id, 'name': i.name, 'price': i.default_price, 'category': i.category, 'store': i.store} for i in repo_items]
    return jsonify(result)


@main_bp.route('/api/common-items/search', methods=['GET'])
def api_search_common_items():
    q = request.args.get('q', '').strip().lower()
    if not q or len(q) < 1:
        return jsonify([])

    repo_items = ShoppingRepoItem.query.filter(ShoppingRepoItem.name.ilike(f'%{q}%')).order_by(ShoppingRepoItem.last_used.desc().nullslast()).limit(20).all()
    result = [{'id': i.id, 'name': i.name, 'price': i.default_price, 'category': i.category, 'store': i.store} for i in repo_items]

    if not result:
        fallback = CommonItem.query.filter(CommonItem.name.ilike(f'%{q}%')).order_by(CommonItem.last_used.desc().nullslast()).limit(10).all()
        result = [{'id': i.id, 'name': i.name, 'price': i.default_price, 'category': i.category, 'store': None} for i in fallback]

    return jsonify(result)


@main_bp.route('/api/shopping/purchase-photo', methods=['POST'])
def api_upload_shopping_purchase_photo():
    if 'photo' not in request.files:
        return jsonify({'ok': False, 'error': 'Photo is required'}), 400

    photo = request.files['photo']
    if not photo or not photo.filename:
        return jsonify({'ok': False, 'error': 'Photo is required'}), 400

    filename = secure_filename(photo.filename)
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in _SHOPPING_PHOTO_EXTS:
        return jsonify({'ok': False, 'error': 'Unsupported image format'}), 400

    request_max = request.content_length or 0
    if request_max > 8 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'Image too large (max 8MB)'}), 400

    stored = f"purchase-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}.{ext}"
    photo.save(os.path.join(_shopping_purchase_upload_dir(), stored))
    return jsonify({'ok': True, 'filename': stored, 'url': _purchase_photo_url(stored)})


@main_bp.route('/uploads/shopping-purchases/<path:filename>')
def uploaded_shopping_purchase_photo(filename: str):
    return send_from_directory(_shopping_purchase_upload_dir(), filename, as_attachment=False)
