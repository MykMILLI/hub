"""
Money Management Blueprint - Comprehensive Family Budget & Expense Tracking

Features:
- Family budgets with category allocation
- Shared expense splitting and debt tracking
- Financial goals and contributions
- Plaid bank account integration
- Expense analytics and reporting
"""

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, current_app, session, g
from ..models import (
    db, FamilyBudget, BudgetCategory, SharedExpense, DebtAccount, DebtPayment,
    FinancialGoal, GoalContribution, PlaidAccount, PlaidTransaction, ExpenseEntry,
    FamilyMember
)
from ..plaid_service import plaid_service
from ..security import sanitize_text
from datetime import datetime, date, timedelta
import json
import logging
from decimal import Decimal

money_bp = Blueprint('money', __name__, url_prefix='/money')
logger = logging.getLogger(__name__)


# ==================== AUTHENTICATION ====================

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


# ==================== PAGE ROUTES ====================

@money_bp.route('/')
def dashboard():
    """Redirect to consolidated expense tracker"""
    return redirect(url_for('main.expenses'))


# ==================== FAMILY BUDGETS ====================

@money_bp.route('/budgets', methods=['GET'])
def list_budgets():
    """Get all family budgets (paginated)"""
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    today = date.today()
    current_month = today.month
    current_year = today.year
    
    # Get current and recent months
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


@money_bp.route('/budgets', methods=['POST'])
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
    
    # Check if budget already exists for this month/year
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


@money_bp.route('/budgets/<int:budget_id>', methods=['GET'])
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


@money_bp.route('/budgets/<int:budget_id>', methods=['PUT'])
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


@money_bp.route('/budgets/<int:budget_id>/categories', methods=['POST'])
@_require_auth()
def add_budget_category(budget_id):
    """Add or update a category allocation"""
    budget = FamilyBudget.query.get_or_404(budget_id)
    data = request.get_json() or {}
    
    category_name = sanitize_text(data.get('category_name', ''))
    allocated_amount = float(data.get('allocated_amount', 0))
    
    if not category_name or allocated_amount <= 0:
        return jsonify({'error': 'Invalid category or amount'}), 400
    
    # Check if category already exists
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


@money_bp.route('/budgets/<int:budget_id>/categories/<int:cat_id>', methods=['PUT'])
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


@money_bp.route('/budgets/<int:budget_id>/categories/<int:cat_id>', methods=['DELETE'])
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

@money_bp.route('/shared-expenses', methods=['GET'])
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


@money_bp.route('/shared-expenses', methods=['POST'])
@_require_auth()
def create_shared_expense():
    """Create a shared expense and split it among participants"""
    data = request.get_json() or {}
    
    description = sanitize_text(data.get('description', ''))
    total_amount = float(data.get('total_amount', 0))
    paid_by = sanitize_text(data.get('paid_by', ''))
    expense_date = data.get('date')
    category = data.get('category', '')
    participants = data.get('participants', [])  # [{'name': 'Alice', 'amount': 50}, ...]
    
    if not description or total_amount <= 0 or not paid_by:
        return jsonify({'error': 'Missing required fields'}), 400
    
    try:
        expense_date = datetime.strptime(expense_date, '%Y-%m-%d').date() if expense_date else date.today()
    except:
        expense_date = date.today()
    
    # Validate participants
    participants_total = sum(p.get('amount', 0) for p in participants)
    if abs(participants_total - total_amount) > 0.01:  # Allow for floating point rounding
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


@money_bp.route('/shared-expenses/<int:expense_id>', methods=['PUT'])
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


@money_bp.route('/shared-expenses/<int:expense_id>', methods=['DELETE'])
@_require_auth()
def delete_shared_expense(expense_id):
    """Delete a shared expense"""
    expense = SharedExpense.query.get_or_404(expense_id)
    db.session.delete(expense)
    db.session.commit()
    
    return jsonify({'message': 'Expense deleted'})


# ==================== DEBT TRACKING ====================

@money_bp.route('/debts', methods=['GET'])
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


@money_bp.route('/debts', methods=['POST'])
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


@money_bp.route('/debts/<int:debt_id>/payments', methods=['POST'])
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
    
    # Update debt balance
    debt.current_balance = max(0, debt.current_balance - principal)
    
    # If debt is fully paid, mark as inactive
    if debt.current_balance <= 0:
        debt.active = False
    
    # Record payment
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


