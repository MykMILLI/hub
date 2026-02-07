from flask import render_template, request, redirect, url_for, flash, jsonify, current_app, session
from datetime import datetime, date, timedelta
import calendar as _calendar
import json
from ..models import (
    db, RecurringExpense, ExpenseEntry, IncomeEntry, RecurringBillPayment,
    FamilyBudget, BudgetCategory, SharedExpense, DebtAccount, DebtPayment,
    FinancialGoal, GoalContribution, PlaidAccount, PlaidTransaction,
)
from ..security import sanitize_text
from ..blueprints import main_bp
from ..plaid_service import plaid_service
import bleach
import logging

logger = logging.getLogger(__name__)


def _generate_recurring_entries_until(today: date | None = None) -> None:
    today = today or date.today()
    recs = RecurringExpense.query.all()
    for r in recs:
        start = r.start_date or today
        # Generate from rule start date; don't clamp to effective_from so rule owns entire range
        base_day = (r.start_date or today).day
        last = r.last_generated_date

        def next_date(d: date) -> date:
            if r.frequency == 'daily':
                return d + timedelta(days=1)
            if r.frequency == 'weekly':
                return d + timedelta(weeks=1)
            mode = getattr(r, 'monthly_mode', 'day_of_month') or 'day_of_month'
            if mode == 'calendar':
                ny = d.year + (1 if d.month == 12 else 0)
                nm = 1 if d.month == 12 else d.month + 1
                return date(ny, nm, 1)
            else:
                ny = d.year + (1 if d.month == 12 else 0)
                nm = 1 if d.month == 12 else d.month + 1
                last_dom = _calendar.monthrange(ny, nm)[1]
                day = min(base_day, last_dom)
                return date(ny, nm, day)

        if last is None or (last and last < start):
            if r.frequency in ('daily', 'weekly'):
                d = start
            else:
                mode = getattr(r, 'monthly_mode', 'day_of_month') or 'day_of_month'
                if mode == 'calendar':
                    if start.day == 1:
                        d = start
                    else:
                        ny = start.year + (1 if start.month == 12 else 0)
                        nm = 1 if start.month == 12 else start.month + 1
                        d = date(ny, nm, 1)
                else:
                    d = start
        else:
            d = next_date(last)

        while d <= today and (not r.end_date or d <= r.end_date):
            exists = ExpenseEntry.query.filter_by(date=d, recurring_id=r.id).first()
            if not exists:
                qty = r.default_quantity or 1.0
                amt = (r.unit_price or 0.0) * qty
                db.session.add(ExpenseEntry(
                    date=d,
                    title=r.title,
                    category=getattr(r, 'category', None),
                    unit_price=r.unit_price,
                    quantity=qty,
                    amount=amt,
                    payer=r.creator,
                    recurring_id=r.id
                ))
            r.last_generated_date = d
            d = next_date(d)
    db.session.commit()


def _load_expense_settings() -> dict:
    settings = {'currency': '\u20b9', 'categories': []}
    try:
        rows = db.session.execute(db.text("SELECT key, value FROM app_setting WHERE key IN ('currency','categories')"))
        data = {k: v for k, v in rows}
        if data.get('currency'):
            settings['currency'] = data['currency']
        if data.get('categories'):
            settings['categories'] = [c.strip() for c in data['categories'].split(',') if c.strip()]
    except Exception:
        # Best-effort: fall back to default settings but log for diagnostics
        current_app.logger.exception('Failed to load expense settings; using defaults')
    return settings


def _build_month_payload(y: int, m: int) -> dict:
    month_start = date(y, m, 1)
    last_day = _calendar.monthrange(y, m)[1]
    month_end = date(y, m, last_day)

    q_entries = (
        ExpenseEntry.query
        .filter(ExpenseEntry.date >= month_start, ExpenseEntry.date <= month_end)
        .order_by(ExpenseEntry.date.asc(), ExpenseEntry.timestamp.asc())
        .all()
    )
    by_date: dict[str, dict] = {}
    total = 0.0
    per_payer: dict[str, float] = {}
    per_category: dict[str, float] = {}
    for e in q_entries:
        ds = e.date.strftime('%Y-%m-%d')
        by_date.setdefault(ds, {'total': 0.0, 'entries': []})
        by_date[ds]['total'] += float(e.amount or 0)
        total += float(e.amount or 0)
        per_payer[e.payer or ''] = per_payer.get(e.payer or '', 0.0) + float(e.amount or 0)
        if e.category:
            per_category[e.category] = per_category.get(e.category, 0.0) + float(e.amount or 0)
        by_date[ds]['entries'].append({
            'id': e.id,
            'title': e.title,
            'category': e.category,
            'unit_price': float(e.unit_price) if e.unit_price is not None else None,
            'amount': float(e.amount or 0),
            'quantity': float(e.quantity or 0) if e.quantity is not None else None,
            'recurring': bool(e.recurring_id),
            'payer': e.payer or ''
        })

    top_category = None
    if per_category:
        top_category = max(per_category.items(), key=lambda kv: kv[1])[0]

    settings = _load_expense_settings()
    payload = {
        'by_date': by_date,
        'summary': {
            'total_this_month': total,
            'per_payer': per_payer,
            'per_category': per_category,
            'top_category': top_category,
        },
        'year': y,
        'month': m,
        'settings': settings,
    }
    return payload


@main_bp.route('/expenses', methods=['GET', 'POST'])
def expenses():
    today = date.today()
    # Ensure recurring entries are generated up to today for a consistent view
    _generate_recurring_entries_until(today)

    if request.method == 'POST':
        form_type = request.form.get('form_type')
        if form_type == 'recurring':
            title = bleach.clean(request.form.get('title',''))
            unit_price = float(request.form.get('unit_price') or 0)
            default_quantity = float(request.form.get('default_quantity') or 1)
            frequency = bleach.clean(request.form.get('frequency','daily'))
            monthly_mode = bleach.clean(request.form.get('monthly_mode','day_of_month'))
            category = bleach.clean(request.form.get('category',''))
            start_date = request.form.get('start_date')
            end_date = request.form.get('end_date')
            creator = bleach.clean(request.form.get('creator',''))
            sd = datetime.strptime(start_date, '%Y-%m-%d').date() if start_date else date.today()
            ed = datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else None
            db.session.add(RecurringExpense(title=title, unit_price=unit_price, default_quantity=default_quantity, frequency=frequency, monthly_mode=monthly_mode, category=category, start_date=sd, end_date=ed, creator=creator, effective_from=sd))
            db.session.commit()
            flash('Recurring expense added.', 'success')
            y = request.args.get('y') or today.year
            m = request.args.get('m') or today.month
            sel = request.args.get('sel')
            return redirect(url_for('main.expenses', y=y, m=m, sel=sel))
        else:
            title = bleach.clean(request.form.get('title',''))
            amount = float(request.form.get('amount') or 0)
            category = bleach.clean(request.form.get('category') or '')
            payer = bleach.clean(request.form.get('payer') or '')
            date_s = request.form.get('date')
            d = datetime.strptime(date_s, '%Y-%m-%d').date() if date_s else date.today()
            unit_price = request.form.get('unit_price'); quantity = request.form.get('quantity')
            up = float(unit_price) if unit_price else None
            q = float(quantity) if quantity else None
            db.session.add(ExpenseEntry(date=d, title=title, category=category, unit_price=up, quantity=q, amount=amount, payer=payer))
            db.session.commit()
            flash('Expense added.', 'success')
            y = request.args.get('y') or d.year
            m = request.args.get('m') or d.month
            sel = request.args.get('sel') or d.strftime('%Y-%m-%d')
            return redirect(url_for('main.expenses', y=y, m=m, sel=sel))

    try:
        y = int(request.args.get('y') or today.year)
        m = int(request.args.get('m') or today.month)
    except Exception:
        y, m = today.year, today.month

    payload = _build_month_payload(y, m)
    rules = RecurringExpense.query.order_by(RecurringExpense.timestamp.desc()).all()
    
    # Get all income entries for the current month
    month_start = date(y, m, 1)
    last_day = _calendar.monthrange(y, m)[1]
    month_end = date(y, m, last_day)
    income_entries = IncomeEntry.query.filter(
        IncomeEntry.date >= month_start,
        IncomeEntry.date <= month_end
    ).order_by(IncomeEntry.date.desc()).all()
    
    config = current_app.config['HOMEHUB_CONFIG']
    return render_template('expenses.html', rules=rules, config=config, expenses_json=json.dumps(payload), income_entries=income_entries)


