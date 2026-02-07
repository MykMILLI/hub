# 💰 HomeHub Money Management System

## Quick Start Guide

### Access the Dashboard
1. Open HomeHub in your browser (`http://localhost:5000`)
2. Navigate to **Money Management** in the sidebar (🐷 icon)
3. You'll see the financial analytics dashboard

### Create Your First Budget

**Step 1: Click "New Budget"**
- Budget Name: "July 2024"
- Month: July
- Year: 2024
- Total Amount: $5,000.00
- Click "Create"

**Step 2: Add Budget Categories**
From the budget details view:
- Click "+ Category"
- Category: "Groceries"
- Allocated: $800
- Repeat for other categories (utilities, dining, etc.)

**Step 3: Log Expenses**
Visit the Expense Tracker to add expenses that match your budget categories. The Money Management system will automatically track spending against your budget.

### Split a Shared Expense

**Example: Restaurant dinner with friends**

1. Click "Split Expense"
2. Fill in details:
   - Description: "Italian Restaurant"
   - Total Amount: $120.00
   - Category: "Dining"
   - Paid by: Your name
3. Add participants:
   - Your name: $40.00
   - Friend 1: $40.00
   - Friend 2: $40.00
4. Click "Create"

The system automatically tracks who owes whom and can be marked settled when payments are made.

### Track a Personal Debt

**Example: Borrow money from family member**

**API Approach (Command Line):**
```bash
curl -X POST http://localhost:5000/money/debts \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Car down payment",
    "debtor": "Alice",
    "creditor": "Dad",
    "initial_amount": 5000,
    "interest_rate": 0,
    "payoff_date": "2025-07-31"
  }'
```

Or through the dashboard form:
1. Click "Track Debt" (via tab)
2. Enter debt details
3. Record payments as they're made

### Set a Family Savings Goal

**Example: Summer vacation fund**

1. Click "Set Goal"
2. Fill in details:
   - Goal Name: "Summer Vacation 2024"
   - Target Amount: $5,000
   - Target Date: August 31, 2024
   - Category: "Travel"
   - Priority: High (1)
3. Click "Create"

**Add Contributions:**
- Family members contribute toward the goal
- System tracks total progress
- Visual progress bar shows percentage complete

### Link Bank Accounts (Optional - Requires Plaid)

**If you have Plaid configured:**

1. Click "Money Management" → "Bank Accounts" tab
2. Click "Link Bank Account"
3. A Plaid Link modal will appear
4. Select your bank and authorize
5. Choose which accounts to link
6. The system will automatically:
   - Import transactions
   - Track balances
   - Categorize spending
   - Sync new transactions daily

**Without Plaid:**
The system works perfectly fine without bank linking. You can manually log all expenses.

---

## Core Concepts

### Family Budget
A monthly budget for the entire household with:
- Total monthly allocation
- Category-based sub-budgets
- Real-time spending tracking
- Progress visualization

**Use Case:** Family of 4 wants to limit spending to $5,000/month across groceries ($800), utilities ($300), dining ($500), entertainment ($300), and savings ($2,100).

### Shared Expense
An expense paid by one person but owed by multiple people:
- Automatic split calculation
- Participant tracking
- Settlement status
- Category classification

**Use Case:** Mom buys groceries for $150 and splits with Dad and Alice ($50 each). When Dad pays her back, the expense is marked settled.

### Debt Account
A loan or personal debt with:
- Principal and interest tracking
- Payment history
- Payoff target date
- Progress toward settlement

**Use Case:** Alice borrows $5,000 from Dad for a car down payment, paying back over 2 years with no interest.

### Financial Goal
A savings target with:
- Target amount and date
- Multiple contributors
- Progress tracking
- Priority levels

**Use Case:** Family wants to save $10,000 for a new roof by next year. Everyone contributes as they can.

### Bank Account (Plaid)
A linked financial account that:
- Syncs transactions automatically
- Updates balances in real-time
- Categorizes expenses
- Enables reconciliation

**Use Case:** Link checking and savings accounts to see a complete picture of family finances.

---

## Data Flow

```
User Input
    ↓
Form/API Submission
    ↓
Validation & Sanitization
    ↓
Database Storage
    ↓
Real-time Calculation
    ↓
Dashboard Display / API Response

Bank Account Sync (Optional):
    ↓
Plaid OAuth
    ↓
Access Token Exchange
    ↓
Transaction Retrieval (30-day window)
    ↓
Automatic Categorization
    ↓
Database Storage
    ↓
Dashboard Integration
```

---

## Key Calculations

### Budget Status
```
Remaining = Total Budget - Sum(Category Spending)
Percentage = (Category Spending / Category Budget) × 100
```

### Shared Expense Settlement
```
Per Person Share = Total Amount / Number of Participants
Amount Owed = Participant Share (if paid by someone else)
```

### Debt Progress
```
Paid Principal = Initial Amount - Current Balance
Progress % = (Paid Principal / Initial Amount) × 100
Interest Accrued = Current Balance × Interest Rate / 12 (monthly)
```

### Goal Progress
```
Progress % = (Current Amount / Target Amount) × 100
Months Until Target = (Target Date - Today) / 30 days
Monthly Needed = (Target Amount - Current Amount) / Months Until Target
```

