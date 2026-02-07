# Money Management System - Setup & API Guide

## Overview

HomeHub now includes a comprehensive **Family Money Management System** combining the best features from both HomeHub and Family Hub:

- ✅ **Family Budgets** - Monthly budgets with category allocation and spending tracking
- ✅ **Shared Expenses** - Split expenses among family members with automatic settlement calculations
- ✅ **Debt Tracking** - Track personal debts within the family with payment history and interest calculation
- ✅ **Financial Goals** - Set savings goals and track contributions toward them
- ✅ **Plaid Integration** - Link bank accounts, sync transactions, and track balances automatically
- ✅ **Analytics** - Comprehensive financial dashboard with spending analytics and goal progress

## Installation & Configuration

### 1. Install Dependencies

The required packages have been added to `requirements.txt`:

```bash
pip install -r requirements.txt
```

Key packages:
- `plaid-python` - Official Plaid API client
- `cryptography` - For secure token storage
- `python-dateutil` - Date handling utilities

### 2. Configure Plaid (Optional)

To enable Plaid bank account integration, you'll need Plaid API credentials:

1. Create a Plaid account at https://plaid.com
2. Get your Client ID and Secret from the dashboard
3. Set environment variables or add to your Flask config:

```bash
# Via environment variables
export PLAID_CLIENT_ID="your_client_id"
export PLAID_SECRET="your_secret"
export PLAID_ENV="sandbox"  # or "development" or "production"
```

Or in your `config.yml`:

```yaml
plaid:
  client_id: "your_client_id"
  secret: "your_secret"
  env: "sandbox"
```

If Plaid is not configured, the system will work with all other features (budgets, shared expenses, debts, goals).

### 3. Database Setup

The system automatically creates all necessary tables on first run. The database includes:

**Budget Tables:**
- `family_budget` - Monthly family budgets
- `budget_category` - Category allocations within budgets

**Expense Tables:**
- `shared_expense` - Expenses split among family members

**Debt Tables:**
- `debt_account` - Debt records
- `debt_payment` - Payment history

**Goal Tables:**
- `financial_goal` - Savings goals
- `goal_contribution` - Contributions toward goals

**Plaid Tables:**
- `plaid_account` - Linked bank accounts
- `plaid_transaction` - Synced transactions

## API Reference

All money management endpoints are under `/money/` prefix and require password authentication.

### Family Budgets API

#### List Budgets
```
GET /money/budgets?page=1
```

Response:
```json
{
  "budgets": [
    {
      "id": 1,
      "name": "July Budget",
      "month": 7,
      "year": 2024,
      "total": 5000.0,
      "spent": 3200.5,
      "currency": "USD",
      "is_current": true
    }
  ],
  "total": 5,
  "pages": 1
}
```

#### Create Budget
```
POST /money/budgets
Content-Type: application/json

{
  "name": "August Budget",
  "month": 8,
  "year": 2024,
  "total_amount": 5000,
  "currency": "USD",
  "created_by": "Mom"
}
```

#### Get Budget Details
```
GET /money/budgets/<budget_id>
```

Includes all categories and spending breakdown.

#### Update Budget
```
PUT /money/budgets/<budget_id>
Content-Type: application/json

{
  "name": "Updated Budget Name",
  "total_amount": 5500,
  "notes": "Adjusted for summer expenses"
}
```

#### Add/Update Budget Category
```
POST /money/budgets/<budget_id>/categories
Content-Type: application/json

{
  "category_name": "Groceries",
  "allocated_amount": 800
}
```

#### Update Category
```
PUT /money/budgets/<budget_id>/categories/<category_id>
Content-Type: application/json

{
  "allocated_amount": 850
}
```

#### Delete Category
```
DELETE /money/budgets/<budget_id>/categories/<category_id>
```

### Shared Expenses API

#### List Shared Expenses
```
GET /money/shared-expenses?month=7&year=2024&settled=false
```

