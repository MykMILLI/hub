from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import SimpleNamespace
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import create_app
from app.models import (
    db,
    ExpenseEntry,
    IncomeEntry,
    FamilyBudget,
    BudgetCategory,
    PlaidAccount,
    PlaidTransaction,
)
from app.plaid_service import PlaidService


@dataclass
class CheckResult:
    name: str
    ok: bool
    details: str


class FakePlaidClient:
    def __init__(self, *, item_id: str, accounts: list[SimpleNamespace], tx_pages: list[list[SimpleNamespace]]):
        self._item_id = item_id
        self._accounts = accounts
        self._tx_pages = tx_pages
        self.tx_requests = []

    def item_public_token_exchange(self, request):
        return SimpleNamespace(access_token="access-token-1", item_id=self._item_id)

    def accounts_get(self, request):
        return SimpleNamespace(accounts=self._accounts)

    def transactions_get(self, request):
        opts = request.options or {}
        offset = int(opts.get("offset", 0) or 0)
        count = int(opts.get("count", 250) or 250)
        self.tx_requests.append({
            "start_date": request.start_date,
            "end_date": request.end_date,
            "offset": offset,
            "count": count,
        })

        flat = [tx for page in self._tx_pages for tx in page]
        page = flat[offset: offset + count]
        return SimpleNamespace(transactions=page, total_transactions=len(flat))



def _build_app():
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SECRET_KEY": "test-key",
        }
    )
    return app