---

## API Authentication

All money management endpoints require **password authentication**.

### How to Authenticate

**With Form (UI):**
The dashboard automatically prompts for password before state-changing operations.

**With API:**
Include password in request headers or body:

```bash
# Example: Create a budget with curl
curl -X POST http://localhost:5000/money/budgets \
  -H "Content-Type: application/json" \
  -d '{
    "password": "your_homehub_password",
    "name": "August Budget",
    "month": 8,
    "year": 2024,
    "total_amount": 5000,
    "created_by": "Family"
  }'
```

---

## Troubleshooting

### "404 Not Found" on Money Page
**Solution:** 
- Ensure you've logged in with the correct password
- Clear browser cache and reload
- Check that the money blueprint was registered

### Plaid Link Not Showing
**Solution:**
- Verify Plaid credentials are configured
- Check environment variables: `PLAID_CLIENT_ID` and `PLAID_SECRET`
- Switch to sandbox environment if testing
- Check browser console for JavaScript errors

### Budget Not Showing Spending
**Solution:**
- Add expenses to the Expense Tracker
- Ensure expense categories match budget categories (case-sensitive)
- Expenses must have dates within the budget month/year
- Check that expense amounts are greater than 0

### Can't Add Shared Expense
**Solution:**
- Participant amounts must sum to total amount (within 0.01 tolerance)
- All participants must have non-empty names
- Expense date must be valid (YYYY-MM-DD format)

### Plaid Transactions Not Syncing
**Solution:**
- Click "Sync" button on each account to force refresh
- Check that at least one Plaid account is linked and active
- Verify Plaid credentials are still valid (may need re-linking)
- Check Docker logs for Plaid API errors

---

## Performance Tips

1. **Archive Old Budgets** - Keep active budgets under 12 months for faster queries
2. **Batch Expense Entry** - Add multiple expenses at once rather than one-by-one
3. **Regular Plaid Sync** - Set a reminder to sync accounts weekly for fresh data
4. **Clear Settled Expenses** - Mark shared expenses as settled to reduce clutter
5. **Archive Paid Debts** - Mark debts as inactive once fully paid

---

## Data Privacy

✅ **What is stored locally:**
- All budgets, expenses, debts, goals
- Bank account names and balances
- Transaction descriptions and amounts
- User preferences and settings

❌ **What is NOT stored:**
- Actual bank credentials
- Passwords (only hashed)
- API keys or secrets
- Login tokens beyond session

✅ **Plaid Specific:**
- Access tokens are encrypted in database
- No transaction details sent externally
- OAuth connection handled securely
- Can disconnect at any time

---

## Monthly Workflow

### Beginning of Month
1. Create new monthly budget
2. Review previous month's spending
3. Adjust category allocations as needed
4. Set financial goals for the month

### During Month
1. Log expenses daily or weekly
2. Settle shared expenses promptly
3. Record debt payments on schedule
4. Monitor budget progress
5. Contribute toward goals

### End of Month
1. Review final budget vs. actual
2. Analyze spending patterns
3. Settle any outstanding expenses
4. Pay debts on time
5. Plan next month's budget

---

## Advanced Features

### Expense Splitting Algorithm
```
If 3 people split $120 equally:
  Each person's share = $120 ÷ 3 = $40
  
If split unequally ($50, $40, $30):
  Verify sum = $120 ✓
  Record individual shares in database
```

### Interest Calculation
```
Monthly Interest = (Balance × Annual Rate) / 12
Example: $10,000 loan at 3% annual rate
  Monthly Interest = ($10,000 × 0.03) / 12 = $25
```

### Budget Burn Rate
```
Days into month: 20 / 31
Budget spent: $3,200 / $5,000 = 64%
Burn rate: 64% / (20/31) = 98.4%
Status: On track (should be ~65% at day 20)
```

---

## Integration with Other HomeHub Features

### Expense Tracker Integration
- Individual expenses from Expense Tracker count toward budget
- Recurring expenses automatically generate daily entries
- Expense categories align with budget categories

### Family Member Integration
- Add family members in Settings
- Assign shared expenses to family members
- Track contributions by person
- View per-person spending and debt

### Dashboard Integration
- Money summary appears on main dashboard
- Quick links to financial features
- At-a-glance budget status
- Alerts for budget overages

---

## Support & Documentation

📚 **Full Documentation:**
- `MONEY_MANAGEMENT_GUIDE.md` - Complete API reference
- `MONEY_ENHANCEMENT_SUMMARY.md` - Technical implementation details
- `MONEY_FEATURES_SHOWCASE.md` - Feature overview with examples

💬 **Getting Help:**
- Check inline code comments for implementation details
- Review API endpoint examples in documentation
- Check Docker logs for server errors
- Verify database schema with SQLAlchemy models

---

## What's Next?

**Coming Soon:**
- 📊 Advanced spending analytics and charts
- 📧 Budget alert notifications
- 🎯 Automatic recurring-to-budget matching
- 💳 Credit card reconciliation
- 📈 Financial projections and forecasting
- 🌍 Multi-currency support
- 📱 Mobile app companion

---

**Start managing your family finances today with HomeHub Money Management! 💰**

Questions? Check the comprehensive documentation files or review the API examples in the Money Management Guide.