Response:
```json
{
  "expenses": [
    {
      "id": 1,
      "description": "Costco Groceries",
      "total_amount": 150.00,
      "paid_by": "Mom",
      "date": "2024-07-15",
      "category": "Groceries",
      "participants": [
        {"name": "Mom", "amount": 75.00},
        {"name": "Dad", "amount": 75.00}
      ],
      "settled": false
    }
  ]
}
```

#### Create Shared Expense
```
POST /money/shared-expenses
Content-Type: application/json

{
  "description": "Restaurant Dinner",
  "total_amount": 120.00,
  "paid_by": "Dad",
  "date": "2024-07-20",
  "category": "Dining",
  "participants": [
    {"name": "Dad", "amount": 40.00},
    {"name": "Mom", "amount": 40.00},
    {"name": "Alice", "amount": 40.00}
  ]
}
```

#### Mark as Settled
```
PUT /money/shared-expenses/<expense_id>
Content-Type: application/json

{
  "settled": true
}
```

#### Delete Expense
```
DELETE /money/shared-expenses/<expense_id>
```

### Debt Tracking API

#### List Debts
```
GET /money/debts?active=true
```

Response:
```json
{
  "debts": [
    {
      "id": 1,
      "description": "Car Loan",
      "debtor": "Alice",
      "creditor": "Bank",
      "initial_amount": 20000.00,
      "current_balance": 15000.00,
      "interest_rate": 3.5,
      "payoff_date": "2026-07-20",
      "active": true,
      "progress": 25.0
    }
  ]
}
```

#### Create Debt
```
POST /money/debts
Content-Type: application/json

{
  "description": "Student Loan",
  "debtor": "Bob",
  "creditor": "University",
  "initial_amount": 30000,
  "interest_rate": 2.5,
  "payoff_date": "2030-06-01"
}
```

#### Record Debt Payment
```
POST /money/debts/<debt_id>/payments
Content-Type: application/json

{
  "payment_date": "2024-07-20",
  "amount_paid": 500,
  "principal": 480,
  "interest": 20,
  "notes": "Monthly payment"
}
```

#### Delete Debt
```
DELETE /money/debts/<debt_id>
```

### Financial Goals API

#### List Goals
```
GET /money/goals?active=true
```

Response:
```json
{
  "goals": [
    {
      "id": 1,
      "name": "Summer Vacation",
      "description": "Family trip to Hawaii",
      "target_amount": 5000.00,
      "current_amount": 2500.00,
      "target_date": "2024-08-01",
      "category": "Travel",
      "priority": 1,
      "created_by": "Mom",
      "active": true,
      "progress": 50.0
    }
  ]
}
```

#### Create Goal
```
POST /money/goals
Content-Type: application/json

{
  "name": "Emergency Fund",
  "description": "3 months of living expenses",
  "target_amount": 15000,
  "category": "Emergency",
  "priority": 1,
  "target_date": "2025-12-31",
  "created_by": "Mom"
}
```

#### Add Contribution
```
POST /money/goals/<goal_id>/contributions
Content-Type: application/json

{
  "contribution_date": "2024-07-20",
  "amount": 500,
  "contributor": "Dad",
  "notes": "Monthly contribution"
}
```

### Plaid Integration API

#### Create Link Token
```
POST /money/plaid/link-token
Content-Type: application/json

{
  "user": "Alice",
  "redirect_uri": "https://yourdomain.com/money"
}
```

Response:
```json
{
  "link_token": "link-sandbox-abc123xyz"
}
```

Use this token to initialize Plaid Link in frontend.

#### Exchange Public Token
```
POST /money/plaid/exchange-token
Content-Type: application/json

{
  "public_token": "public-sandbox-abc123xyz",
  "user": "Alice"
}
```

Response:
```json
{
  "message": "Successfully linked 2 account(s)",
  "accounts": [
    {
      "id": 1,
      "name": "Checking Account",
      "type": "depository",
      "balance": 5000.00
    }
  ]
}
```