@main_bp.route('/expenses/recurring/edit/<int:rid>', methods=['POST'])
def edit_recurring_expense(rid):
    r = RecurringExpense.query.get_or_404(rid)
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if not (user in admin_aliases or user == (r.creator or '')):
        flash('Not allowed to edit rule.', 'error')
        return redirect(url_for('main.expenses'))
    # Update fields
    r.title = bleach.clean(request.form.get('title', r.title))
    cat_raw = request.form.get('category')
    if cat_raw is not None:
        cat_val = bleach.clean(cat_raw or '')
        r.category = cat_val or None
    up = request.form.get('unit_price'); dq = request.form.get('default_quantity')
    r.unit_price = float(up) if up not in (None, '') else r.unit_price
    r.default_quantity = float(dq) if dq not in (None, '') else r.default_quantity
    freq = bleach.clean(request.form.get('frequency', r.frequency))
    mmode = bleach.clean(request.form.get('monthly_mode', getattr(r, 'monthly_mode', 'day_of_month')))
    r.frequency = freq
    r.monthly_mode = mmode
    sd = request.form.get('start_date'); ed = request.form.get('end_date')
    r.start_date = datetime.strptime(sd, '%Y-%m-%d').date() if sd else r.start_date
    r.end_date = datetime.strptime(ed, '%Y-%m-%d').date() if ed else r.end_date
    # Do not force effective_from to today; edits are authoritative for full rule window
    today = date.today()
    db.session.commit()
    # Authoritative pruning: remove entries outside [start_date, end_date]
    try:
        if r.start_date:
            ExpenseEntry.query.filter(ExpenseEntry.recurring_id==r.id, ExpenseEntry.date < r.start_date).delete()
        if r.end_date:
            ExpenseEntry.query.filter(ExpenseEntry.recurring_id==r.id, ExpenseEntry.date > r.end_date).delete()
        db.session.commit()
    except Exception:
        db.session.rollback()
    # Update all in-range entries to reflect new rule (title/category/prices/qty)
    try:
        entries = ExpenseEntry.query.filter(ExpenseEntry.recurring_id==r.id).all()
        for e in entries:
            e.title = r.title
            e.category = getattr(r, 'category', None)
            e.unit_price = r.unit_price
            e.quantity = r.default_quantity
            e.amount = (r.unit_price or 0.0) * (r.default_quantity or 1.0)
        db.session.commit()
    except Exception:
        db.session.rollback()
    flash('Recurring rule updated. Entries pruned/updated to match.', 'success')
    # Preserve view state
    y = request.args.get('y') or today.year
    m = request.args.get('m') or today.month
    sel = request.args.get('sel')
    return redirect(url_for('main.expenses', y=y, m=m, sel=sel, open='recurring'))


@main_bp.route('/expenses/recurring/delete/<int:rid>', methods=['POST'])
def delete_recurring_expense(rid):
    r = RecurringExpense.query.get_or_404(rid)
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if not (user in admin_aliases or user == (r.creator or '')):
        flash('Not allowed to delete rule.', 'error')
        return redirect(url_for('main.expenses'))
    delete_entries = request.form.get('delete_entries') in ('1', 'true', 'on', 'yes')
    if delete_entries:
        try:
            ExpenseEntry.query.filter_by(recurring_id=r.id).delete()
        except Exception:
            pass
    db.session.delete(r)
    db.session.commit()
    flash('Recurring rule deleted.' + (' Associated entries removed.' if delete_entries else ''), 'success')
    # Preserve view
    today = date.today()
    y = request.args.get('y') or today.year
    m = request.args.get('m') or today.month
    sel = request.args.get('sel')
    return redirect(url_for('main.expenses', y=y, m=m, sel=sel, open='recurring'))


