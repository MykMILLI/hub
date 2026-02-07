# Plaid & Family Budget - Quick Start Guide

## 🏦 Getting Started with Plaid Integration

### Step 1: Configure Plaid Credentials

Add to your `.env` file or environment variables:

```bash
PLAID_CLIENT_ID=your_plaid_client_id
PLAID_SECRET=your_plaid_secret
PLAID_ENV=sandbox  # or development, production
```

**Get Credentials**: Visit [https://plaid.com/developers](https://plaid.com/developers) to sign up and get your credentials.

### Step 2: Link a Bank Account

#### Via API
```bash
# 1. Create a link token
curl -X POST http://localhost:5000/expenses/plaid/link-token \
  -H "Content-Type: application/json" \
  -d '{"user": "john_doe"}'

# Response:
# {
#   "link_token": "link-sandbox-abc123...",
#   "expiration": "2026-02-06T01:15:00Z"
# }

# 2. Use link_token with Plaid Link UI to authenticate
# 3. Exchange the public token:
curl -X POST http://localhost:5000/expenses/plaid/exchange-token \
  -H "Content-Type: application/json" \
  -d '{
    "user": "john_doe",
    "publicToken": "public-sandbox-abc123..."
  }'
```

---

## 💰 Family Budget Setup

### Create a Monthly Budget

```bash
curl -X POST http://localhost:5000/expenses/budget/family \
  -H "Content-Type: application/json" \
  -d '{
    "total_amount": 5000,
    "month": 2,
    "year": 2026
  }'
```

### Add Budget Categories

```bash
curl -X POST http://localhost:5000/expenses/budgets/1/categories \
  -H "Content-Type: application/json" \
  -d '{
    "category_name": "Food & Dining",
    "allocated_amount": 600
  }'
```

### View Current Budget Status

```bash
curl http://localhost:5000/expenses/budget/family
```

**Response**:
```json
{
  "id": 1,
  "month": 2,
  "year": 2026,
  "total_budget": 5000,
  "total_spent": 1856.43,
  "total_remaining": 3143.57,
  "categories": [
    {
      "id": 1,
      "name": "Food & Dining",
      "allocated": 600,
      "spent": 485.23,
      "remaining": 114.77,
      "percentage": 80.87
    }
  ]
}
```

---

## 📊 View Account Spending

### See All Linked Accounts Summary

```bash
curl http://localhost:5000/expenses/accounts/summary
```

**Shows**:
- Total accounts linked
- Total wealth across accounts
- Individual account balances
- Last sync times

### View Single Account Details

```bash
curl http://localhost:5000/expenses/accounts/1/summary
```

**Shows**:
- Current balance
- This month's spending
- Year-to-date spending
- Top spending categories

### Get Account Transactions

```bash
# First 50 transactions
curl "http://localhost:5000/expenses/accounts/1/transactions?page=1&per_page=50"

# Filter by category and date
curl "http://localhost:5000/expenses/accounts/1/transactions?category=Food%20and%20Drink&start_date=2026-02-01&end_date=2026-02-28"
```

### Category Breakdown

```bash
curl http://localhost:5000/expenses/accounts/1/expenses-by-category
```

**Shows**:
- Spending by category for current month
- Percentage of total per category
- Transaction count per category
- Individual transactions per category

---

## 👥 Family Spending Comparison

### View Member Spending

```bash
curl http://localhost:5000/expenses/budget/family/member/john
```

**Shows**:
- John's personal expenses
- John's share of shared expenses
- Breakdown by category
- Combined total

---

## 🤖 Smart Budget Recommendations

```bash
curl http://localhost:5000/expenses/budget/recommendations
```

**Shows**:
- Last 3 months of spending data
- Recommended monthly budget per category
- Transaction frequency
- Confidence levels

**Response**:
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

## 📈 Comprehensive Analytics

```bash
curl http://localhost:5000/expenses/analytics/summary
```

**Includes**:
- Monthly budget vs actual
- Debt summary
- Financial goals progress
- Transaction breakdown

---

## 🔄 Sync Transactions

### Manually Sync an Account

```bash
curl -X POST http://localhost:5000/expenses/plaid/accounts/1/sync \
  -H "Content-Type: application/json" \
  -d '{}'
```

### Auto-Sync Schedule

Transactions automatically sync:
- When you view account details
- On-demand via the endpoint above
- Configurable intervals (suggested: daily)

---

## 💳 Manage Shared Expenses

### Create a Shared Expense

```bash
curl -X POST http://localhost:5000/expenses/shared-expenses \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Grocery shopping",
    "total_amount": 120.50,
    "paid_by": "john",
    "date": "2026-02-05",
    "category": "groceries",
    "participants": [
      {"name": "john", "amount": 60.25},
      {"name": "jane", "amount": 60.25}
    ]
  }'
```

### Track Who Owes What

```bash
curl http://localhost:5000/expenses/budget/family/member/jane
```

John paid $60.25 for groceries, so Jane owes him $60.25.

---

## 💰 Debt Tracking

### Create a Debt Account

```bash
curl -X POST http://localhost:5000/expenses/debts \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Home Loan",
    "debtor": "john",
    "creditor": "Chase Bank",
    "initial_amount": 350000,
    "interest_rate": 3.5,
    "payoff_date": "2056-02-05"
  }'
```

### Record a Payment

```bash
curl -X POST http://localhost:5000/expenses/debts/1/payments \
  -H "Content-Type: application/json" \
  -d '{
    "payment_date": "2026-02-05",
    "amount_paid": 2100.50,
    "principal": 1500,
    "interest": 600.50,
    "notes": "February payment"
  }'
```

### View Debt Progress

```bash
curl http://localhost:5000/expenses/debts
```

Shows:
- Current balance
- Progress toward payoff
- Interest rate
- Target payoff date

---

## 🎯 Financial Goals

### Create a Goal

```bash
curl -X POST http://localhost:5000/expenses/goals \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Emergency Fund",
    "description": "6 months of expenses",
    "target_amount": 25000,
    "target_date": "2027-12-31",
    "category": "savings",
    "priority": 1
  }'
```

### Contribute to a Goal

```bash
curl -X POST http://localhost:5000/expenses/goals/1/contributions \
  -H "Content-Type: application/json" \
  -d '{
    "contribution_date": "2026-02-05",
    "amount": 500,
    "contributor": "john",
    "notes": "Monthly contribution"
  }'
```

### View Goal Progress

```bash
curl http://localhost:5000/expenses/goals
```

Shows:
- Progress toward target
- Percentage complete
- Contributions made
- Time remaining

---

## 🔌 Disconnect an Account

```bash
curl -X DELETE http://localhost:5000/expenses/plaid/accounts/1
```

- Removes the account connection
- Retains transaction history
- Can re-link anytime

---

## ⚙️ Best Practices

### 1. Regular Syncing
- Sync accounts daily for accurate spending
- Check dashboard weekly for budget status
- Review spending monthly

### 2. Budget Setup
- Start with 3-month average spending
- Add 10% buffer for categories you can't control
- Adjust monthly based on actual spending

### 3. Family Participation
- Have all members input manual expenses
- Share budget goals with family
- Review spending together monthly

### 4. Debt Management
- Record all debt accounts
- Update balances after payments
- Track payoff progress toward goals

### 5. Security
- Don't share Plaid link tokens
- Use strong passwords
- Rotate credentials regularly

---

## 🆘 Troubleshooting

### Plaid Features Not Working

**Check**:
```bash
curl http://localhost:5000/expenses/plaid/accounts
```

If error "Plaid is not configured":
- Verify `PLAID_CLIENT_ID` and `PLAID_SECRET` are set
- Restart the application
- Check logs for credential errors

### Transactions Not Syncing

```bash
# Force sync
curl -X POST http://localhost:5000/expenses/plaid/accounts/1/sync

# Check last sync
curl http://localhost:5000/expenses/accounts/summary
```

### Budget Not Showing

- Confirm budget created for current month/year
- Check month and year values
- POST to `/expenses/budget/family` to create

### Category Not Matching

Plaid auto-categorizes transactions, but you can:
- Add manual categories in expenses
- Filter by known categories
- Use recommendations to improve budget

---

## 📱 API Response Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 201 | Created |
| 400 | Bad request (missing/invalid data) |
| 401 | Unauthorized (auth required) |
| 404 | Not found |
| 409 | Conflict (e.g., duplicate budget) |
| 500 | Server error |
| 503 | Plaid not configured |

---

## 🔗 Useful Links

- [Plaid Developers](https://plaid.com/developers)
- [Plaid Documentation](https://plaid.com/docs)
- [HomeHub GitHub](https://github.com/yourusername/homehub)
- [HomeHub Issues](https://github.com/yourusername/homehub/issues)

---

## 📞 Support

For issues or questions:
1. Check this guide and the main implementation docs
2. Review error messages in application logs
3. Verify Plaid credentials are correct
4. Check database connectivity
5. Open an issue with error details and API endpoint

---

**Last Updated**: February 6, 2026  
**Version**: 1.0.0
