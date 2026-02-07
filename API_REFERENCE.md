# Complete API Reference - Plaid & Family Budget System

## Overview
Comprehensive REST API reference for the Plaid bank integration and family budget management system.

---

## 🔐 Authentication

All endpoints that modify data require authentication. Add to requests:
```
Session cookie (automatically handled) or
Authorization header: Authorization: Bearer <token> (if implemented)
```

---

## 📋 API Endpoints

### PLAID ACCOUNT MANAGEMENT

#### 1. Create Plaid Link Token
```
POST /expenses/plaid/link-token

Request:
{
  "user": "string (required)",
  "redirect_uri": "string (optional)"
}

Response (200 OK):
{
  "link_token": "link-sandbox-abc123...",
  "expiration": "2026-02-06T01:15:00Z"
}
```

#### 2. Exchange Public Token
```
POST /expenses/plaid/exchange-token

Request:
{
  "user": "string (required)",
  "publicToken": "string (required)"
}

Response (201 Created):
{
  "success": true,
  "message": "Bank account linked successfully",
  "accountId": 1,
  "accounts": 3
}
```

#### 3. List Linked Accounts
```
GET /expenses/plaid/accounts

Response (200 OK):
{
  "accounts": [
    {
      "id": 1,
      "user": "john",
      "account_name": "Chase Checking",
      "account_type": "depository",
      "balance": 5234.56,
      "currency": "USD",
      "synced_at": "2026-02-06T00:15:00Z"
    }
  ]
}
```

#### 4. Sync Account Transactions
```
POST /expenses/plaid/accounts/<account_id>/sync

Request: {} (empty body)

Response (200 OK):
{
  "message": "Sync completed",
  "transactions_synced": 24,
  "balance": 5234.56
}
```

#### 5. Disconnect Account
```
DELETE /expenses/plaid/accounts/<account_id>

Response (200 OK):
{
  "message": "Account disconnected"
}
```

---

### ACCOUNT ANALYTICS

#### 1. Get All Accounts Summary
```
GET /expenses/accounts/summary

Response (200 OK):
{
  "total_accounts": 3,
  "total_balance": 15645.32,
  "accounts": [
    {
      "id": 1,
      "name": "Chase Checking",
      "type": "depository",
      "balance": 5234.56,
      "currency": "USD",
      "mask": "1234",
      "recent_transactions": 5,
      "synced_at": "2026-02-06T00:15:00Z"
    }
  ]
}
```

#### 2. Get Single Account Summary
```
GET /expenses/accounts/<account_id>/summary

Response (200 OK):
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
    }
  ]
}
```

#### 3. Get Account Transactions
```
GET /expenses/accounts/<account_id>/transactions?page=1&per_page=50&category=Food&start_date=2026-02-01&end_date=2026-02-28

Response (200 OK):
{
  "account": {
    "id": 1,
    "name": "Chase Checking"
  },
  "transactions": [
    {
      "id": 1,
      "date": "2026-02-05",
      "amount": 45.32,
      "description": "Starbucks Coffee",
      "merchant": "Starbucks",
      "category": "Food and Drink",
      "pending": false
    }
  ],
  "total": 245,
  "pages": 5,
  "current_page": 1
}
```

#### 4. Get Expenses by Category
```
GET /expenses/accounts/<account_id>/expenses-by-category

Response (200 OK):
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

---

### FAMILY BUDGET MANAGEMENT

#### 1. Create Family Budget
```
POST /expenses/budget/family

Request:
{
  "total_amount": 5000.00,
  "month": 2,
  "year": 2026
}

Response (201 Created):
{
  "id": 1,
  "message": "Family budget created"
}
```

#### 2. Get Family Budget
```
GET /expenses/budget/family

Response (200 OK):
{
  "id": 1,
  "month": 2,
  "year": 2026,
  "total_budget": 5000.00,
  "total_spent": 1856.43,
  "total_remaining": 3143.57,
  "categories": [
    {
      "id": 1,
      "name": "Food & Dining",
      "allocated": 600.00,
      "spent": 485.23,
      "remaining": 114.77,
      "percentage": 80.87
    }
  ]
}
```

#### 3. Get Member Spending
```
GET /expenses/budget/family/member/<member_name>

