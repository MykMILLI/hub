# Money Management System - Developer Guide

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Frontend (Templates)                 │
│  money.html - Dashboard UI with Material 3 Design       │
└────────────────────┬────────────────────────────────────┘
                     │ HTTP/JSON
┌────────────────────▼────────────────────────────────────┐
│              Blueprint (app/blueprints/money.py)         │
│  ├── Routes (40+ endpoints)                             │
│  ├── Request handling                                    │
│  ├── Data validation                                     │
│  └── Response formatting                                │
└────────────────────┬────────────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        │            │            │
┌───────▼──┐  ┌──────▼──┐  ┌─────▼──────┐
│ Plaid    │  │Database │  │  Security  │
│ Service  │  │ Models  │  │  Utilities │
└──────────┘  └─────────┘  └────────────┘
```

## File Structure

```
app/
├── models.py                    # SQLAlchemy models (11 new tables)
├── plaid_service.py             # Plaid API wrapper
├── blueprints/
│   ├── money.py                 # Money management routes
│   └── __init__.py              # Blueprint registration
├── __init__.py                  # App factory with Plaid init
├── security.py                  # Existing auth utilities
└── utils.py                     # Existing sanitization
    
templates/
└── money.html                   # Dashboard UI (450+ lines)

Documentation/
├── MONEY_MANAGEMENT_GUIDE.md        # Full API reference
├── MONEY_ENHANCEMENT_SUMMARY.md     # Technical summary
├── MONEY_FEATURES_SHOWCASE.md       # Feature overview
└── MONEY_MANAGEMENT_QUICKSTART.md   # User guide
```

## Core Classes & Methods

### PlaidService (`app/plaid_service.py`)

**Class: `PlaidService`**

```python
class PlaidService:
    """Handles all Plaid API interactions"""
    
    # Initialization
    init_app(app)                    # Set up with Flask config
    is_configured()                  # Check if API creds exist
    
    # OAuth & Account Linking
    create_link_token(user_id, redirect_uri)
    exchange_public_token(public_token, user)
    
    # Data Syncing
    sync_transactions(plaid_account_id, days_back=30)
    sync_balances(plaid_account_id)
    
    # Account Management
    disconnect_account(plaid_account_id)
    get_account_transactions(plaid_account_id, filters...)
```

**Error Handling:**
- All methods raise exceptions on Plaid API errors
- Errors logged with detailed context
- Database rolled back on transaction failure
- Graceful fallback if Plaid not configured

**Usage Example:**
```python
from app.plaid_service import plaid_service

# Create link token for user
token = plaid_service.create_link_token(user_id="alice")

# Sync transactions after linking
count = plaid_service.sync_transactions(account_id=1)

# Get recent transactions
txns = plaid_service.get_account_transactions(
    account_id=1,
    start_date=date.today() - timedelta(days=30)
)
```

### Money Blueprint (`app/blueprints/money.py`)

**Route Groups:**

**1. Budget Routes**
```python
GET    /money/budgets                    # List paginated
POST   /money/budgets                    # Create
GET    /money/budgets/<id>               # Get details
PUT    /money/budgets/<id>               # Update
POST   /money/budgets/<id>/categories    # Add category
PUT    /money/budgets/<id>/categories/<cat_id>
DELETE /money/budgets/<id>/categories/<cat_id>
```

**2. Shared Expense Routes**
```python
GET    /money/shared-expenses            # List with filters
POST   /money/shared-expenses            # Create
PUT    /money/shared-expenses/<id>       # Update
DELETE /money/shared-expenses/<id>       # Delete
```

**3. Debt Routes**
```python
GET    /money/debts                      # List
POST   /money/debts                      # Create
POST   /money/debts/<id>/payments        # Record payment
DELETE /money/debts/<id>                 # Delete
```

**4. Goal Routes**
```python
GET    /money/goals                      # List
POST   /money/goals                      # Create
POST   /money/goals/<id>/contributions   # Add contribution
```

**5. Plaid Routes**
```python
POST   /money/plaid/link-token           # Get link token
POST   /money/plaid/exchange-token       # Link account
GET    /money/plaid/accounts             # List accounts
POST   /money/plaid/accounts/<id>/sync   # Sync data
GET    /money/plaid/accounts/<id>/transactions
DELETE /money/plaid/accounts/<id>        # Disconnect
```

**6. Analytics Routes**
```python
GET    /money/analytics/summary          # Dashboard data
```

### Database Models

All models in `app/models.py`:

**FamilyBudget**
```python
class FamilyBudget(db.Model):
    id, budget_name, month, year, total_amount
    currency, created_by, created_at, updated_at, notes
    categories = relationship('BudgetCategory')
