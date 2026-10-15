# FINANCE_DAILY_DRIVER_TECHNICAL_HANDOFF

## 1. Current architecture

### Web frontend stack and main template files
- Stack: server-rendered Jinja templates + vanilla JavaScript + Chart.js.
- Base shell/navigation: `templates/base.html`.
- Main finance web UI: `templates/expenses.html`.
- Legacy/parallel money UI file still present: `templates/money.html`.
- Chart library usage in finance UI: `templates/expenses.html` loads Chart.js and creates charts (`new Chart(...)`).

### Flutter frontend stack and main finance UI files
- Stack: Flutter app with finance functionality implemented inside a single large file.
- Main Flutter app and finance client/UI code: `flutter/lib/main.dart`.
- Flutter dependencies include HTTP, SharedPreferences, notifications, webview, etc.: `flutter/pubspec.yaml`.
- Finance API client methods in Flutter: `HomeHubApi` methods in `flutter/lib/main.dart` (budget, debt, goals, Plaid endpoints).
- Native finance page implementation: `ExpensesPage` in `flutter/lib/main.dart`.

### Backend stack, app factory, blueprints, and DB model ownership
- Stack: Flask + Flask-SQLAlchemy.
- App factory and setup: `app/__init__.py` (`create_app`).
- App factory registers:
  - `main_bp` routes from `app/blueprints/*.py` modules.
  - `money.money_bp` from `app/blueprints/money.py`.
- Plaid service initialized in app factory: `plaid_service.init_app(app)` in `app/__init__.py`.
- Primary finance route ownership today:
  - `/expenses...` and `/api/expenses...` routes in `app/blueprints/expenses.py`.
  - Parallel `/money...` finance routes in `app/blueprints/money.py`.
- Primary finance model ownership: `app/models.py`.

### Storage model for finance data and Plaid-linked data
- Primary DB: SQLite (`data/app.db`) configured in `app/__init__.py`.
- ORM: SQLAlchemy models in `app/models.py`.
- Core finance models:
  - `IncomeEntry`, `RecurringIncome`
  - `ExpenseEntry`, `RecurringExpense`, `RecurringBillPayment`
  - `FamilyBudget`, `BudgetCategory`
  - `SharedExpense`, `SharedRecurringBill`, `SharedRecurringBillParticipant`
  - `DebtAccount`, `DebtPayment`
  - `FinancialGoal`, `GoalContribution`
  - `PlaidAccount`, `PlaidTransaction`
- Plaid-linked data storage:
  - Account metadata and access token in `PlaidAccount`.
  - Transactions in `PlaidTransaction`.
- Client-side storage:
  - Web: `localStorage` for member/layout/theme/session-ish client state.
  - Flutter: `SharedPreferences` for app settings and privacy mask state.

---

## 2. Exact finance feature status

Status labels used here:
- **implemented**: fully present with routes/model/UI path in current code.
- **partial**: present but incomplete or inconsistent for daily-driver behavior.
- **missing**: no implementation path found.

### Income
- Status: **implemented**
- Backend:
  - Add income: route `@main_bp.route('/expenses/income', methods=['POST'])` in `app/blueprints/expenses.py` (`income_add`).
  - Income scheduling: `@main_bp.route('/expenses/income/schedule', methods=['POST'])`.
  - Pending confirmations: `@main_bp.route('/api/expenses/income/pending-confirmations', methods=['GET'])`.
  - Confirm income: `@main_bp.route('/expenses/income/confirm/<int:income_id>', methods=['POST'])`.
  - Include/exclude in family totals: `@main_bp.route('/expenses/income/<int:income_id>/toggle-family', methods=['POST'])`.
- Models:
  - `IncomeEntry`, `RecurringIncome` in `app/models.py`.
- UI:
  - Web: forms and controls in `templates/expenses.html`.
  - Flutter: API methods and UI actions in `flutter/lib/main.dart`.

### Expenses
- Status: **implemented**
- Backend:
  - Main page: `@main_bp.route('/expenses', methods=['GET', 'POST'])` (`expenses`).
  - Month/all APIs: `/api/expenses/month`, `/api/expenses/all`.
  - Quick add: `/api/expenses/quick-add`.
  - Edit/delete/bulk delete: `/expenses/edit/<int:entry_id>`, `/expenses/delete/<int:entry_id>`, `/expenses/bulk-delete`.
  - Paid toggle/amount update path: `/api/expenses/entries/<int:entry_id>/paid`.
