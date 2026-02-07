# Plaid & Family Budget Implementation - HomeHub

## Overview

Complete integration of Plaid banking API and comprehensive family budget management features into HomeHub, consolidating all financial management into a single "Expense & Money Tracker" interface.

---

## Features Implemented

### 1. Plaid Bank Account Integration

#### Account Linking & Management
- **Generate Plaid Link Token**: `/expenses/plaid/link-token` (POST)
  - Creates a secure token for Plaid Link UI integration
  - User connects their bank account securely
  
- **Exchange Token**: `/expenses/plaid/exchange-token` (POST)
  - Exchanges public token from Plaid Link for persistent access token
  - Stores encrypted access token in database
  - Supports multiple accounts per user
  
- **Fetch Accounts**: `/expenses/plaid/accounts` (GET)
  - Lists all linked bank accounts
  - Shows current balances and account types (checking, savings, etc.)
  - Real-time balance synchronization
  
- **Sync Transactions**: `/expenses/plaid/accounts/<id>/sync` (POST)
  - Fetches up to 30 days of transactions
  - Automatically categorizes transactions
  - Stores transactions locally for offline access
  
- **Disconnect Account**: `/expenses/plaid/accounts/<id>` (DELETE)
  - Safely removes bank account connection
  - Retains historical transaction data
  - Re-link anytime

---

### 2. Individual Account Analytics

#### Account Summary
- **Endpoint**: `/expenses/accounts/<account_id>/summary`
- **Shows**:
  - Current account balance
  - Monthly transaction count & totals
  - Year-to-date spending
  - Top spending categories
  - Average transaction amount

#### Transactions per Account
- **Endpoint**: `/expenses/accounts/<account_id>/transactions`
- **Features**:
  - Paginated transaction list (50 per page)
  - Filter by category, date range
  - Merchant information
  - Transaction status (pending/posted)

#### Expenses by Category
- **Endpoint**: `/expenses/accounts/<account_id>/expenses-by-category`
- **Shows**:
  - Monthly breakdown by category
  - Category totals & percentages
  - Transaction count per category
  - Individual transaction details
  - Trends and patterns

---

### 3. Family Budget Management

#### Multi-Account Summary
- **Endpoint**: `/expenses/accounts/summary`
- **Shows**:
  - All linked Plaid accounts
  - Total family wealth across accounts
  - Individual account balances
  - Last sync timestamp
  - Recent transaction counts

#### Family-Wide Budget
- **Create Budget**: `/expenses/budget/family` (POST)
  - Set monthly budget amount
  - Allocate funds across categories
  - Track budget vs actual spending
  
- **View Budget**: `/expenses/budget/family` (GET)
  - Category-by-category allocation
  - Spending per category
  - Remaining budget
  - Percentage spent per category

#### Member Spending
- **Endpoint**: `/expenses/budget/family/member/<member_name>`
- **Shows**:
  - Personal expenses for the month
  - Share of shared expenses
  - Spending by category
  - Combined personal + shared totals
  - Easy member-to-member comparison

---

### 4. Advanced Budget Features

#### Budget Recommendations
- **Endpoint**: `/expenses/budget/recommendations`
- **Analysis**:
  - Last 3 months of spending data
  - Category-by-category averages
  - Recommended monthly budgets (with 10% buffer)
  - Confidence levels for recommendations
  - Transaction frequency per category

#### Analytics Summary
- **Endpoint**: `/expenses/analytics/summary`
- **Comprehensive View**:
  - Monthly budget vs actual
  - Debt summary (active debts, total balance)
  - Financial goals (target, current, progress)
  - Individual vs shared transactions
  - Period overview

---

## Database Schema Extensions

### PlaidAccount Model
```
- id (PK)
- user (family member identifier)
- access_token (encrypted)
- item_id (Plaid Item ID)
- account_id (Plaid Account ID)
- account_name (e.g., "Chase Checking")
- account_type (e.g., "depository", "credit")
- account_subtype (e.g., "checking", "savings")
- balance (current balance)
- currency (ISO code)
- active (status)
- synced_at (last sync timestamp)
```

### PlaidTransaction Model
```
- id (PK)
- plaid_account_id (FK)
- plaid_transaction_id (Plaid's ID)
- transaction_date
- posted_date
- amount
- currency
- description
- merchant
- category (auto-categorized by Plaid)
- pending (status)
- created_at
```

### FamilyBudget Model
```
- id (PK)
- budget_name
- month (current period)
- year
- total_amount (monthly limit)
- spent (total spent this month)
- currency
- created_by (administrator)
- notes
- created_at
- updated_at
```

### BudgetCategory Model
```
- id (PK)
- budget_id (FK)
- category_name
- allocated_amount (budgeted)
- spent_amount (actual spending)
- updated_at
```

### SharedExpense Model
```
- id (PK)
- description
- total_amount
- paid_by (family member)
- date
- category
- participants (JSON array with names & amounts)
- settled (boolean)
- created_at
- updated_at
```

### DebtAccount Model
```
- id (PK)
- description
- debtor
- creditor
- initial_amount
- current_balance
- interest_rate
- payoff_date
- active
- created_at
```

---

## API Response Examples

### Account Summary
```json
{
  "account": {
    "id": 1,
    "name": "Chase Checking",
    "type": "depository",
    "balance": 5234.56,
    "currency": "USD"
  },
  "period": {
    "month": {
      "total_transactions": 24,
      "total_spent": 1856.43,
      "average_transaction": 77.35
    },
    "year": {
      "total_transactions": 287,
      "total_spent": 22145.78,
      "average_transaction": 77.21
    }
  },
  "top_categories": [
    {
      "category": "Food and Drink",
      "count": 12,
      "total": 485.23,
      "average": 40.43
    },
    {
      "category": "Transportation",
      "count": 8,
      "total": 312.50,
      "average": 39.06
    }
  ]
}
```

