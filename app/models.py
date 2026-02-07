from . import db
from datetime import datetime

class Note(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    creator = db.Column(db.String(64), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(256), nullable=False)
    creator = db.Column(db.String(64), nullable=False)
    upload_time = db.Column(db.DateTime, default=datetime.utcnow)
    category = db.Column(db.String(64), default='uncategorized')

class Media(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(256))
    url = db.Column(db.String(512))
    creator = db.Column(db.String(64))
    download_time = db.Column(db.DateTime, default=datetime.utcnow)
    filepath = db.Column(db.String(512))
    status = db.Column(db.String(32), default='done')  # pending, done, error
    progress = db.Column(db.Text)  # latest progress line or JSON

class PDF(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(256))
    creator = db.Column(db.String(64))
    upload_time = db.Column(db.DateTime, default=datetime.utcnow)
    compressed_path = db.Column(db.String(512))

class ShoppingItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item = db.Column(db.String(256), nullable=False)
    checked = db.Column(db.Boolean, default=False)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    # JSON-encoded list of tags (e.g., ["Costco", "Dairy"]) for filtering/grouping
    tags = db.Column(db.Text, default='[]')
    price = db.Column(db.Float)  # price per unit
    quantity = db.Column(db.Float, default=1.0)  # quantity to purchase

class GroceryHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item = db.Column(db.String(256), nullable=False)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class CommonItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), unique=True, nullable=False)
    default_price = db.Column(db.Float)
    category = db.Column(db.String(64))
    last_used = db.Column(db.DateTime)

class HomeStatus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(16), default='Away')

class Chore(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.Text, nullable=False)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    done = db.Column(db.Boolean, default=False)
    # JSON-encoded list of tags (e.g., ["Alice", "Weekend"]) for assignment/filtering
    tags = db.Column(db.Text, default='[]')

class Recipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(256), nullable=False)
    link = db.Column(db.String(512))
    ingredients = db.Column(db.Text)
    instructions = db.Column(db.Text)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    # JSON-encoded list of tags (e.g., ["Dessert", "Quick", "Vegetarian"]) for filtering/grouping
    tags = db.Column(db.Text, default='[]')

class ExpiryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), nullable=False)
    expiry_date = db.Column(db.Date)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class ShortURL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_url = db.Column(db.String(512), nullable=False)
    short_code = db.Column(db.String(16), unique=True, nullable=False)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class QRCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    filename = db.Column(db.String(256), nullable=False)
    original_input = db.Column(db.Text)  # what user typed (for history display)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Notice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, default='')
    updated_by = db.Column(db.String(64))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    time = db.Column(db.String(5))  # HH:MM (optional)
    title = db.Column(db.String(256), nullable=False)
    description = db.Column(db.Text)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    # New fields (phase 1) - added via auto-migration if missing
    category = db.Column(db.String(64))  # key referencing configured category
    color = db.Column(db.String(16))     # optional override hex color
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Tie back to a recurring rule (if generated)
    recurring_id = db.Column(db.Integer)

class MemberStatus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    text = db.Column(db.Text, default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

class RecurringExpense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(256), nullable=False)
    unit_price = db.Column(db.Float, default=0.0)
    default_quantity = db.Column(db.Float, default=1.0)
    frequency = db.Column(db.String(16), default='daily')  # daily|weekly|monthly
    category = db.Column(db.String(64))
    monthly_mode = db.Column(db.String(16), default='day_of_month')  # calendar|day_of_month
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    last_generated_date = db.Column(db.Date)
    effective_from = db.Column(db.Date)  # apply changes from this date forward
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class RecurringReminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(256), nullable=False)
    description = db.Column(db.Text)
    creator = db.Column(db.String(64))
    # Legacy fields kept for backward compatibility
    frequency = db.Column(db.String(16), default='daily')  # daily|weekly|monthly (legacy)
    monthly_mode = db.Column(db.String(16), default='day_of_month')  # calendar|day_of_month (legacy)
    # New flexible recurrence
    interval = db.Column(db.Integer, default=1)  # e.g., 1,2,3
    unit = db.Column(db.String(8), default='day')  # 'day'|'week'|'month'|'year'
    time = db.Column(db.String(5))  # optional HH:MM
    category = db.Column(db.String(64))
    color = db.Column(db.String(16))
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    last_generated_date = db.Column(db.Date)
    effective_from = db.Column(db.Date)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class ExpenseEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    title = db.Column(db.String(256), nullable=False)
    category = db.Column(db.String(64))
    unit_price = db.Column(db.Float)
    quantity = db.Column(db.Float)
    amount = db.Column(db.Float, nullable=False)
    payer = db.Column(db.String(64))
    recurring_id = db.Column(db.Integer, db.ForeignKey('recurring_expense.id'))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class IncomeEntry(db.Model):
    """Track income entries for the family"""
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    title = db.Column(db.String(256), nullable=False)
    source = db.Column(db.String(128))  # e.g., "Salary", "Freelance"
    amount = db.Column(db.Float, nullable=False)
    recipient = db.Column(db.String(64))  # who received the income
    notes = db.Column(db.Text)
    include_in_family_total = db.Column(db.Boolean, default=True)  # Toggle for family finance calculation
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class RecurringBillPayment(db.Model):
    """Track payment status for recurring bills by month"""
    id = db.Column(db.Integer, primary_key=True)
    recurring_expense_id = db.Column(db.Integer, db.ForeignKey('recurring_expense.id'), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    is_paid = db.Column(db.Boolean, default=False)
    paid_by = db.Column(db.String(64))  # who marked it as paid
    paid_date = db.Column(db.DateTime)  # when it was marked as paid
    notes = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    __table_args__ = (db.UniqueConstraint('recurring_expense_id', 'year', 'month', name='unique_bill_payment'),)

class HubSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    hub_name = db.Column(db.String(256), default='HomeHub')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class FamilyMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    pin = db.Column(db.String(4), nullable=True)  # 4-digit PIN for member login
    status = db.Column(db.String(50), default='home')  # home, away, at_work, traveling, or custom
    status_updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    avatar = db.Column(db.Text, nullable=True)  # base64 encoded avatar image
    added_at = db.Column(db.DateTime, default=datetime.utcnow)

class FamilyBudget(db.Model):
    """Shared family budget for a specific month/year"""
    id = db.Column(db.Integer, primary_key=True)
    budget_name = db.Column(db.String(256), nullable=False)
    month = db.Column(db.Integer, nullable=False)  # 1-12
    year = db.Column(db.Integer, nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), default='USD')
    created_by = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    notes = db.Column(db.Text)