- Models:
  - `ExpenseEntry`, `RecurringExpense`, `SharedExpense`, `SharedRecurringBill`, `SharedRecurringBillParticipant`.
- UI:
  - Web: full expense tracker in `templates/expenses.html`.
  - Flutter: `ExpensesPage` and API methods in `flutter/lib/main.dart`.

### Budgets
- Status: **partial**
- Backend routes:
  - `/expenses/budgets` (GET/POST)
  - `/expenses/budgets/<int:budget_id>` (GET/PUT)
  - `/expenses/budgets/<int:budget_id>/categories` (POST)
  - `/expenses/budgets/<int:budget_id>/categories/<int:cat_id>` (PUT/DELETE)
  - `/expenses/budget/family` (GET/POST)
  - `/expenses/budget/family/member/<member>` (GET)
  - `/expenses/budget/recommendations` (GET)
- Models:
  - `FamilyBudget`, `BudgetCategory`.
- Partial reason:
  - `BudgetCategory.spent_amount` is read by budget responses but no robust update flow from transaction posting was found.

### Categories
- Status: **partial**
- Implemented pieces:
  - Category fields on `ExpenseEntry`, `IncomeEntry`, `PlaidTransaction`.
  - Category aggregation endpoints (e.g., account category analytics).
- Missing pieces:
  - No merchant/category rules engine for automatic recategorization of Plaid transactions.
  - No rule model/table for category mapping was found.

### Recurring bills
- Status: **implemented**
- Backend:
  - Recurring expense generation and carryover logic in functions:
    - `_generate_recurring_entries_until`
    - `_recalculate_recurring_series_balances`
    - recurring adjustment/history routes:
      - `/api/expenses/recurring/<int:recurring_id>/amount`
      - `/api/expenses/recurring/<int:recurring_id>/history`
  - Shared recurring bills routes:
    - `/api/expenses/shared-recurring-bills` (GET/POST)
- Models:
  - `RecurringExpense`, `RecurringBillPayment`, `SharedRecurringBill`, `SharedRecurringBillParticipant`.

### Debt tracking
- Status: **implemented**
- Backend routes:
  - `/expenses/debts` (GET/POST)
  - `/expenses/debts/<int:debt_id>/payments` (POST)
  - `/expenses/debts/<int:debt_id>` (DELETE)
  - debt workflow snapshots and impact:
    - `/api/expenses/debt-workflow`
    - `/api/expenses/debt-workflow/impact`
- Models:
  - `DebtAccount`, `DebtPayment`.

### Savings goals
- Status: **implemented**
- Backend routes:
  - `/expenses/goals` (GET/POST)
  - `/expenses/goals/<int:goal_id>/contributions` (POST)
  - savings helper endpoints:
    - `/expenses/savings/add`
    - `/expenses/savings/entries`
    - `/expenses/savings/entries/<int:contribution_id>` (DELETE)
- Models:
  - `FinancialGoal`, `GoalContribution`.

### Reporting/dashboard summaries
- Status: **partial**
- Implemented routes:
  - `/api/expenses/finance/summary`
  - `/api/expenses/collaboration`
  - `/expenses/analytics/summary`
  - Plaid account analytics:
    - `/expenses/accounts/summary`
    - `/expenses/accounts/<int:account_id>/summary`
    - `/expenses/accounts/<int:account_id>/transactions`
    - `/expenses/accounts/<int:account_id>/expenses-by-category`
- UI:
  - Web chart/dashboard logic in `templates/expenses.html`.
- Partial reason:
  - Multiple summary paths compute totals differently, so displayed totals can disagree.

---

## 3. Exact Plaid implementation status

### Current web Plaid flow (implemented end-to-end)
1. Link token route
- Backend route: `@main_bp.route('/expenses/plaid/link-token', methods=['POST'])` in `app/blueprints/expenses.py` (`create_plaid_link_token`).
- Calls `plaid_service.create_link_token(user, redirect_uri)` in `app/plaid_service.py`.

2. Plaid Link frontend launch
- Web UI button and flow in `templates/expenses.html`:
  - Requests `/expenses/plaid/link-token`.
  - Loads Plaid SDK (proxy route first, CDN fallback).
  - Calls `Plaid.create({ token, onSuccess, onExit })` and `handler.open()`.