@main_bp.route('/expenses/bill-payment/toggle', methods=['POST'])
def toggle_bill_payment():
    """Toggle paid/unpaid status for a recurring bill in a specific month"""
    data = request.get_json() or {}
    bill_id = data.get('bill_id')
    year = data.get('year')
    month = data.get('month')
    user = sanitize_text(data.get('user', ''))
    
    if not all([bill_id, year, month, user]):
        return jsonify({'error': 'Missing required parameters'}), 400
    
    try:
        # Find or create the payment record
        payment = RecurringBillPayment.query.filter_by(
            recurring_expense_id=bill_id,
            year=year,
            month=month
        ).first()
        
        if not payment:
            # Create new payment record (unpaid by default)
            payment = RecurringBillPayment(
                recurring_expense_id=bill_id,
                year=year,
                month=month,
                is_paid=False
            )
            db.session.add(payment)
        
        # Toggle the paid status
        payment.is_paid = not payment.is_paid
        payment.paid_by = user if payment.is_paid else None
        payment.paid_date = datetime.utcnow() if payment.is_paid else None
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'is_paid': payment.is_paid,
            'paid_by': payment.paid_by,
            'message': f'Bill marked as {"paid" if payment.is_paid else "unpaid"}'
        }), 200
    except Exception as e:
        logger.error(f"Error toggling bill payment: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@main_bp.route('/expenses/bill-payment/status/<int:bill_id>/<int:year>/<int:month>', methods=['GET'])
def get_bill_payment_status(bill_id, year, month):
    """Get payment status for a specific bill"""
    try:
        payment = RecurringBillPayment.query.filter_by(
            recurring_expense_id=bill_id,
            year=year,
            month=month
        ).first()
        
        if not payment:
            return jsonify({'is_paid': False, 'paid_by': None}), 200
        
        return jsonify({
            'is_paid': payment.is_paid,
            'paid_by': payment.paid_by,
            'paid_date': payment.paid_date.isoformat() if payment.paid_date else None
        }), 200
    except Exception as e:
        logger.error(f"Error getting bill payment status: {e}")
        return jsonify({'error': str(e)}), 500


@main_bp.route('/expenses/income/<int:income_id>/toggle-family', methods=['POST'])
def toggle_income_family_inclusion(income_id):
    """Toggle whether income is included in family total"""
    data = request.get_json() or {}
    user = sanitize_text(data.get('user', ''))
    
    try:
        income = IncomeEntry.query.get_or_404(income_id)
        income.include_in_family_total = not income.include_in_family_total
        db.session.commit()
        
        return jsonify({
            'success': True,
            'include_in_family_total': income.include_in_family_total,
            'message': f'Income {"included" if income.include_in_family_total else "excluded"} from family total'
        }), 200
    except Exception as e:
        logger.error(f"Error toggling income inclusion: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@main_bp.route('/expenses/settings', methods=['POST'])
def expenses_settings():
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    if user != admin_name:
        flash('Only admin can update settings.', 'error')
        return redirect(url_for('main.expenses'))
    currency = sanitize_text(request.form.get('currency', ''))
    categories = sanitize_text(request.form.get('categories', ''))
    try:
        db.session.execute(db.text("INSERT INTO app_setting(key,value) VALUES('currency', :v) ON CONFLICT(key) DO UPDATE SET value=excluded.value"), {"v": currency})
        db.session.execute(db.text("INSERT INTO app_setting(key,value) VALUES('categories', :v) ON CONFLICT(key) DO UPDATE SET value=excluded.value"), {"v": categories})
        db.session.commit()
        flash('Settings saved.', 'success')
    except Exception:
        flash('Failed to save settings.', 'error')
    today = date.today()
    y = request.args.get('y') or today.year
    m = request.args.get('m') or today.month
    sel = request.args.get('sel')
    return redirect(url_for('main.expenses', y=y, m=m, sel=sel, open='recurring'))


@main_bp.route('/expenses/delete/<int:entry_id>', methods=['POST'])
def delete_expense_entry(entry_id):
    entry = ExpenseEntry.query.get_or_404(entry_id)
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if not (user in admin_aliases or user == (entry.payer or '')):
        flash('Not allowed to delete entry.', 'error')
        return redirect(url_for('main.expenses'))
    db.session.delete(entry)
    db.session.commit()
    flash('Expense deleted.', 'success')
    # Preserve view
    today = date.today()
    y = request.args.get('y') or today.year
    m = request.args.get('m') or today.month
    sel = request.args.get('sel')
    return redirect(url_for('main.expenses', y=y, m=m, sel=sel))


@main_bp.route('/expenses/income', methods=['POST'])
def add_income():
    """Add income entry"""
    title = bleach.clean(request.form.get('title', ''))
    amount = float(request.form.get('amount') or 0)
    source = bleach.clean(request.form.get('source') or '')
    recipient = bleach.clean(request.form.get('recipient') or '')
    date_s = request.form.get('date')
    d = datetime.strptime(date_s, '%Y-%m-%d').date() if date_s else date.today()
    
    db.session.add(IncomeEntry(date=d, title=title, source=source, amount=amount, recipient=recipient))
    db.session.commit()
    flash('Income added.', 'success')
    
    # Redirect back to expenses page
    today = date.today()
    y = request.args.get('y') or d.year
    m = request.args.get('m') or d.month
    return redirect(url_for('main.expenses', y=y, m=m))


@main_bp.route('/expenses/edit/<int:entry_id>', methods=['POST'])
def edit_expense_entry(entry_id):
    entry = ExpenseEntry.query.get_or_404(entry_id)
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if not (user in admin_aliases or user == (entry.payer or '')):
        flash('Not allowed to edit entry.', 'error')
        return redirect(url_for('main.expenses'))
    # Update fields
    entry.title = bleach.clean(request.form.get('title', entry.title))
    date_s = request.form.get('date')
    if date_s:
        entry.date = datetime.strptime(date_s, '%Y-%m-%d').date()
    cat_raw = request.form.get('category')
    if cat_raw is not None:
        entry.category = bleach.clean(cat_raw or '') or None
    entry.payer = bleach.clean(request.form.get('payer', entry.payer or ''))
    up = request.form.get('unit_price')
    q = request.form.get('quantity')
    amt = request.form.get('amount')
    entry.unit_price = float(up) if up not in (None, '') else entry.unit_price
    entry.quantity = float(q) if q not in (None, '') else entry.quantity
    entry.amount = float(amt) if amt not in (None, '') else entry.amount
    db.session.commit()
    flash('Expense updated.', 'success')
    # Preserve view
    today = date.today()
    y = request.args.get('y') or entry.date.year
    m = request.args.get('m') or entry.date.month
    sel = request.args.get('sel') or entry.date.strftime('%Y-%m-%d')
    return redirect(url_for('main.expenses', y=y, m=m, sel=sel))


@main_bp.route('/expenses/bulk-delete', methods=['POST'])
def bulk_delete_expenses():
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    ids = request.form.getlist('ids')
    if not ids:
        flash('No entries selected.', 'warning')
        return redirect(url_for('main.expenses'))
    deleted = 0
    for entry_id in ids:
        try:
            entry = ExpenseEntry.query.get(int(entry_id))
            if entry and (user in admin_aliases or user == (entry.payer or '')):
                db.session.delete(entry)
                deleted += 1
        except Exception:
            continue
    db.session.commit()
    flash(f'{deleted} expense(s) deleted.', 'success')
    # Preserve view
    today = date.today()
    y = request.args.get('y') or today.year
    m = request.args.get('m') or today.month
    sel = request.args.get('sel')
    return redirect(url_for('main.expenses', y=y, m=m, sel=sel))


@main_bp.route('/api/expenses/month', methods=['GET'])
def api_expenses_month():
    # Keep recurring data up-to-date before answering
    _generate_recurring_entries_until(date.today())
    today = date.today()
    # Parse query params with clear validation and logging
    y, m = today.year, today.month
    year_q = request.args.get('year')
    month_q = request.args.get('month')
    try:
        if year_q is not None:
            y = int(year_q)
        if month_q is not None:
            m = int(month_q)
    except (ValueError, TypeError):
        current_app.logger.warning('Invalid year/month query params', extra={'year': year_q, 'month': month_q})
        # Keep defaults y, m
    # Validate month range; return helpful 400 if invalid
    if m < 1 or m > 12:
        return jsonify({
            'error': 'Invalid month parameter. Must be an integer between 1 and 12.',
            'year': year_q if year_q is not None else y,
            'month': month_q if month_q is not None else m,
        }), 400
    payload = _build_month_payload(y, m)
    return jsonify(payload)


# ==================== AUTHENTICATION HELPERS ====================

def _check_auth():
    """Verify user is authenticated for state-changing operations"""
    config = current_app.config.get('HOMEHUB_CONFIG', {})
    if config.get('password_hash'):
        if not session.get('authed'):
            return False
    return True


def _require_auth():
    """Decorator to require authentication"""
    def decorator(f):
        def wrapper(*args, **kwargs):
            if not _check_auth():
                return jsonify({'error': 'Unauthorized'}), 401
            return f(*args, **kwargs)
        wrapper.__name__ = f.__name__
        return wrapper
    return decorator


# ==================== FAMILY BUDGETS ====================

@main_bp.route('/expenses/budgets', methods=['GET'])
def list_budgets():
    """Get all family budgets (paginated)"""
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    today = date.today()
    current_month = today.month
    current_year = today.year
    
    budgets = FamilyBudget.query.order_by(
        FamilyBudget.year.desc(),
        FamilyBudget.month.desc()
    ).paginate(page=page, per_page=per_page)
    
    result = []
    for budget in budgets.items:
        spent_total = sum(
            float(cat.spent_amount or 0) for cat in budget.categories
        )
        result.append({
            'id': budget.id,
            'name': budget.budget_name,
            'month': budget.month,
            'year': budget.year,
            'total': float(budget.total_amount),
            'spent': spent_total,
            'currency': budget.currency,
            'created_by': budget.created_by,
            'is_current': budget.month == current_month and budget.year == current_year,
        })
    
    return jsonify({
        'budgets': result,
        'total': budgets.total,
        'pages': budgets.pages,
    })


@main_bp.route('/expenses/budgets', methods=['POST'])
@_require_auth()
def create_budget():
    """Create a new family budget"""
    data = request.get_json() or {}
    
    budget_name = sanitize_text(data.get('name', ''))
    month = int(data.get('month', date.today().month))
    year = int(data.get('year', date.today().year))
    total_amount = float(data.get('total_amount', 0))
    currency = data.get('currency', 'USD')
    created_by = sanitize_text(data.get('created_by', 'Administrator'))
    
    existing = FamilyBudget.query.filter_by(
        month=month, year=year
    ).first()
    
    if existing:
        return jsonify({'error': 'Budget already exists for this month'}), 409
    
    if total_amount <= 0:
        return jsonify({'error': 'Total amount must be positive'}), 400
    
    budget = FamilyBudget(
        budget_name=budget_name,
        month=month,
        year=year,
        total_amount=total_amount,
        currency=currency,
        created_by=created_by,
    )
    db.session.add(budget)
    db.session.commit()
    
    return jsonify({
        'id': budget.id,
        'message': 'Budget created successfully'
    }), 201


@main_bp.route('/expenses/budgets/<int:budget_id>', methods=['GET'])
def get_budget(budget_id):
    """Get detailed budget with categories and spending"""
    budget = FamilyBudget.query.get_or_404(budget_id)
    
    categories = []
    total_spent = 0
    for cat in budget.categories:
        cat_data = {
            'id': cat.id,
            'name': cat.category_name,
            'allocated': float(cat.allocated_amount),
            'spent': float(cat.spent_amount or 0),
            'remaining': float(cat.allocated_amount) - float(cat.spent_amount or 0),
            'percentage': (float(cat.spent_amount or 0) / float(cat.allocated_amount) * 100) if cat.allocated_amount > 0 else 0,
        }
        categories.append(cat_data)
        total_spent += float(cat.spent_amount or 0)
    
    return jsonify({
        'id': budget.id,
        'name': budget.budget_name,
        'month': budget.month,
        'year': budget.year,
        'total': float(budget.total_amount),
        'spent': total_spent,
        'remaining': float(budget.total_amount) - total_spent,
        'currency': budget.currency,
        'created_by': budget.created_by,
        'created_at': budget.created_at.isoformat(),
        'categories': categories,
        'notes': budget.notes,
    })


@main_bp.route('/expenses/budgets/<int:budget_id>', methods=['PUT'])
@_require_auth()
def update_budget(budget_id):
    """Update budget details"""
    budget = FamilyBudget.query.get_or_404(budget_id)
    data = request.get_json() or {}
    
    if 'name' in data:
        budget.budget_name = sanitize_text(data['name'])
    if 'total_amount' in data:
        amount = float(data['total_amount'])
        if amount > 0:
            budget.total_amount = amount
    if 'notes' in data:
        budget.notes = sanitize_text(data['notes'])
    
    budget.updated_at = datetime.utcnow()
    db.session.commit()
    
    return jsonify({'message': 'Budget updated'})


@main_bp.route('/expenses/budgets/<int:budget_id>/categories', methods=['POST'])
@_require_auth()
def add_budget_category(budget_id):
    """Add or update a category allocation"""
    budget = FamilyBudget.query.get_or_404(budget_id)
    data = request.get_json() or {}
    
    category_name = sanitize_text(data.get('category_name', ''))
    allocated_amount = float(data.get('allocated_amount', 0))
    
    if not category_name or allocated_amount <= 0:
        return jsonify({'error': 'Invalid category or amount'}), 400
    
    cat = BudgetCategory.query.filter_by(
        budget_id=budget_id,
        category_name=category_name
    ).first()
    
    if cat:
        cat.allocated_amount = allocated_amount
    else:
        cat = BudgetCategory(
            budget_id=budget_id,
            category_name=category_name,
            allocated_amount=allocated_amount,
        )
        db.session.add(cat)
    
    db.session.commit()
    
    return jsonify({
        'id': cat.id,
        'message': 'Category added/updated'
    }), 201


@main_bp.route('/expenses/budgets/<int:budget_id>/categories/<int:cat_id>', methods=['PUT'])
@_require_auth()
def update_budget_category(budget_id, cat_id):
    """Update category allocation"""
    cat = BudgetCategory.query.filter_by(
        id=cat_id,
        budget_id=budget_id
    ).first_or_404()
    
    data = request.get_json() or {}
    
    if 'allocated_amount' in data:
        cat.allocated_amount = float(data['allocated_amount'])
    
    cat.updated_at = datetime.utcnow()
    db.session.commit()
    
    return jsonify({'message': 'Category updated'})


@main_bp.route('/expenses/budgets/<int:budget_id>/categories/<int:cat_id>', methods=['DELETE'])
@_require_auth()
def delete_budget_category(budget_id, cat_id):
    """Delete a budget category"""
    cat = BudgetCategory.query.filter_by(
        id=cat_id,
        budget_id=budget_id
    ).first_or_404()
    
    db.session.delete(cat)
    db.session.commit()
    
    return jsonify({'message': 'Category deleted'})


# ==================== SHARED EXPENSES ====================

@main_bp.route('/expenses/shared-expenses', methods=['GET'])
def list_shared_expenses():
    """Get shared expenses with optional filtering"""
    month = request.args.get('month', type=int)
    year = request.args.get('year', type=int)
    settled = request.args.get('settled', '').lower() == 'true'
    
    query = SharedExpense.query.order_by(SharedExpense.date.desc())
    
    if month and year:
        query = query.filter(
            db.func.month(SharedExpense.date) == month,
            db.func.year(SharedExpense.date) == year,
        )
    
    if settled:
        query = query.filter_by(settled=True)
    
    expenses = query.limit(100).all()
    
    result = []
    for exp in expenses:
        participants = json.loads(exp.participants) if isinstance(exp.participants, str) else exp.participants
        result.append({
            'id': exp.id,
            'description': exp.description,
            'total_amount': float(exp.total_amount),
            'paid_by': exp.paid_by,
            'date': exp.date.isoformat(),
            'category': exp.category,
            'participants': participants,
            'settled': exp.settled,
        })
    
    return jsonify({'expenses': result})


@main_bp.route('/expenses/shared-expenses', methods=['POST'])
@_require_auth()
def create_shared_expense():
    """Create a shared expense and split it among participants"""
    data = request.get_json() or {}
    
    description = sanitize_text(data.get('description', ''))
    total_amount = float(data.get('total_amount', 0))
    paid_by = sanitize_text(data.get('paid_by', ''))
    expense_date = data.get('date')
    category = data.get('category', '')
    participants = data.get('participants', [])
    
    if not description or total_amount <= 0 or not paid_by:
        return jsonify({'error': 'Missing required fields'}), 400
    
    try:
        expense_date = datetime.strptime(expense_date, '%Y-%m-%d').date() if expense_date else date.today()
    except:
        expense_date = date.today()
    
    participants_total = sum(p.get('amount', 0) for p in participants)
    if abs(participants_total - total_amount) > 0.01:
        return jsonify({'error': 'Participant amounts do not match total'}), 400
    
    expense = SharedExpense(
        description=description,
        total_amount=total_amount,
        paid_by=paid_by,
        date=expense_date,
        category=category,
        participants=json.dumps(participants),
    )
    db.session.add(expense)
    db.session.commit()
    
    return jsonify({
        'id': expense.id,
        'message': 'Shared expense created'
    }), 201


@main_bp.route('/expenses/shared-expenses/<int:expense_id>', methods=['PUT'])
@_require_auth()
def update_shared_expense(expense_id):
    """Update shared expense"""
    expense = SharedExpense.query.get_or_404(expense_id)
    data = request.get_json() or {}
    
    if 'description' in data:
        expense.description = sanitize_text(data['description'])
    if 'settled' in data:
        expense.settled = bool(data['settled'])
    
    expense.updated_at = datetime.utcnow()
    db.session.commit()
    
    return jsonify({'message': 'Expense updated'})


@main_bp.route('/expenses/shared-expenses/<int:expense_id>', methods=['DELETE'])
@_require_auth()
def delete_shared_expense(expense_id):
    """Delete a shared expense"""
    expense = SharedExpense.query.get_or_404(expense_id)
    db.session.delete(expense)
    db.session.commit()
    
    return jsonify({'message': 'Expense deleted'})


# ==================== DEBT TRACKING ====================

@main_bp.route('/expenses/debts', methods=['GET'])
def list_debts():
    """Get all debts"""
    active_only = request.args.get('active', 'true').lower() == 'true'
    
    query = DebtAccount.query.order_by(DebtAccount.created_at.desc())
    
    if active_only:
        query = query.filter_by(active=True)
    
    debts = query.all()
    
    result = []
    for debt in debts:
        result.append({
            'id': debt.id,
            'description': debt.description,
            'debtor': debt.debtor,
            'creditor': debt.creditor,
            'initial_amount': float(debt.initial_amount),
            'current_balance': float(debt.current_balance),
            'interest_rate': float(debt.interest_rate),
            'payoff_date': debt.payoff_date.isoformat() if debt.payoff_date else None,
            'active': debt.active,
            'progress': (float(debt.initial_amount) - float(debt.current_balance)) / float(debt.initial_amount) * 100 if debt.initial_amount > 0 else 0,
        })
    
    return jsonify({'debts': result})


@main_bp.route('/expenses/debts', methods=['POST'])
@_require_auth()
def create_debt():
    """Create a debt account"""
    data = request.get_json() or {}
    
    description = sanitize_text(data.get('description', ''))
    debtor = sanitize_text(data.get('debtor', ''))
    creditor = sanitize_text(data.get('creditor', ''))
    initial_amount = float(data.get('initial_amount', 0))
    interest_rate = float(data.get('interest_rate', 0))
    payoff_date = data.get('payoff_date')
    
    if not description or not debtor or not creditor or initial_amount <= 0:
        return jsonify({'error': 'Missing required fields'}), 400
    
    try:
        payoff_date = datetime.strptime(payoff_date, '%Y-%m-%d').date() if payoff_date else None
    except:
        payoff_date = None
    
    debt = DebtAccount(
        description=description,
        debtor=debtor,
        creditor=creditor,
        initial_amount=initial_amount,
        current_balance=initial_amount,
        interest_rate=interest_rate,
        payoff_date=payoff_date,
    )
    db.session.add(debt)
    db.session.commit()
    
    return jsonify({
        'id': debt.id,
        'message': 'Debt account created'
    }), 201


@main_bp.route('/expenses/debts/<int:debt_id>/payments', methods=['POST'])
@_require_auth()
def record_debt_payment(debt_id):
    """Record a debt payment"""
    debt = DebtAccount.query.get_or_404(debt_id)
    data = request.get_json() or {}
    
    payment_date = data.get('payment_date')
    amount_paid = float(data.get('amount_paid', 0))
    principal = float(data.get('principal', amount_paid))
    interest = float(data.get('interest', 0))
    notes = data.get('notes', '')
    
    if amount_paid <= 0:
        return jsonify({'error': 'Payment amount must be positive'}), 400
    
    if amount_paid > debt.current_balance + (debt.current_balance * debt.interest_rate / 100):
        return jsonify({'error': 'Payment exceeds balance + interest'}), 400
    
    try:
        payment_date = datetime.strptime(payment_date, '%Y-%m-%d').date() if payment_date else date.today()
    except:
        payment_date = date.today()
    
    debt.current_balance = max(0, debt.current_balance - principal)
    
    if debt.current_balance <= 0:
        debt.active = False
    
    payment = DebtPayment(
        debt_id=debt_id,
        payment_date=payment_date,
        amount_paid=amount_paid,
        principal=principal,
        interest=interest,
        notes=sanitize_text(notes),
    )
    db.session.add(payment)
    db.session.commit()
    
    return jsonify({
        'id': payment.id,
        'message': 'Payment recorded'
    }), 201


@main_bp.route('/expenses/debts/<int:debt_id>', methods=['DELETE'])
@_require_auth()
def delete_debt(debt_id):
    """Delete a debt account"""
    debt = DebtAccount.query.get_or_404(debt_id)
    
    DebtPayment.query.filter_by(debt_id=debt_id).delete()
    db.session.delete(debt)
    db.session.commit()
    
    return jsonify({'message': 'Debt deleted'})


# ==================== FINANCIAL GOALS ====================

@main_bp.route('/expenses/goals', methods=['GET'])
def list_goals():
    """Get financial goals"""
    active_only = request.args.get('active', 'true').lower() == 'true'
    
    query = FinancialGoal.query.order_by(FinancialGoal.priority)
    
    if active_only:
        query = query.filter_by(active=True)
    
    goals = query.all()
    
    result = []
    for goal in goals:
        result.append({
            'id': goal.id,
            'name': goal.goal_name,
            'description': goal.description,
            'target_amount': float(goal.target_amount),
            'current_amount': float(goal.current_amount),
            'target_date': goal.target_date.isoformat() if goal.target_date else None,
            'category': goal.category,
            'priority': goal.priority,
            'created_by': goal.created_by,
            'active': goal.active,
            'progress': (float(goal.current_amount) / float(goal.target_amount) * 100) if goal.target_amount > 0 else 0,
        })
    
    return jsonify({'goals': result})


@main_bp.route('/expenses/goals', methods=['POST'])
@_require_auth()
def create_goal():
    """Create a financial goal"""
    data = request.get_json() or {}
    
    goal_name = sanitize_text(data.get('name', ''))
    description = sanitize_text(data.get('description', ''))
    target_amount = float(data.get('target_amount', 0))
    target_date = data.get('target_date')
    category = data.get('category', '')
    priority = int(data.get('priority', 2))
    created_by = sanitize_text(data.get('created_by', 'Administrator'))
    
    if not goal_name or target_amount <= 0:
        return jsonify({'error': 'Invalid goal name or amount'}), 400
    
    try:
        target_date = datetime.strptime(target_date, '%Y-%m-%d').date() if target_date else None
    except:
        target_date = None
    
    goal = FinancialGoal(
        goal_name=goal_name,
        description=description,
        target_amount=target_amount,
        target_date=target_date,
        category=category,
        priority=priority,
        created_by=created_by,
    )
    db.session.add(goal)
    db.session.commit()
    
    return jsonify({
        'id': goal.id,
        'message': 'Goal created'
    }), 201


@main_bp.route('/expenses/goals/<int:goal_id>/contributions', methods=['POST'])
@_require_auth()
def add_goal_contribution(goal_id):
    """Contribute towards a financial goal"""
    goal = FinancialGoal.query.get_or_404(goal_id)
    data = request.get_json() or {}
    
    contribution_date = data.get('contribution_date')
    amount = float(data.get('amount', 0))
    contributor = sanitize_text(data.get('contributor', ''))
    notes = data.get('notes', '')
    
    if amount <= 0:
        return jsonify({'error': 'Amount must be positive'}), 400
    
    try:
        contribution_date = datetime.strptime(contribution_date, '%Y-%m-%d').date() if contribution_date else date.today()
    except:
        contribution_date = date.today()
    
    goal.current_amount = min(goal.target_amount, float(goal.current_amount) + amount)
    
    contribution = GoalContribution(
        goal_id=goal_id,
        contribution_date=contribution_date,
        amount=amount,
        contributor=contributor,
        notes=sanitize_text(notes),
    )
    db.session.add(contribution)
    db.session.commit()
    
    return jsonify({
        'id': contribution.id,
        'message': 'Contribution recorded'
    }), 201


# ==================== PLAID INTEGRATION ====================

@main_bp.route('/expenses/plaid/link-token', methods=['POST'])
@_require_auth()
def create_plaid_link_token():
    """Create a Plaid Link token for account connection"""
    if not plaid_service.is_configured():
        return jsonify({'error': 'Plaid is not configured'}), 503
    
    data = request.get_json() or {}
    user = sanitize_text(data.get('user', ''))
    redirect_uri = data.get('redirect_uri')
    
    try:
        link_token = plaid_service.create_link_token(user, redirect_uri)
        return jsonify({'link_token': link_token})
    except Exception as e:
        logger.error(f"Error creating Plaid link token: {e}")
        return jsonify({'error': str(e)}), 500


@main_bp.route('/expenses/plaid/exchange-token', methods=['POST'])
@_require_auth()
def exchange_plaid_token():
    """Exchange public token for access token and link account"""
    if not plaid_service.is_configured():
        return jsonify({'error': 'Plaid is not configured'}), 503
    
    data = request.get_json() or {}
    public_token = data.get('public_token', '')
    user = sanitize_text(data.get('user', ''))
    
    if not public_token or not user:
        return jsonify({'error': 'Missing token or user'}), 400
    
    try:
        accounts = plaid_service.exchange_public_token(public_token, user)
        return jsonify({
            'message': f'Successfully linked {len(accounts)} account(s)',
            'accounts': [
                {
                    'id': a.id,
                    'name': a.account_name,
                    'type': a.account_type,
                    'balance': a.balance,
                }
                for a in accounts
            ]
        }), 201
    except Exception as e:
        logger.error(f"Error exchanging Plaid token: {e}")
        return jsonify({'error': str(e)}), 500


@main_bp.route('/expenses/plaid/accounts', methods=['GET'])
def list_plaid_accounts():
    """Get linked Plaid accounts"""
    accounts = PlaidAccount.query.filter_by(active=True).all()
    
    result = []
    for account in accounts:
        result.append({
            'id': account.id,
            'user': account.user,
            'account_name': account.account_name,
            'account_type': account.account_type,
            'balance': float(account.balance) if account.balance else 0,
            'currency': account.currency,
            'synced_at': account.synced_at.isoformat() if account.synced_at else None,
        })
    
    return jsonify({'accounts': result})


@main_bp.route('/expenses/plaid/accounts/<int:account_id>/sync', methods=['POST'])
@_require_auth()
def sync_plaid_account(account_id):
    """Sync transactions and balances for a Plaid account"""
    account = PlaidAccount.query.get_or_404(account_id)
    
    if not plaid_service.is_configured():
        return jsonify({'error': 'Plaid is not configured'}), 503
    
    trans_count = plaid_service.sync_transactions(account_id)
    balance = plaid_service.sync_balances(account_id)
    
    return jsonify({
        'message': 'Sync completed',
        'transactions_synced': trans_count,
        'balance': balance,
    })


@main_bp.route('/expenses/plaid/accounts/<int:account_id>/transactions', methods=['GET'])
def get_plaid_transactions(account_id):
    """Get transactions for a Plaid account"""
    account = PlaidAccount.query.get_or_404(account_id)
    
    days = request.args.get('days', 30, type=int)
    start_date = date.today() - timedelta(days=days)
    
    transactions = plaid_service.get_account_transactions(
        account_id,
        start_date=start_date,
        limit=100
    )
    
    result = []
    for trans in transactions:
        result.append({
            'id': trans.id,
            'date': trans.transaction_date.isoformat(),
            'amount': float(trans.amount),
            'description': trans.description,
            'merchant': trans.merchant,
            'category': trans.category,
            'pending': trans.pending,
        })
    
    return jsonify({'transactions': result})


@main_bp.route('/expenses/plaid/accounts/<int:account_id>', methods=['DELETE'])
@_require_auth()
def disconnect_plaid_account(account_id):
    """Disconnect a Plaid account"""
    if plaid_service.disconnect_account(account_id):
        return jsonify({'message': 'Account disconnected'})
    else:
        return jsonify({'error': 'Failed to disconnect account'}), 500


# ==================== ANALYTICS & REPORTING ====================

@main_bp.route('/expenses/analytics/summary', methods=['GET'])
def get_analytics_summary():
    """Get comprehensive financial analytics summary"""
    today = date.today()
    month_start = date(today.year, today.month, 1)
    month_end = date(today.year, today.month, 28) if today.month == 2 else \
                date(today.year, today.month if today.month < 12 else 1, 
                     30 if today.month in [4,6,9,11] else 31)
    
    current_budget = FamilyBudget.query.filter_by(
        month=today.month,
        year=today.year
    ).first()
    
    month_expenses = ExpenseEntry.query.filter(
        ExpenseEntry.date >= month_start,
        ExpenseEntry.date <= month_end
    ).all()
    
    active_debts = DebtAccount.query.filter_by(active=True).all()
    goals = FinancialGoal.query.filter_by(active=True).all()
    shared_expenses = SharedExpense.query.filter(
        SharedExpense.date >= month_start,
        SharedExpense.date <= month_end
    ).all()
    
    month_expense_total = sum(float(e.amount or 0) for e in month_expenses)
    shared_expense_total = sum(float(e.total_amount) for e in shared_expenses)
    total_debt = sum(float(d.current_balance) for d in active_debts)
    goal_total_target = sum(float(g.target_amount) for g in goals)
    goal_total_current = sum(float(g.current_amount) for g in goals)
    
    return jsonify({
        'period': f"{today.month}/{today.year}",
        'budget': {
            'total': float(current_budget.total_amount) if current_budget else 0,
            'spent': month_expense_total + shared_expense_total,
            'remaining': float(current_budget.total_amount) - (month_expense_total + shared_expense_total) if current_budget else 0,
        },
        'debts': {
            'active_count': len(active_debts),
            'total_balance': total_debt,
        },
        'goals': {
            'count': len(goals),
            'target_total': goal_total_target,
            'current_total': goal_total_current,
            'progress': (goal_total_current / goal_total_target * 100) if goal_total_target > 0 else 0,
        },
        'transactions': {
            'individual': len(month_expenses),
            'shared': len(shared_expenses),
        }
    })


# ==================== PLAID ACCOUNT ANALYTICS ====================

@main_bp.route('/expenses/accounts/summary', methods=['GET'])
def get_accounts_summary():
    """Get summary of all linked Plaid accounts with balances"""
    accounts = PlaidAccount.query.filter_by(active=True).all()
    
    total_balance = 0
    account_details = []
    
    for account in accounts:
        balance = float(account.balance) if account.balance else 0
        total_balance += balance
        
        # Get recent transactions for this account
        recent_trans = PlaidTransaction.query.filter_by(
            plaid_account_id=account.id
        ).order_by(PlaidTransaction.transaction_date.desc()).limit(5).all()
        
        account_details.append({
            'id': account.id,
            'name': account.account_name,
            'type': account.account_type,
            'subtype': account.account_subtype,
            'balance': balance,
            'currency': account.currency,
            'mask': account.account_id[-4:] if len(account.account_id) >= 4 else account.account_id,
            'synced_at': account.synced_at.isoformat() if account.synced_at else None,
            'recent_transactions': len(recent_trans),
        })
    
    return jsonify({
        'total_accounts': len(accounts),
        'total_balance': total_balance,
        'accounts': account_details,
    })


@main_bp.route('/expenses/accounts/<int:account_id>/summary', methods=['GET'])
def get_account_summary(account_id):
    """Get detailed summary for a specific Plaid account"""
    account = PlaidAccount.query.get_or_404(account_id)
    
    # Get transactions for various time periods
    today = date.today()
    month_start = date(today.year, today.month, 1)
    year_start = date(today.year, 1, 1)
    
    # Current month transactions
    month_trans = PlaidTransaction.query.filter(
        PlaidTransaction.plaid_account_id == account_id,
        PlaidTransaction.transaction_date >= month_start,
        PlaidTransaction.transaction_date <= today
    ).all()
    
    # Year-to-date transactions
    year_trans = PlaidTransaction.query.filter(
        PlaidTransaction.plaid_account_id == account_id,
        PlaidTransaction.transaction_date >= year_start,
        PlaidTransaction.transaction_date <= today
    ).all()
    
    # Category breakdown
    category_breakdown = {}
    for trans in month_trans:
        category = trans.category or 'Uncategorized'
        if category not in category_breakdown:
            category_breakdown[category] = {'count': 0, 'total': 0}
        category_breakdown[category]['count'] += 1
        category_breakdown[category]['total'] += float(trans.amount)
    
    # Sort by total
    sorted_categories = sorted(
        category_breakdown.items(),
        key=lambda x: x[1]['total'],
        reverse=True
    )
    
    month_total = sum(float(t.amount) for t in month_trans)
    year_total = sum(float(t.amount) for t in year_trans)
    
    return jsonify({
        'account': {
            'id': account.id,
            'name': account.account_name,
            'type': account.account_type,
            'balance': float(account.balance) if account.balance else 0,
            'currency': account.currency,
        },
        'period': {
            'month': {
                'total_transactions': len(month_trans),
                'total_spent': month_total,
                'average_transaction': month_total / len(month_trans) if month_trans else 0,
            },
            'year': {
                'total_transactions': len(year_trans),
                'total_spent': year_total,
                'average_transaction': year_total / len(year_trans) if year_trans else 0,
            }
        },
        'top_categories': [
            {
                'category': cat,
                'count': data['count'],
                'total': data['total'],
                'average': data['total'] / data['count']
            }
            for cat, data in sorted_categories[:10]
        ],
    })


@main_bp.route('/expenses/accounts/<int:account_id>/transactions', methods=['GET'])
def get_account_transactions(account_id):
    """Get paginated transactions for a specific account with filtering"""
    account = PlaidAccount.query.get_or_404(account_id)
    
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    category = request.args.get('category')
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    
    query = PlaidTransaction.query.filter_by(
        plaid_account_id=account_id
    ).order_by(PlaidTransaction.transaction_date.desc())
    
    if category:
        query = query.filter_by(category=category)
    
    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            query = query.filter(PlaidTransaction.transaction_date >= start_date)
        except ValueError:
            pass
    
    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            query = query.filter(PlaidTransaction.transaction_date <= end_date)
        except ValueError:
            pass
    
    paginated = query.paginate(page=page, per_page=per_page)
    
    transactions = []
    for trans in paginated.items:
        transactions.append({
            'id': trans.id,
            'date': trans.transaction_date.isoformat(),
            'amount': float(trans.amount),
            'description': trans.description,
            'merchant': trans.merchant,
            'category': trans.category,
            'pending': trans.pending,
        })
    
    return jsonify({
        'account': {
            'id': account.id,
            'name': account.account_name,
        },
        'transactions': transactions,
        'total': paginated.total,
        'pages': paginated.pages,
        'current_page': page,
    })


@main_bp.route('/expenses/accounts/<int:account_id>/expenses-by-category', methods=['GET'])
def get_account_expenses_by_category(account_id):
    """Get expense breakdown by category for an account"""
    account = PlaidAccount.query.get_or_404(account_id)
    
    # Get transactions for current month
    today = date.today()
    month_start = date(today.year, today.month, 1)
    
    transactions = PlaidTransaction.query.filter(
        PlaidTransaction.plaid_account_id == account_id,
        PlaidTransaction.transaction_date >= month_start,
        PlaidTransaction.transaction_date <= today
    ).all()
    
    # Build category breakdown
    categories = {}
    for trans in transactions:
        category = trans.category or 'Uncategorized'
        if category not in categories:
            categories[category] = {
                'count': 0,
                'total': 0,
                'transactions': []
            }
        categories[category]['count'] += 1
        categories[category]['total'] += float(trans.amount)
        categories[category]['transactions'].append({
            'date': trans.transaction_date.isoformat(),
            'amount': float(trans.amount),
            'description': trans.description,
            'merchant': trans.merchant,
        })
    
    # Sort by total descending
    sorted_categories = sorted(
        categories.items(),
        key=lambda x: x[1]['total'],
        reverse=True
    )
    
    return jsonify({
        'account': {
            'id': account.id,
            'name': account.account_name,
            'month': f"{today.year}-{today.month:02d}",
        },
        'categories': [
            {
                'name': cat,
                'count': data['count'],
                'total': data['total'],
                'average_per_transaction': data['total'] / data['count'],
                'percentage': (data['total'] / sum(c['total'] for c in categories.values()) * 100) if categories else 0,
            }
            for cat, data in sorted_categories
        ],
        'total_spent': sum(c['total'] for c in categories.values()),
    })


@main_bp.route('/expenses/budget/family', methods=['GET', 'POST'])
@_require_auth()
def manage_family_budget():
    """Get or create family-wide budget"""
    if request.method == 'GET':
        today = date.today()
        budget = FamilyBudget.query.filter_by(
            month=today.month,
            year=today.year
        ).first()
        
        if not budget:
            return jsonify({'error': 'No budget for current month'}), 404
        
        categories = []
        total_spent = 0
        for cat in budget.categories:
            cat_spent = float(cat.spent_amount or 0)
            total_spent += cat_spent
            categories.append({
                'id': cat.id,
                'name': cat.category_name,
                'allocated': float(cat.allocated_amount),
                'spent': cat_spent,
                'remaining': float(cat.allocated_amount) - cat_spent,
                'percentage': (cat_spent / float(cat.allocated_amount) * 100) if cat.allocated_amount > 0 else 0,
            })
        
        return jsonify({
            'id': budget.id,
            'month': budget.month,
            'year': budget.year,
            'total_budget': float(budget.total_amount),
            'total_spent': total_spent,
            'total_remaining': float(budget.total_amount) - total_spent,
            'categories': categories,
        })
    
    # POST - Create new budget
    data = request.get_json() or {}
    today = date.today()
    month = int(data.get('month', today.month))
    year = int(data.get('year', today.year))
    total_amount = float(data.get('total_amount', 0))
    
    if total_amount <= 0:
        return jsonify({'error': 'Total amount must be positive'}), 400
    
    existing = FamilyBudget.query.filter_by(month=month, year=year).first()
    if existing:
        return jsonify({'error': 'Budget already exists for this month'}), 409
    
    budget = FamilyBudget(
        budget_name=f"Family Budget {month}/{year}",
        month=month,
        year=year,
        total_amount=total_amount,
        currency='USD',
        created_by='Administrator',
    )
    db.session.add(budget)
    db.session.commit()
    
    return jsonify({
        'id': budget.id,
        'message': 'Family budget created'
    }), 201


@main_bp.route('/expenses/budget/family/member/<member>', methods=['GET'])
def get_member_spending(member):
    """Get spending summary for a specific family member"""
    today = date.today()
    month_start = date(today.year, today.month, 1)
    month_end = today
    
    # Get member's expenses
    expenses = ExpenseEntry.query.filter(
        ExpenseEntry.payer == member,
        ExpenseEntry.date >= month_start,
        ExpenseEntry.date <= month_end
    ).all()
    
    # Get member's share of shared expenses
    shared_expenses = SharedExpense.query.filter(
        SharedExpense.date >= month_start,
        SharedExpense.date <= month_end
    ).all()
    
    member_shared_total = 0
    for shared in shared_expenses:
        participants = json.loads(shared.participants) if isinstance(shared.participants, str) else shared.participants
        for p in participants:
            if p.get('name') == member:
                member_shared_total += float(p.get('amount', 0))
    
    # Group by category
    category_spending = {}
    for exp in expenses:
        category = exp.category or 'Uncategorized'
        if category not in category_spending:
            category_spending[category] = 0
        category_spending[category] += float(exp.amount or 0)
    
    total_personal = sum(float(e.amount or 0) for e in expenses)
    
    return jsonify({
        'member': member,
        'period': f"{today.year}-{today.month:02d}",
        'personal_spending': {
            'total': total_personal,
            'count': len(expenses),
            'by_category': category_spending,
        },
        'shared_spending': {
            'total': member_shared_total,
        },
        'combined_total': total_personal + member_shared_total,
    })


@main_bp.route('/expenses/budget/recommendations', methods=['GET'])
def get_budget_recommendations():
    """Get budget recommendations based on spending patterns"""
    # Analyze last 3 months of data
    today = date.today()
    three_months_ago = today - timedelta(days=90)
    
    expenses = ExpenseEntry.query.filter(
        ExpenseEntry.date >= three_months_ago,
        ExpenseEntry.date <= today
    ).all()
    
    # Category analysis
    category_totals = {}
    category_counts = {}
    
    for exp in expenses:
        category = exp.category or 'Uncategorized'
        amount = float(exp.amount or 0)
        
        if category not in category_totals:
            category_totals[category] = 0
            category_counts[category] = 0
        
        category_totals[category] += amount
        category_counts[category] += 1
    
    # Calculate monthly averages
    months_analyzed = 3
    recommendations = []
    
    for category, total in category_totals.items():
        monthly_avg = total / months_analyzed
        recommended = monthly_avg * 1.1  # Add 10% buffer
        
        recommendations.append({
            'category': category,
            'monthly_average': round(monthly_avg, 2),
            'recommended_budget': round(recommended, 2),
            'transaction_count': category_counts[category],
            'confidence': 'high' if category_counts[category] >= 5 else 'medium',
        })
    
    # Sort by monthly average descending
    recommendations.sort(key=lambda x: x['monthly_average'], reverse=True)
    
    return jsonify({
        'period': '3 months',
        'total_spent': sum(category_totals.values()),
        'total_recommendations': sum(r['recommended_budget'] for r in recommendations),
        'recommendations': recommendations,
    })