class BudgetCategory(db.Model):
    """Allocated budget for a specific category within a family budget"""
    id = db.Column(db.Integer, primary_key=True)
    budget_id = db.Column(db.Integer, db.ForeignKey('family_budget.id'), nullable=False)
    category_name = db.Column(db.String(128), nullable=False)
    allocated_amount = db.Column(db.Float, nullable=False)
    spent_amount = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SharedExpense(db.Model):
    """Expense split between multiple family members"""
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(256), nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    paid_by = db.Column(db.String(64), nullable=False)  # who paid
    date = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(64))
    # JSON list of participants: [{"name": "Alice", "amount": 50.0}, {"name": "Bob", "amount": 50.0}]
    participants = db.Column(db.Text, nullable=False)  # JSON
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    settled = db.Column(db.Boolean, default=False)

class DebtAccount(db.Model):
    """Track personal debts within the family"""
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(256), nullable=False)
    debtor = db.Column(db.String(64), nullable=False)  # who owes
    creditor = db.Column(db.String(64), nullable=False)  # who is owed
    initial_amount = db.Column(db.Float, nullable=False)
    current_balance = db.Column(db.Float, nullable=False)
    interest_rate = db.Column(db.Float, default=0.0)
    payoff_date = db.Column(db.Date)  # target payoff date
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    active = db.Column(db.Boolean, default=True)

class DebtPayment(db.Model):
    """Record of debt payment"""
    id = db.Column(db.Integer, primary_key=True)
    debt_id = db.Column(db.Integer, db.ForeignKey('debt_account.id'), nullable=False)
    payment_date = db.Column(db.Date, nullable=False)
    amount_paid = db.Column(db.Float, nullable=False)
    principal = db.Column(db.Float)  # portion towards principal
    interest = db.Column(db.Float)  # portion towards interest
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class FinancialGoal(db.Model):
    """Track financial goals (savings, investment targets, etc)"""
    id = db.Column(db.Integer, primary_key=True)
    goal_name = db.Column(db.String(256), nullable=False)
    description = db.Column(db.Text)
    target_amount = db.Column(db.Float, nullable=False)
    current_amount = db.Column(db.Float, default=0.0)
    target_date = db.Column(db.Date)
    category = db.Column(db.String(64))  # vacation, emergency fund, car, house, etc
    priority = db.Column(db.Integer, default=0)  # 1=high, 2=medium, 3=low
    created_by = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    active = db.Column(db.Boolean, default=True)

class GoalContribution(db.Model):
    """Track contributions towards a financial goal"""
    id = db.Column(db.Integer, primary_key=True)
    goal_id = db.Column(db.Integer, db.ForeignKey('financial_goal.id'), nullable=False)
    contribution_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    contributor = db.Column(db.String(64), nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class PlaidAccount(db.Model):
    """Linked bank account via Plaid"""
    id = db.Column(db.Integer, primary_key=True)
    user = db.Column(db.String(64), nullable=False)  # family member
    access_token = db.Column(db.String(512), nullable=False)  # encrypted Plaid access token
    item_id = db.Column(db.String(256), nullable=False, unique=True)
    account_id = db.Column(db.String(256), nullable=False)
    account_name = db.Column(db.String(256))
    account_type = db.Column(db.String(64))  # checking, savings, credit_card, etc
    account_subtype = db.Column(db.String(64))
    balance = db.Column(db.Float)
    currency = db.Column(db.String(3), default='USD')
    synced_at = db.Column(db.DateTime)
    last_transaction_id = db.Column(db.String(256))  # for incremental sync
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    active = db.Column(db.Boolean, default=True)

class PlaidTransaction(db.Model):
    """Transactions synced from Plaid"""
    id = db.Column(db.Integer, primary_key=True)
    plaid_account_id = db.Column(db.Integer, db.ForeignKey('plaid_account.id'), nullable=False)
    plaid_transaction_id = db.Column(db.String(256), unique=True, nullable=False)
    transaction_date = db.Column(db.Date, nullable=False)
    posted_date = db.Column(db.Date)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), default='USD')
    description = db.Column(db.String(512))
    merchant = db.Column(db.String(256))
    category = db.Column(db.String(64))  # Plaid category
    pending = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow)