def verify() -> list[CheckResult]:
    results: list[CheckResult] = []
    app = _build_app()

    with app.app_context():
        today = date.today()
        y, m = today.year, today.month

        # Seed ledger + budget
        income = IncomeEntry(
            date=today,
            title="Salary",
            source="Manual",
            category="Income",
            amount=5000.0,
            recipient="Alice",
            include_in_family_total=True,
            needs_confirmation=False,
        )
        expense = ExpenseEntry(
            date=today,
            title="Groceries",
            category="Food",
            amount=123.45,
            amount_paid=0.0,
            payer="Alice",
            include_in_family_total=True,
        )
        budget = FamilyBudget(
            budget_name="Family Budget",
            month=m,
            year=y,
            total_amount=1000.0,
            currency="USD",
            created_by="Administrator",
        )
        db.session.add_all([income, expense, budget])
        db.session.flush()
        db.session.add(BudgetCategory(budget_id=budget.id, category_name="Food", allocated_amount=300.0, spent_amount=0.0))

        # Seed Plaid rows used by privacy checks and dynamic sync setup
        plaid_account = PlaidAccount(
            user="Alice",
            access_token="seed-token",
            item_id="legacy:seed",
            plaid_item_id="item-seed",
            item_account_key="item-seed:acct-seed",
            account_id="acct-seed",
            account_name="Checking",
            account_type="depository",
            account_subtype="checking",
            balance=4321.99,
            currency="USD",
            active=True,
            synced_at=datetime.utcnow(),
        )
        db.session.add(plaid_account)
        db.session.flush()
        db.session.add(
            PlaidTransaction(
                plaid_account_id=plaid_account.id,
                plaid_transaction_id="seed-old-tx",
                transaction_date=today - timedelta(days=120),
                amount=88.88,
                currency="USD",
                description="Old Transaction",
                merchant="Store One",
                category="Food",
                pending=False,
            )
        )
        db.session.commit()

        # 1) Plaid persistence correctness: one item -> multiple accounts should create rows without conflict
        svc = PlaidService()
        accounts = [
            SimpleNamespace(
                account_id="acct-1",
                name="Main Checking",
                type="depository",
                subtype="checking",
                balances=SimpleNamespace(current=100.0, iso_currency_code="USD"),
            ),
            SimpleNamespace(
                account_id="acct-2",
                name="Savings",
                type="depository",
                subtype="savings",
                balances=SimpleNamespace(current=250.0, iso_currency_code="USD"),
            ),
        ]
        fake_client = FakePlaidClient(item_id="item-xyz", accounts=accounts, tx_pages=[])
        svc.client = fake_client
        svc.config = {"configured": True}

        linked = svc.exchange_public_token("public-token", "Alice")
        linked_again = svc.exchange_public_token("public-token", "Alice")
        all_item_rows = PlaidAccount.query.filter_by(plaid_item_id="item-xyz").all()

        results.append(
            CheckResult(
                "plaid_persistence_multi_account",
                len(linked) == 2 and len(linked_again) == 2 and len(all_item_rows) == 2,
                f"linked_once={len(linked)} linked_twice={len(linked_again)} stored_rows={len(all_item_rows)}",
            )
        )

        # 2) Plaid sync completeness: dynamic start date + pagination
        tx_rows = []
        for idx in range(520):
            tx_rows.append(
                SimpleNamespace(
                    transaction_id=f"new-tx-{idx}",
                    date=today - timedelta(days=max(1, 150 - idx)),
                    posted_date=today - timedelta(days=max(1, 149 - idx)),
                    amount=50.0 + idx,
                    iso_currency_code="USD",
                    name=f"Purchase {idx}",
                    merchant_name="Store One",
                    personal_finance_category=SimpleNamespace(primary="Food and Drink"),
                    pending=False,
                )
            )
        fake_client._tx_pages = [tx_rows[:260], tx_rows[260:]]

        new_count = svc.sync_transactions(plaid_account.id)
        first_req = fake_client.tx_requests[0] if fake_client.tx_requests else {}
        paginated = len(fake_client.tx_requests) >= 2
        not_fixed_30 = bool(first_req) and first_req["start_date"] <= (today - timedelta(days=90))

        results.append(
            CheckResult(
                "plaid_sync_dynamic_paginated",
                new_count >= 520 and paginated and not_fixed_30,
                f"new_count={new_count} requests={len(fake_client.tx_requests)} first_start={first_req.get('start_date')}",
            )
        )

        # 3/4/6) Endpoint reconciliation + budget sync + privacy no-leak
        client = app.test_client()
        finance = client.get(f"/api/expenses/finance/summary?year={y}&month={m}&member=Alice").get_json()
        collab = client.get(f"/api/expenses/collaboration?year={y}&month={m}").get_json()
        analytics = client.get(f"/expenses/analytics/summary?year={y}&month={m}").get_json()
        budget_detail = client.get(f"/expenses/budgets/{budget.id}").get_json()

        fam_exp = float(finance["family"]["expenses_total"])
        col_exp = float(collab["summary"]["shared_expenses_total"])
        ana_spent = float(analytics["budget"]["spent"])
        math_ok = abs(fam_exp - col_exp) < 0.01 and abs(fam_exp - ana_spent) < 0.01

        results.append(
            CheckResult(
                "finance_math_reconciles",
                math_ok,
                f"finance_expenses={fam_exp} collab_expenses={col_exp} analytics_spent={ana_spent}",
            )
        )

        budget_food = 0.0
        for row in budget_detail.get("categories", []):
            if (row.get("name") or "").strip().lower() == "food":
                budget_food = float(row.get("spent") or 0.0)
        budget_ok = abs(budget_food - 123.45) < 0.01

        results.append(
            CheckResult(
                "budget_category_derived_synced",
                budget_ok,
                f"food_spent={budget_food}",
            )
        )

        # Privacy mode checks: values should be masked
        privacy_checks = {
            "month": client.get(f"/api/expenses/month?year={y}&month={m}&privacy=1").get_json(),
            "all": client.get(f"/api/expenses/all?privacy=1").get_json(),
            "finance": client.get(f"/api/expenses/finance/summary?year={y}&month={m}&member=Alice&privacy=1").get_json(),
            "collab": client.get(f"/api/expenses/collaboration?year={y}&month={m}&privacy=1").get_json(),
            "analytics": client.get(f"/expenses/analytics/summary?year={y}&month={m}&privacy=1").get_json(),
            "plaid_accounts": client.get("/expenses/plaid/accounts?privacy=1").get_json(),
            "plaid_tx": client.get(f"/expenses/plaid/accounts/{plaid_account.id}/transactions?privacy=1").get_json(),
        }

        leaked_values = [123.45, 4321.99, 88.88, 5000.0]

        def contains_real(v):
            if isinstance(v, dict):
                return any(contains_real(x) for x in v.values())
            if isinstance(v, list):
                return any(contains_real(x) for x in v)
            if isinstance(v, (int, float)):
                return any(abs(float(v) - n) < 0.001 for n in leaked_values)
            return False

        leak_keys = [name for name, payload in privacy_checks.items() if contains_real(payload)]
        privacy_ok = not leak_keys
        results.append(
            CheckResult(
                "privacy_no_numeric_leak",
                privacy_ok,
                "checked month/all/finance/collab/analytics/plaid accounts/plaid tx payloads"
                + ("; leaked=" + ",".join(leak_keys) if leak_keys else ""),
            )
        )

    return results


if __name__ == "__main__":
    checks = verify()
    for item in checks:
        status = "PASS" if item.ok else "FAIL"
        print(f"[{status}] {item.name}: {item.details}")
    if not all(c.ok for c in checks):
        raise SystemExit(1)