### Expenses by Category
```json
{
  "account": {
    "id": 1,
    "name": "Chase Checking",
    "month": "2026-02"
  },
  "categories": [
    {
      "name": "Food and Drink",
      "count": 12,
      "total": 485.23,
      "average_per_transaction": 40.43,
      "percentage": 26.15
    }
  ],
  "total_spent": 1856.43
}
```

### Member Spending
```json
{
  "member": "John",
  "period": "2026-02",
  "personal_spending": {
    "total": 450.32,
    "count": 8,
    "by_category": {
      "Food and Drink": 180.00,
      "Entertainment": 120.00,
      "Transportation": 150.32
    }
  },
  "shared_spending": {
    "total": 285.50
  },
  "combined_total": 735.82
}
```

### Budget Recommendations
```json
{
  "period": "3 months",
  "total_spent": 5245.67,
  "total_recommendations": 5770.24,
  "recommendations": [
    {
      "category": "Food and Drink",
      "monthly_average": 456.78,
      "recommended_budget": 502.45,
      "transaction_count": 42,
      "confidence": "high"
    }
  ]
}
```

---

## Security Features

### Token Encryption
- Plaid access tokens are encrypted before storage
- Decrypted only when needed for API calls
- Regular token refresh supported

### Authentication
- All state-changing operations require authentication
- Session-based auth via Flask
- Password-protected endpoints with `@_require_auth()` decorator

### Data Privacy
- Transaction data stored locally (not transmitted elsewhere)
- Account numbers masked
- Sensitive balance information only shown to authorized users
- Family members can have different permission levels

---

## Configuration

### Environment Variables
```
PLAID_CLIENT_ID=your_client_id
PLAID_SECRET=your_secret
PLAID_ENV=sandbox|development|production
```

### Feature Toggle
```yaml
# config.yml
expense_tracker: true  # Enables expense & money tracker
```

---

## Usage Flow

### For Users

1. **Link Bank Account**
   - Navigate to Expense & Money Tracker
   - Click "Connect Bank Account" 
   - Complete Plaid Link flow
   - Select accounts to connect

2. **View Spending**
   - See per-account spending breakdown
   - View all transactions by category
   - Access personalized spending insights

3. **Family Budget**
   - Admin sets monthly family budget
   - Allocates funds across categories
   - Family members track their spending
   - View shared expenses
   - Compare member-to-member spending

4. **Debt & Goals**
   - Record debt accounts
   - Track debt payoff progress
   - Set financial goals
   - Track contributions toward goals

---

## Integration with Existing Features

### Expense Tracking
- Manual expenses combined with Plaid transactions
- All filtered by same category system
- Unified reporting across both sources

### Recurring Expenses
- Support for recurring bills
- Auto-generation of expense entries
- Family-wide visibility

### Shared Expenses
- Split bills among family members
- Track who owes whom
- Settlement tracking

---

## API Endpoints Summary

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/expenses/plaid/link-token` | Create Plaid link token |
| POST | `/expenses/plaid/exchange-token` | Exchange token for access |
| GET | `/expenses/plaid/accounts` | List linked accounts |
| POST | `/expenses/plaid/accounts/<id>/sync` | Sync transactions |
| DELETE | `/expenses/plaid/accounts/<id>` | Disconnect account |
| GET | `/expenses/accounts/summary` | All accounts summary |
| GET | `/expenses/accounts/<id>/summary` | Single account summary |
| GET | `/expenses/accounts/<id>/transactions` | Account transactions |
| GET | `/expenses/accounts/<id>/expenses-by-category` | Category breakdown |
| GET | `/expenses/budget/family` | View family budget |
| POST | `/expenses/budget/family` | Create family budget |
| GET | `/expenses/budget/family/member/<name>` | Member spending |
| GET | `/expenses/budget/recommendations` | Smart recommendations |
| GET | `/expenses/analytics/summary` | Comprehensive analytics |

---

## Next Steps

1. **Frontend Integration**
   - Create Expense & Money Tracker dashboard with tabs
   - Implement Plaid Link UI component
   - Build per-account transaction views
   - Design family budget allocation interface

2. **Notifications**
   - Alert when spending approaches budget limit
   - Remind about debt payments
   - Celebrate goal milestones

3. **Mobile Support**
   - Mobile-responsive account views
   - Quick transaction categorization
   - Mobile-friendly budget tracking

4. **Reporting**
   - Monthly spending reports (PDF)
   - Year-over-year comparisons
   - Category trend analysis
   - Family spending summaries

---

## Testing

### Test Endpoints
```bash
# Link account
curl -X POST http://localhost:5000/expenses/plaid/link-token \
  -H "Content-Type: application/json" \
  -d '{"user": "john"}'

# Get all accounts
curl http://localhost:5000/expenses/accounts/summary

# Get account transactions
curl "http://localhost:5000/expenses/accounts/1/transactions?page=1&per_page=20"

# Create budget
curl -X POST http://localhost:5000/expenses/budget/family \
  -H "Content-Type: application/json" \
  -d '{"total_amount": 5000, "month": 2, "year": 2026}'
```

---

## Notes

- Plaid features disabled if credentials not configured (graceful degradation)
- All timestamps in UTC
- Currency defaults to USD (configurable per account)
- Family budgets are monthly, rolling forward each month
- Shared expenses tracked separately for fairness
- Debt tracking supports flexible payoff strategies