3. Exchange route
- Backend route: `@main_bp.route('/expenses/plaid/exchange-token', methods=['POST'])` in `app/blueprints/expenses.py` (`exchange_plaid_token`).
- Receives `public_token` + `user`, calls `plaid_service.exchange_public_token(public_token, user)`.

4. access_token storage path
- In `app/plaid_service.py`, `exchange_public_token` stores `access_token` on each `PlaidAccount` row.
- Model field: `PlaidAccount.access_token` in `app/models.py`.

### Current Flutter Plaid flow status
- Implemented token request methods:
  - `createPlaidLinkToken()` in `flutter/lib/main.dart`.
  - `exchangePlaidPublicToken(String publicToken)` in `flutter/lib/main.dart`.
- Implemented UI callsite found:
  - `createPlaidLinkToken()` is called from the native expense page and shows a snackbar.
- Missing native/web Plaid launch path:
  - No native Plaid Link SDK integration/package was found in Flutter dependencies.
  - No WebView-based Plaid Link launch path was found.
- Missing exchange callsite:
  - No callsite was found in Flutter UI that invokes `exchangePlaidPublicToken(...)` after user completes Plaid Link.

### Current schema/runtime issues found in code
- **item_id uniqueness conflict risk**:
  - `PlaidAccount.item_id` is declared unique in `app/models.py`.
  - `exchange_public_token` loops through all accounts returned by Plaid and inserts one `PlaidAccount` row per account, each with the same `item_id`.
  - This creates a conflict risk when an item has multiple accounts.
- **Limited sync retrieval behavior**:
  - `sync_transactions` uses a single `transactions_get` request with `count=250`, `offset=0`.
  - No pagination loop/cursor sync path exists in current implementation.

---

## 4. Financial math and reconciliation status

### Source-of-truth models currently used
- Expense totals: `ExpenseEntry` (`amount`, `amount_paid`, `include_in_family_total`, recurring metadata).
- Income totals: `IncomeEntry` (`amount`, `needs_confirmation`, `include_in_family_total`) and `RecurringIncome` for projected occurrences.
- Budget allocations/spent: `FamilyBudget` + `BudgetCategory` (`allocated_amount`, `spent_amount`).
- Goals: `FinancialGoal` + `GoalContribution`.
- Debts: `DebtAccount` + `DebtPayment`.
- Plaid balances/transactions: `PlaidAccount.balance` + `PlaidTransaction`.

### Core math paths in code
- Entry-level paid/due math helpers:
  - `_entry_paid_amount`, `_entry_due_amount`, `_expense_breakdown_for_entries` in `app/blueprints/expenses.py`.
- Month assembly and totals:
  - `_build_month_history` in `app/blueprints/expenses.py`.
- Family/member aggregation:
  - `_aggregate_month_accounting` in `app/blueprints/expenses.py`.
- Finance summary endpoint:
  - `/api/expenses/finance/summary` (`api_finance_summary`) in `app/blueprints/expenses.py`.
- Collaboration summary endpoint:
  - `/api/expenses/collaboration` (`api_expenses_collaboration`) in `app/blueprints/expenses.py`.
- Separate analytics summary endpoint:
  - `/expenses/analytics/summary` (`get_analytics_summary`) in `app/blueprints/expenses.py`.

### Current mismatch/drift risks found
- Budget drift risk:
  - Budget APIs rely on `BudgetCategory.spent_amount`, but no robust write/update path from expense posting to this field was found.
- Summary divergence risk:
  - `/api/expenses/finance/summary` and `/api/expenses/collaboration` rely on month-history accounting logic.
  - `/expenses/analytics/summary` uses a different direct-sum approach (`sum(ExpenseEntry.amount)` and shared expenses), causing possible disagreement.
- Inclusion rule mismatch risk:
  - Some paths use `include_in_family_total` filters; others are direct sums and may not apply identical filtering.
- Projection vs stored data divergence:
  - `_collect_projected_income_occurrences` injects projected items for display/accounting; those entries may not exist as persisted `IncomeEntry` records yet.
- Plaid analytics inclusion risk:
  - Account/category summaries include all retrieved Plaid transactions in queried ranges; no transfer/refund normalization layer was found.

### Where totals can disagree today
- `api_finance_summary` (`/api/expenses/finance/summary`) vs `get_analytics_summary` (`/expenses/analytics/summary`):
  - Different aggregation methods and inclusion handling.