Response (200 OK):
{
  "member": "john",
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

#### 4. Get Budget Recommendations
```
GET /expenses/budget/recommendations

Response (200 OK):
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

### BUDGET CATEGORIES

#### 1. List All Budgets
```
GET /expenses/budgets?page=1

Response (200 OK):
{
  "budgets": [
    {
      "id": 1,
      "name": "Family Budget",
      "month": 2,
      "year": 2026,
      "total": 5000.00,
      "spent": 1856.43,
      "currency": "USD",
      "created_by": "Administrator",
      "is_current": true
    }
  ],
  "total": 12,
  "pages": 1
}
```

#### 2. Create Budget
```
POST /expenses/budgets

Request:
{
  "name": "Monthly Budget",
  "month": 2,
  "year": 2026,
  "total_amount": 5000.00,
  "currency": "USD",
  "created_by": "Administrator"
}

Response (201 Created):
{
  "id": 1,
  "message": "Budget created successfully"
}
```

#### 3. Get Budget Details
```
GET /expenses/budgets/<budget_id>

Response (200 OK):
{
  "id": 1,
  "name": "Family Budget",
  "month": 2,
  "year": 2026,
  "total": 5000.00,
  "spent": 1856.43,
  "remaining": 3143.57,
  "currency": "USD",
  "created_by": "Administrator",
  "created_at": "2026-02-01T00:00:00Z",
  "categories": [
    {
      "id": 1,
      "name": "Food & Dining",
      "allocated": 600.00,
      "spent": 485.23,
      "remaining": 114.77,
      "percentage": 80.87
    }
  ],
  "notes": "Monthly budget allocation"
}
```

#### 4. Update Budget
```
PUT /expenses/budgets/<budget_id>

Request:
{
  "name": "Updated Budget",
  "total_amount": 5500.00,
  "notes": "Updated for inflation"
}

Response (200 OK):
{
  "message": "Budget updated"
}
```

#### 5. Add Category to Budget
```
POST /expenses/budgets/<budget_id>/categories

Request:
{
  "category_name": "Food & Dining",
  "allocated_amount": 600.00
}

Response (201 Created):
{
  "id": 1,
  "message": "Category added/updated"
}
```

#### 6. Update Category
```
PUT /expenses/budgets/<budget_id>/categories/<category_id>

Request:
{
  "allocated_amount": 650.00
}

Response (200 OK):
{
  "message": "Category updated"
}
```

#### 7. Delete Category
```
DELETE /expenses/budgets/<budget_id>/categories/<category_id>

Response (200 OK):
{
  "message": "Category deleted"
}
```

---

### SHARED EXPENSES

#### 1. List Shared Expenses
```
GET /expenses/shared-expenses?month=2&year=2026&settled=false

Response (200 OK):
{
  "expenses": [
    {
      "id": 1,
      "description": "Grocery shopping",
      "total_amount": 120.50,
      "paid_by": "john",
      "date": "2026-02-05",
      "category": "groceries",
      "participants": [
        {"name": "john", "amount": 60.25},
        {"name": "jane", "amount": 60.25}
      ],
      "settled": false
    }
  ]
}
```

#### 2. Create Shared Expense
```
POST /expenses/shared-expenses

Request:
{
  "description": "Grocery shopping",
  "total_amount": 120.50,
  "paid_by": "john",
  "date": "2026-02-05",
  "category": "groceries",
  "participants": [
    {"name": "john", "amount": 60.25},
    {"name": "jane", "amount": 60.25}
  ]
}

Response (201 Created):
{
  "id": 1,
  "message": "Shared expense created"
}
```

#### 3. Update Shared Expense
```
PUT /expenses/shared-expenses/<expense_id>

Request:
{
  "description": "Costco grocery run",
  "settled": true
}

Response (200 OK):
{
  "message": "Expense updated"
}
```

#### 4. Delete Shared Expense
```
DELETE /expenses/shared-expenses/<expense_id>

Response (200 OK):
{
  "message": "Expense deleted"
}
```

---

### DEBT MANAGEMENT

#### 1. List Debts
```
GET /expenses/debts?active=true

Response (200 OK):
{
  "debts": [
    {
      "id": 1,
      "description": "Home Loan",
      "debtor": "john",
      "creditor": "Chase Bank",
      "initial_amount": 350000.00,
      "current_balance": 310000.00,
      "interest_rate": 3.5,
      "payoff_date": "2056-02-05",
      "active": true,
      "progress": 11.43
    }
  ]
}
```

#### 2. Create Debt
```
POST /expenses/debts

Request:
{
  "description": "Home Loan",
  "debtor": "john",
  "creditor": "Chase Bank",
  "initial_amount": 350000.00,
  "interest_rate": 3.5,
  "payoff_date": "2056-02-05"
}

Response (201 Created):
{
  "id": 1,
  "message": "Debt account created"
}
```

#### 3. Record Debt Payment
```
POST /expenses/debts/<debt_id>/payments

Request:
{
  "payment_date": "2026-02-05",
  "amount_paid": 2100.50,
  "principal": 1500.00,
  "interest": 600.50,
  "notes": "February payment"
}

Response (201 Created):
{
  "id": 1,
  "message": "Payment recorded"
}
```

#### 4. Delete Debt
```
DELETE /expenses/debts/<debt_id>

Response (200 OK):
{
  "message": "Debt deleted"
}
```

---

### FINANCIAL GOALS

#### 1. List Goals
```
GET /expenses/goals?active=true

Response (200 OK):
{
  "goals": [
    {
      "id": 1,
      "name": "Emergency Fund",
      "description": "6 months of expenses",
      "target_amount": 25000.00,
      "current_amount": 12500.00,
      "target_date": "2027-12-31",
      "category": "savings",
      "priority": 1,
      "created_by": "Administrator",
      "active": true,
      "progress": 50.00
    }
  ]
}
```

#### 2. Create Goal
```
POST /expenses/goals

Request:
{
  "name": "Emergency Fund",
  "description": "6 months of expenses",
  "target_amount": 25000.00,
  "target_date": "2027-12-31",
  "category": "savings",
  "priority": 1,
  "created_by": "Administrator"
}

Response (201 Created):
{
  "id": 1,
  "message": "Goal created"
}
```

#### 3. Add Contribution
```
POST /expenses/goals/<goal_id>/contributions

Request:
{
  "contribution_date": "2026-02-05",
  "amount": 500.00,
  "contributor": "john",
  "notes": "Monthly contribution"
}

Response (201 Created):
{
  "id": 1,
  "message": "Contribution recorded"
}
```

---

### ANALYTICS

#### 1. Get Comprehensive Summary
```
GET /expenses/analytics/summary

Response (200 OK):
{
  "period": "2/2026",
  "budget": {
    "total": 5000.00,
    "spent": 1856.43,
    "remaining": 3143.57
  },
  "debts": {
    "active_count": 1,
    "total_balance": 310000.00
  },
  "goals": {
    "count": 1,
    "target_total": 25000.00,
    "current_total": 12500.00,
    "progress": 50.00
  },
  "transactions": {
    "individual": 24,
    "shared": 5
  }
}
```

---

## 📊 Error Responses

### 400 Bad Request
```json
{
  "error": "Missing required field: total_amount"
}
```

### 401 Unauthorized
```json
{
  "error": "Unauthorized"
}
```

### 404 Not Found
```json
{
  "error": "Resource not found"
}
```

### 409 Conflict
```json
{
  "error": "Budget already exists for this month"
}
```

### 500 Server Error
```json
{
  "error": "Internal server error"
}
```

### 503 Service Unavailable (Plaid)
```json
{
  "error": "Plaid is not configured"
}
```

---

## 🔗 Relationship Diagram

```
PlaidAccount (1) ──→ (Many) PlaidTransaction
       ↓
    User/Member
       ↓
FamilyBudget (1) ──→ (Many) BudgetCategory
       ↓
    Family
       ↓
SharedExpense ──→ Multiple Members
       ↓
    Settlement
       ↓
DebtAccount (1) ──→ (Many) DebtPayment
       ↓
    Member
       ↓
FinancialGoal (1) ──→ (Many) GoalContribution
```

---

## 📝 Pagination

Endpoints that support pagination accept:
- `page`: Page number (default: 1)
- `per_page`: Items per page (default: 20-50 depending on endpoint)

Response includes:
```json
{
  "items": [...],
  "total": 245,
  "pages": 5,
  "current_page": 1
}
```

---

## 🔍 Filtering

Common filter parameters:
- `start_date`: YYYY-MM-DD format
- `end_date`: YYYY-MM-DD format
- `category`: Exact category name
- `month`: Month number (1-12)
- `year`: Year (e.g., 2026)
- `active`: true/false boolean

---

## 📚 Data Types

### Money
- Always in USD or specified currency
- Numbers with 2 decimal places
- Stored as floats, displayed with cents

### Dates
- ISO 8601 format: YYYY-MM-DD or ISO timestamp
- All times in UTC
- User's timezone handling done in frontend

### Categories
- Auto-determined by Plaid for Plaid transactions
- User-defined for manual expenses
- Standard categories: Food, Transportation, Entertainment, etc.

---

**Last Updated**: February 6, 2026  
**Version**: 1.0.0  
**Status**: Complete