```

**SharedExpense**
```python
class SharedExpense(db.Model):
    id, description, total_amount, paid_by, date
    category, participants (JSON), settled
    created_at, updated_at
```

**DebtAccount**
```python
class DebtAccount(db.Model):
    id, description, debtor, creditor
    initial_amount, current_balance, interest_rate
    payoff_date, active, created_at, updated_at
    payments = relationship('DebtPayment')
```

**FinancialGoal**
```python
class FinancialGoal(db.Model):
    id, goal_name, description, target_amount
    current_amount, target_date, category, priority
    created_by, active, created_at, updated_at
    contributions = relationship('GoalContribution')
```

**PlaidAccount**
```python
class PlaidAccount(db.Model):
    id, user, access_token, item_id, account_id
    account_name, account_type, account_subtype
    balance, currency, synced_at, active
    created_at, updated_at
    transactions = relationship('PlaidTransaction')
```

## Request/Response Patterns

### List Endpoint Pattern
```python
GET /money/budgets?page=1&per_page=20

Response (200):
{
    "budgets": [...],
    "total": 5,
    "pages": 1
}

# With filters:
GET /money/shared-expenses?month=7&year=2024&settled=false
```

### Create Endpoint Pattern
```python
POST /money/budgets
{
    "name": "July Budget",
    "month": 7,
    "year": 2024,
    "total_amount": 5000,
    "currency": "USD",
    "created_by": "Mom"
}

Response (201):
{
    "id": 1,
    "message": "Budget created successfully"
}

Error (400/409):
{
    "error": "Budget already exists for this month"
}
```

### Update Endpoint Pattern
```python
PUT /money/budgets/1
{
    "name": "Updated Budget",
    "total_amount": 5500
}

Response (200):
{
    "message": "Budget updated"
}
```

### Delete Endpoint Pattern
```python
DELETE /money/budgets/1/categories/5

Response (200):
{
    "message": "Category deleted"
}

Error (404):
{
    "404 Not Found": "..."
}
```

## Adding New Features

### Example 1: Add "Budget Rollover" Feature

**Step 1: Update Model**
```python
class FamilyBudget(db.Model):
    # ... existing fields ...
    rollover_amount = db.Column(db.Float, default=0.0)
    previous_budget_id = db.Column(db.Integer)  # Reference
```

**Step 2: Add Migration**
```python
# In app/__init__.py migration section
ensure_column('family_budget', 'rollover_amount', 'REAL', 0.0)
```

**Step 3: Add API Endpoint**
```python
@money_bp.route('/budgets/<int:budget_id>/rollover', methods=['POST'])
@require_password
def create_rollover_budget(budget_id):
    source_budget = FamilyBudget.query.get_or_404(budget_id)
    remaining = source_budget.total_amount - get_spent(source_budget)
    
    next_budget = FamilyBudget(
        budget_name=source_budget.budget_name,
        month=(source_budget.month % 12) + 1,
        year=source_budget.year + (1 if source_budget.month == 12 else 0),
        total_amount=source_budget.total_amount + remaining,
        rollover_amount=remaining,
        previous_budget_id=source_budget.id
    )
    db.session.add(next_budget)
    db.session.commit()
    return jsonify({'id': next_budget.id})
```

**Step 4: Update UI**
```html
<!-- In templates/money.html -->
<button onclick="rolloverBudget(budgetId)" class="btn btn-secondary">
  Rollover to Next Month
</button>
```

### Example 2: Add "Monthly Report" Export

**Step 1: Create Export Function**
```python
def export_monthly_report(month, year, format='json'):
    """Export complete monthly financial report"""
    budget = FamilyBudget.query.filter_by(month=month, year=year).first()
    expenses = SharedExpense.query.filter(...)
    debts = DebtAccount.query.filter(...)
    
    report = {
        'month': month,
        'year': year,
        'budget': {...},
        'expenses': [...],
        'debts': [...],
    }
    
    if format == 'csv':
        # Convert to CSV
        return csv_string
    elif format == 'pdf':
        # Generate PDF
        return pdf_bytes
    return json.dumps(report)
```

**Step 2: Add Route**
```python
@money_bp.route('/reports/monthly', methods=['GET'])
def export_monthly_report():
    month = request.args.get('month', type=int)
    year = request.args.get('year', type=int)
    fmt = request.args.get('format', 'json')
    
    data = export_monthly_report(month, year, fmt)
    
    if fmt == 'csv':
        return data, 200, {'Content-Type': 'text/csv'}
    elif fmt == 'pdf':
        return data, 200, {'Content-Type': 'application/pdf'}
    return jsonify(data)
```

## Testing Guide

### Unit Tests
```python
def test_create_budget():
    data = {
        'name': 'Test Budget',
        'month': 7,
        'year': 2024,
        'total_amount': 5000
    }
    response = client.post('/money/budgets', json=data)
    assert response.status_code == 201
    assert 'id' in response.json

