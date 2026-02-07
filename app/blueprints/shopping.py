from flask import render_template, request, redirect, url_for, current_app, jsonify
from datetime import datetime, timedelta
from ..models import db, ShoppingItem, GroceryHistory, CommonItem, ExpenseEntry
from ..blueprints import main_bp
from ..security import sanitize_text
import json


@main_bp.route('/shopping', methods=['GET', 'POST'])
def shopping():
    if request.method == 'POST':
        item = sanitize_text(request.form['item'])
        creator = sanitize_text(request.form['creator'])
        raw_tags = request.form.get('tags', '').strip()
        tags_list = []
        if raw_tags:
            try:
                # Expect JSON array from enhanced UI
                tags_list = json.loads(raw_tags)
                if not isinstance(tags_list, list):
                    tags_list = []
            except Exception:
                # Fallback: comma separated
                tags_list = [t.strip() for t in raw_tags.split(',') if t.strip()]
        # sanitize each tag
        tags_list = [sanitize_text(t) for t in tags_list if isinstance(t, str) and t.strip()]
        
        # Parse price and quantity
        try:
            price = float(request.form.get('price', 0)) or None
        except (ValueError, TypeError):
            price = None
        try:
            quantity = float(request.form.get('quantity', 1)) or 1.0
        except (ValueError, TypeError):
            quantity = 1.0
        
        shopping_item = ShoppingItem(item=item, creator=creator, tags=json.dumps(tags_list), price=price, quantity=quantity)
        db.session.add(shopping_item)
        db.session.add(GroceryHistory(item=item, creator=creator))
        
        # Update or create CommonItem if price is provided
        if price:
            common = CommonItem.query.filter_by(name=item.lower()).first()
            if common:
                common.default_price = price
                common.last_used = datetime.utcnow()
            else:
                common = CommonItem(name=item.lower(), default_price=price, last_used=datetime.utcnow())
                db.session.add(common)
        
        db.session.commit()
        return redirect(url_for('main.shopping'))
    # Filtering by tags (if provided)
    filter_tags = request.args.get('tags')
    q = ShoppingItem.query
    items = q.order_by(ShoppingItem.checked.asc(), ShoppingItem.timestamp.desc()).all()
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
    """), {"cutoff": cutoff}).fetchall()
    suggestions = [r[0] for r in rows if r[0].lower() not in existing][:10]
    config = current_app.config['HOMEHUB_CONFIG']
    
    # Get groceries category spending for this month (if there are active items)
    active_items = [i for i in items if not i.checked]
    groceries_budget = 0
    if active_items:
        today = datetime.utcnow()
        month_start = datetime(today.year, today.month, 1)
        groceries_expenses = db.session.query(db.func.sum(ExpenseEntry.amount)).filter(
            ExpenseEntry.category == 'Groceries',
            ExpenseEntry.date >= month_start
        ).scalar() or 0
        groceries_budget = float(groceries_expenses)
    
    return render_template('shopping.html', items=items, suggestions=suggestions, config=config, groceries_budget=groceries_budget)


@main_bp.route('/shopping/check/<int:item_id>', methods=['POST'])
def check_shopping(item_id):
    item = ShoppingItem.query.get_or_404(item_id)
    item.checked = not item.checked
    db.session.commit()
    return redirect(url_for('main.shopping'))


@main_bp.route('/shopping/delete/<int:item_id>', methods=['POST'])
def delete_shopping(item_id):
    item = ShoppingItem.query.get_or_404(item_id)
    user = sanitize_text(request.form['user'])
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if user in admin_aliases or user == item.creator:
        db.session.delete(item)
        db.session.commit()
    return redirect(url_for('main.shopping'))


@main_bp.route('/api/shopping/<int:item_id>/tags', methods=['POST'])
def update_shopping_tags(item_id):
    item = ShoppingItem.query.get_or_404(item_id)
    try:
        data = request.get_json(force=True) or {}
        tags = data.get('tags', [])
        if not isinstance(tags, list):
            tags = []
        # sanitize each tag (keep simple strings)
        cleaned = []
        for t in tags:
            if isinstance(t, str):
                cleaned.append(sanitize_text(t))
        item.tags = json.dumps(cleaned)
        db.session.commit()
        return jsonify({"ok": True, "id": item.id, "tags": cleaned})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/shopping', methods=['GET'])
def api_get_shopping():
    tags = request.args.get('tags')
    items = ShoppingItem.query.order_by(ShoppingItem.checked.asc(), ShoppingItem.timestamp.desc()).all()
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
    def to_dict(i):
        try:
            tags = json.loads(i.tags or '[]')
        except Exception:
            tags = []
        return {"id": i.id, "item": i.item, "checked": i.checked, "creator": i.creator, "timestamp": i.timestamp.isoformat(), "tags": tags}
    return jsonify([to_dict(i) for i in items])


@main_bp.route('/api/shopping/<int:item_id>', methods=['PUT'])
def api_update_shopping(item_id):
    item = ShoppingItem.query.get_or_404(item_id)
    try:
        data = request.get_json(force=True) or {}
        new_item = data.get('item')
        raw_tags = data.get('tags', [])
        if isinstance(new_item, str):
            item.item = sanitize_text(new_item)
        tags = []
        if isinstance(raw_tags, list):
            for t in raw_tags:
                if isinstance(t, str):
                    tags.append(sanitize_text(t))
        item.tags = json.dumps(tags)
        db.session.commit()
        return jsonify({"ok": True, "item": {"id": item.id, "item": item.item, "tags": tags}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/shopping/history', methods=['DELETE'])
def api_delete_shopping_history_item():
    try:
        data = request.get_json(force=True) or {}
        it = sanitize_text(str(data.get('item', '')).strip())
        if not it:
            return jsonify({"ok": False, "error": "missing item"}), 400
        # Delete one most recent matching history row (best effort)
        row = db.session.execute(db.text("""
            SELECT id FROM grocery_history WHERE item = :item ORDER BY timestamp DESC LIMIT 1
        """), {"item": it}).fetchone()
        if row:
            db.session.execute(db.text("DELETE FROM grocery_history WHERE id = :id"), {"id": row[0]})
            db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@main_bp.route('/api/common-items', methods=['GET'])
def api_get_common_items():
    """Get all common items for price autocomplete"""
    items = CommonItem.query.order_by(CommonItem.last_used.desc().nullslast()).all()
    result = [{"id": i.id, "name": i.name, "price": i.default_price, "category": i.category} for i in items]
    return jsonify(result)


@main_bp.route('/api/common-items/search', methods=['GET'])
def api_search_common_items():
    """Search common items by name"""
    q = request.args.get('q', '').strip().lower()
    if not q or len(q) < 2:
        return jsonify([])
    items = CommonItem.query.filter(CommonItem.name.ilike(f'%{q}%')).order_by(CommonItem.last_used.desc().nullslast()).limit(10).all()
    result = [{"id": i.id, "name": i.name, "price": i.default_price, "category": i.category} for i in items]
    return jsonify(result)