#### List Plaid Accounts
```
GET /money/plaid/accounts
```

#### Sync Account Transactions
```
POST /money/plaid/accounts/<account_id>/sync
```

Syncs transactions from the last 30 days by default.

#### Get Account Transactions
```
GET /money/plaid/accounts/<account_id>/transactions?days=30
```

#### Disconnect Account
```
DELETE /money/plaid/accounts/<account_id>
```

### Analytics API

#### Get Financial Summary
```
GET /money/analytics/summary
```

Response:
```json
{
  "period": "7/2024",
  "budget": {
    "total": 5000.0,
    "spent": 3200.5,
    "remaining": 1799.5
  },
  "debts": {
    "active_count": 2,
    "total_balance": 35000.0
  },
  "goals": {
    "count": 3,
    "target_total": 25000.0,
    "current_total": 12500.0,
    "progress": 50.0
  },
  "transactions": {
    "individual": 45,
    "shared": 12
  }
}
```

## Frontend Usage

### Dashboard

Visit `/money` to access the money management dashboard with:

1. **Analytics Summary** - Budget status, debt total, goal progress, month spending
2. **Budget Management** - Create/edit monthly budgets with category allocation
3. **Shared Expenses** - Split and track shared expenses among family
4. **Debt Tracking** - Monitor personal debts and payment history
5. **Financial Goals** - Set and track savings goals
6. **Bank Account Linking** - Connect accounts via Plaid Link

### Plaid Link Setup (Frontend)

Example HTML/JavaScript for Plaid Link integration:

```html
<button id="linkButton">Link Bank Account</button>

<script src="https://cdn.plaid.com/link/v3/stable/link-initialize.js"></script>

<script>
const handler = Plaid.create({
  token: linkToken,  // Get from /money/plaid/link-token endpoint
  onSuccess: (publicToken, metadata) => {
    // Send publicToken to backend
    fetch('/money/plaid/exchange-token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        public_token: publicToken,
        user: 'Alice'
      })
    });
  },
  onExit: (err, metadata) => {
    if (err !== null) console.log('Plaid error:', err);
  }
});

document.getElementById('linkButton').onclick = () => {
  handler.open();
};
</script>
```

## Security Considerations

1. **Password Protection** - All money endpoints require admin password authentication
2. **Token Encryption** - Plaid access tokens are stored securely in the database
3. **XSS Prevention** - All user input is sanitized before storage
4. **CSRF Protection** - Endpoints use standard Flask security practices
5. **No Client-side Secrets** - Plaid credentials are never exposed to frontend

## Database Relationships

```
FamilyBudget
  └── BudgetCategory (multiple)
  
SharedExpense
  └── participants (JSON list)

DebtAccount
  └── DebtPayment (multiple)

FinancialGoal
  └── GoalContribution (multiple)

PlaidAccount
  └── PlaidTransaction (multiple)
```

## Troubleshooting

### Plaid Integration Not Working

1. Check that `plaid-python` is installed: `pip install plaid-python`
2. Verify credentials in environment variables or config.yml
3. Check Plaid dashboard for API calls and errors
4. Try sandbox environment first before production

### Budget Spending Not Updating

1. Ensure `ExpenseEntry` records have matching category names
2. Budget category spending is calculated from individual `ExpenseEntry` records
3. Check that expense dates fall within the budget month/year

### Performance Issues

For large transaction histories:
1. Plaid sync limits to 250 transactions per request
2. Use pagination for budget and expense lists
3. Archive old goals to keep queries efficient

## Future Enhancements

Planned features:
- 📊 Advanced analytics with charts and trends
- 💳 Credit card statements and reconciliation
- 🔄 Automatic recurring expense-to-budget matching
- 📧 Email notifications for budget alerts
- 🎯 Multi-currency support
- 📱 Mobile app synchronization
- 🔐 Biometric security for sensitive operations