def test_split_expense():
    data = {
        'description': 'Test Expense',
        'total_amount': 100,
        'paid_by': 'Alice',
        'participants': [
            {'name': 'Alice', 'amount': 50},
            {'name': 'Bob', 'amount': 50}
        ]
    }
    response = client.post('/money/shared-expenses', json=data)
    assert response.status_code == 201
```

### Integration Tests
```python
def test_budget_spending_flow():
    # Create budget
    budget = FamilyBudget(...)
    db.session.add(budget)
    
    # Add category
    category = BudgetCategory(budget_id=budget.id, ...)
    db.session.add(category)
    db.session.commit()
    
    # Create expense
    expense = ExpenseEntry(...)
    db.session.add(expense)
    db.session.commit()
    
    # Verify calculation
    spent = sum(e.amount for e in expense_query)
    assert spent == category.spent_amount
```

## Performance Optimization

### Database Indexing
```python
class FamilyBudget(db.Model):
    __table_args__ = (
        Index('idx_budget_month_year', 'month', 'year'),
        Index('idx_budget_user', 'created_by'),
    )
```

### Query Optimization
```python
# ❌ Bad: N+1 problem
budgets = FamilyBudget.query.all()
for budget in budgets:
    categories = budget.categories  # Query per budget

# ✅ Good: Eager loading
budgets = FamilyBudget.query.options(
    joinedload(FamilyBudget.categories)
).all()
```

### Caching Strategy
```python
from flask_caching import Cache

cache = Cache(config={'CACHE_TYPE': 'simple'})

@money_bp.route('/analytics/summary')
@cache.cached(timeout=300)  # Cache 5 minutes
def get_analytics_summary():
    # ... calculation ...
```

## Common Patterns

### List with Filtering
```python
@money_bp.route('/expenses', methods=['GET'])
def list_expenses():
    month = request.args.get('month', type=int)
    year = request.args.get('year', type=int)
    settled = request.args.get('settled', type=bool)
    
    query = SharedExpense.query
    
    if month and year:
        query = query.filter(
            func.month(SharedExpense.date) == month,
            func.year(SharedExpense.date) == year
        )
    
    if settled is not None:
        query = query.filter_by(settled=settled)
    
    return jsonify([...])
```

### Cascading Updates
```python
@money_bp.route('/budgets/<int:budget_id>', methods=['PUT'])
@require_password
def update_budget(budget_id):
    budget = FamilyBudget.query.get_or_404(budget_id)
    data = request.get_json()
    
    # Update parent
    budget.total_amount = data.get('total_amount', budget.total_amount)
    
    # Recalculate children
    total_allocated = sum(c.allocated_amount for c in budget.categories)
    if total_allocated > budget.total_amount:
        # Adjust or warn
        pass
    
    db.session.commit()
```

### Error Handling
```python
@money_bp.route('/debts', methods=['POST'])
@require_password
def create_debt():
    try:
        # Validation
        if not data.get('description'):
            return jsonify({'error': 'Missing description'}), 400
        
        # Business logic
        debt = DebtAccount(...)
        db.session.add(debt)
        db.session.commit()
        
        return jsonify({'id': debt.id}), 201
        
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'Database integrity error'}), 409
    except Exception as e:
        logger.error(f"Error creating debt: {e}")
        return jsonify({'error': 'Internal server error'}), 500
```

## Debugging Tips

### Enable Logging
```python
import logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
```

### Database Inspection
```python
# In Flask shell
from app.models import FamilyBudget
budgets = FamilyBudget.query.all()
for b in budgets:
    print(f"{b.budget_name}: {b.total_amount}")
```

### API Testing
```bash
# Create budget
curl -X POST http://localhost:5000/money/budgets \
  -H "Content-Type: application/json" \
  -d '{"name":"Test","month":7,"year":2024,"total_amount":5000}'

# Get list
curl http://localhost:5000/money/budgets

# Get details
curl http://localhost:5000/money/budgets/1

# Update
curl -X PUT http://localhost:5000/money/budgets/1 \
  -H "Content-Type: application/json" \
  -d '{"name":"Updated"}'

# Delete
curl -X DELETE http://localhost:5000/money/budgets/1
```

## Contributing Guidelines

1. **Code Style** - Follow existing patterns in money.py
2. **Docstrings** - Document functions with examples
3. **Error Handling** - Use try/except with proper logging
4. **Testing** - Add tests for new features
5. **Documentation** - Update relevant MD files
6. **Security** - Always sanitize user input
7. **Performance** - Avoid N+1 queries

---

**Happy Developing! 🚀**