- Collaboration summary (`/api/expenses/collaboration`) vs analytics summary (`/expenses/analytics/summary`):
  - Collaboration uses month-history/accounting structures; analytics summary uses direct sums.
- Budget category spent outputs vs expense ledger totals:
  - Category `spent_amount` can diverge from ledger-derived spending totals.

---

## 5. Privacy toggle status

### Web behavior (current)
- No hide-actual-income masking toggle implementation was found in web finance UI.
- Existing toggles in `templates/expenses.html` are:
  - include/exclude income from family totals (`.income-toggle-checkbox` -> `/expenses/income/<id>/toggle-family`)
  - Actual vs Projected history filters (`history-filter-btn`)
- These are filtering/inclusion controls, not value masking controls.

### Flutter behavior (current)
- Privacy mask exists in Flutter shell:
  - State: `_privacyMaskEnabled` in `flutter/lib/main.dart`.
  - Persisted via SharedPreferences key from `_privacyMaskKey`.
  - Toggle action: `_togglePrivacyMask()`.
- Behavior:
  - When enabled, app draws a full-screen visual overlay over many sections, including Expense & Money.
  - It is visual blocking only; no alternate masked values are generated by finance APIs.

### Where true values can still leak
- Web: values are shown normally because no mask toggle exists.
- Flutter: data is still fetched/rendered in the underlying widgets; the mask is a display overlay, not data redaction.

---

## 6. Daily-driver blockers

### Plaid/platform blockers
- Flutter cannot complete Plaid link flow end-to-end (token request exists; link launch + public token exchange callsite missing).
- Plaid item/account storage has unique-constraint conflict risk on `item_id` for multi-account items.
- Transaction sync retrieval is limited to one request window (`count=250`, `offset=0`) with no cursor/pagination loop.

### Financial math/reconciliation blockers
- Multiple summary endpoints calculate totals differently, causing inconsistent numbers across screens.
- Budget category `spent_amount` is not clearly synchronized from expense ledger writes.
- Projected income display/accounting can diverge from persisted income rows.

### Budgeting/categorization blockers
- No transaction categorization rules engine (merchant/category mapping overrides) was found.
- Category quality depends on manual entry/Plaid category fields with no rule governance layer.

### Privacy/safe sharing blockers
- Web has no actual finance masking toggle.
- Flutter privacy mode is UI overlay only; not field-level masking/redaction.

### UX trustworthiness blockers
- Users can see different totals depending on which endpoint/widget they are looking at.
- Budget progress by category can appear authoritative while depending on stale `spent_amount` fields.

---

## 7. Files map

### Backend (finance + app wiring)
- App factory / registration:
  - `app/__init__.py`
- Primary finance routes:
  - `app/blueprints/expenses.py`
- Parallel finance routes (/money prefix):
  - `app/blueprints/money.py`
- Plaid API integration service:
  - `app/plaid_service.py`
- Finance/Plaid data models:
  - `app/models.py`

### Web finance files
- Main finance page and JS flows:
  - `templates/expenses.html`
- Base app shell/nav:
  - `templates/base.html`
- Legacy/parallel money template:
  - `templates/money.html`

### Flutter finance files
- Main app + API client + native finance page:
  - `flutter/lib/main.dart`
- Flutter dependency/config file:
  - `flutter/pubspec.yaml`

### Plaid model/service ownership files
- Plaid service implementation:
  - `app/plaid_service.py`
- Plaid DB models (`PlaidAccount`, `PlaidTransaction`):
  - `app/models.py`
- Plaid routes under `/expenses/plaid/...`:
  - `app/blueprints/expenses.py`

---

## Most Important Fixes to Implement Next

1. Fix Plaid account-link persistence conflict (`PlaidAccount.item_id` uniqueness vs per-account inserts) so multi-account links are reliable.
2. Implement end-to-end Flutter Plaid flow (native/web Plaid launch + `public_token` exchange callsite).
3. Unify summary math paths so `/api/expenses/finance/summary`, `/api/expenses/collaboration`, and `/expenses/analytics/summary` cannot disagree on core totals.
4. Add deterministic budget-spent synchronization so `BudgetCategory.spent_amount` reflects ledger reality.
5. Add robust Plaid sync retrieval beyond a single `transactions_get` page.
6. Add transaction categorization rules (merchant/category mapping and override persistence).
7. Implement a real web privacy masking control and convert Flutter privacy mode from overlay-only to field-level redaction behavior.