@money_bp.route('/debts/<int:debt_id>', methods=['DELETE'])
@_require_auth()
def delete_debt(debt_id):
    """Delete a debt account"""
    debt = DebtAccount.query.get_or_404(debt_id)
    
    # Also delete related payments
    DebtPayment.query.filter_by(debt_id=debt_id).delete()
    db.session.delete(debt)
    db.session.commit()
    
    return jsonify({'message': 'Debt deleted'})


# ==================== FINANCIAL GOALS ====================

@money_bp.route('/goals', methods=['GET'])
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


@money_bp.route('/goals', methods=['POST'])
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


@money_bp.route('/goals/<int:goal_id>/contributions', methods=['POST'])
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
    
    # Update goal current amount
    goal.current_amount = min(goal.target_amount, float(goal.current_amount) + amount)
    
    # Record contribution
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

@money_bp.route('/plaid/link-token', methods=['POST'])
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


@money_bp.route('/plaid/exchange-token', methods=['POST'])
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


@money_bp.route('/plaid/accounts', methods=['GET'])
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


@money_bp.route('/plaid/accounts/<int:account_id>/sync', methods=['POST'])
@_require_auth()
def sync_plaid_account(account_id):
    """Sync transactions and balances for a Plaid account"""
    account = PlaidAccount.query.get_or_404(account_id)
    
    if not plaid_service.is_configured():
        return jsonify({'error': 'Plaid is not configured'}), 503
    
    # Sync transactions
    trans_count = plaid_service.sync_transactions(account_id)
    
    # Sync balance
    balance = plaid_service.sync_balances(account_id)
    
    return jsonify({
        'message': 'Sync completed',
        'transactions_synced': trans_count,
        'balance': balance,
    })


@money_bp.route('/plaid/accounts/<int:account_id>/transactions', methods=['GET'])
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


@money_bp.route('/plaid/accounts/<int:account_id>', methods=['DELETE'])
@_require_auth()
def disconnect_plaid_account(account_id):
    """Disconnect a Plaid account"""
    if plaid_service.disconnect_account(account_id):
        return jsonify({'message': 'Account disconnected'})
    else:
        return jsonify({'error': 'Failed to disconnect account'}), 500


# ==================== ANALYTICS & REPORTING ====================

@money_bp.route('/analytics/summary', methods=['GET'])
def get_analytics_summary():
    """Get comprehensive financial analytics summary"""
    today = date.today()
    month_start = date(today.year, today.month, 1)
    month_end = date(today.year, today.month, 28) if today.month == 2 else \
                date(today.year, today.month if today.month < 12 else 1, 
                     30 if today.month in [4,6,9,11] else 31)
    
    # Current month budget
    current_budget = FamilyBudget.query.filter_by(
        month=today.month,
        year=today.year
    ).first()
    
    # Expenses this month
    month_expenses = ExpenseEntry.query.filter(
        ExpenseEntry.date >= month_start,
        ExpenseEntry.date <= month_end
    ).all()
    
    # Debts
    active_debts = DebtAccount.query.filter_by(active=True).all()
    
    # Goals
    goals = FinancialGoal.query.filter_by(active=True).all()
    
    # Shared expenses
    shared_expenses = SharedExpense.query.filter(
        SharedExpense.date >= month_start,
        SharedExpense.date <= month_end
    ).all()
    
    month_expense_charged_total = 0.0
    month_expense_paid_total = 0.0
    month_expense_due_total = 0.0
    for entry in month_expenses:
        amount = max(float(entry.amount or 0), 0.0)
        paid_raw = max(float(getattr(entry, 'amount_paid', 0.0) or 0.0), 0.0)
        month_expense_charged_total += amount
        if entry.recurring_id:
            paid_amount = min(paid_raw, amount)
            due_amount = max(amount - paid_amount, 0.0)
        else:
            paid_amount = amount
            due_amount = 0.0
        month_expense_paid_total += paid_amount
        month_expense_due_total += due_amount

    month_expense_total = month_expense_paid_total
    shared_expense_total = sum(float(e.total_amount) for e in shared_expenses)
    total_debt = sum(float(d.current_balance) for d in active_debts)
    goal_total_target = sum(float(g.target_amount) for g in goals)
    goal_total_current = sum(float(g.current_amount) for g in goals)
    
    return jsonify({
        'period': f"{today.month}/{today.year}",
        'budget': {
            'total': float(current_budget.total_amount) if current_budget else 0,
            'spent': month_expense_total + shared_expense_total,
            'paid': month_expense_paid_total,
            'due': month_expense_due_total,
            'charged': month_expense_charged_total,
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
