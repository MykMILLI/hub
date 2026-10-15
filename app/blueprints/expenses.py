from flask import render_template, request, redirect, url_for, flash, jsonify, current_app, session, Response
from datetime import datetime, date, timedelta
import calendar as _calendar
import json
import os
import math
import hashlib
from types import SimpleNamespace
from collections import defaultdict
from uuid import uuid4
from werkzeug.utils import secure_filename
from ..models import (
    db, RecurringExpense, ExpenseEntry, IncomeEntry,
    FamilyBudget, BudgetCategory, SharedExpense, DebtAccount, DebtPayment,
    FinancialGoal, GoalContribution, PlaidAccount, PlaidTransaction,
    FamilyMember, RecurringIncome, RecurringBillPayment, ActivityLog,
    SharedRecurringBill, SharedRecurringBillParticipant, TransactionCategoryRule,
)
from ..security import sanitize_text
from ..blueprints import main_bp
from ..plaid_service import plaid_service
import bleach
import logging
import requests
from ..activity import log_activity

logger = logging.getLogger(__name__)
HUB_SAVINGS_NOTE_PREFIX = '[hub-savings]'


def _matches_category_rule(value: str, match_type: str, needle: str) -> bool:
    hay = (value or '').strip().lower()
    ndl = (needle or '').strip().lower()
    if not hay or not ndl:
        return False
    t = (match_type or 'contains').strip().lower()
    if t == 'equals':
        return hay == ndl
    if t == 'startswith':
        return hay.startswith(ndl)
    if t == 'endswith':
        return hay.endswith(ndl)
    if t == 'normalized_equals':
        norm_hay = ' '.join(''.join(ch if ch.isalnum() else ' ' for ch in hay).split())
        norm_ndl = ' '.join(''.join(ch if ch.isalnum() else ' ' for ch in ndl).split())
        return bool(norm_hay and norm_ndl and norm_hay == norm_ndl)
    return ndl in hay


def _apply_expense_category_rules(*, title: str = '', merchant: str = '', description: str = '', fallback: str = '') -> str:
    rules = (
        TransactionCategoryRule.query
        .filter_by(active=True)
        .order_by(TransactionCategoryRule.priority.asc(), TransactionCategoryRule.id.asc())
        .all()
    )
    for rule in rules:
        field = (rule.match_field or 'merchant').strip().lower()
        source = merchant
        if field in {'description', 'title'}:
            source = description or title
        if _matches_category_rule(source or '', rule.match_type or 'contains', rule.match_value or ''):
            category = sanitize_text(rule.category or '').strip()
            if category:
                return category
    return fallback


def _is_transfer_like(text: str | None, category: str | None = None) -> bool:
    blob = f"{text or ''} {(category or '')}".lower()
    transfer_markers = (
        'transfer', 'xfer', 'zelle', 'venmo cashout', 'internal transfer', 'ach transfer', 'payment from', 'payment to'
    )
    return any(marker in blob for marker in transfer_markers)


def _is_refund_like(text: str | None, amount: float, category: str | None = None) -> bool:
    blob = f"{text or ''} {(category or '')}".lower()
    if amount < 0:
        return True
    refund_markers = ('refund', 'reversal', 'chargeback', 'adjustment credit', 'credit')
    return any(marker in blob for marker in refund_markers)


def _plaid_spending_amount(row: PlaidTransaction, *, include_pending: bool = False) -> float:
    if not include_pending and bool(row.pending):
        return 0.0
    amount = float(row.amount or 0.0)
    text = f"{row.merchant or ''} {row.description or ''}"
    if _is_transfer_like(text, row.category):
        return 0.0
    if _is_refund_like(text, amount, row.category):
        return 0.0
    return max(amount, 0.0)


def _budget_category_spent_map(year_value: int, month_value: int) -> dict[str, float]:
    history = _build_month_history(year_value, month_value)
    totals: dict[str, float] = defaultdict(float)
    for item in history.get('items', []):
        if not bool(item.get('include_in_family_total', True)):
            continue
        if (item.get('entry_type') or '') not in {'expense', 'debt_due'}:
            continue
        category = (item.get('category') or 'Uncategorized').strip() or 'Uncategorized'
        totals[category] += float(item.get('paid_amount', 0.0) or 0.0) + float(item.get('due_amount', 0.0) or 0.0)
    return {k: round(float(v), 2) for k, v in totals.items()}


def _refresh_budget_category_spent_for_period(year_value: int, month_value: int) -> None:
    budget = FamilyBudget.query.filter_by(year=year_value, month=month_value).first()
    if not budget:
        return

    totals = _budget_category_spent_map(year_value, month_value)

    for cat in budget.categories:
        key = (cat.category_name or '').strip() or 'Uncategorized'
        cat.spent_amount = round(float(totals.get(key, 0.0)), 2)
    db.session.flush()


def _sync_budget_for_entry_date(entry_date: date | None) -> None:
    if not entry_date:
        return
    _refresh_budget_category_spent_for_period(int(entry_date.year), int(entry_date.month))


def _is_privacy_mode() -> bool:
    value = (request.args.get('privacy') or '').strip().lower()
    return value in {'1', 'true', 'yes', 'on'}


def _privacy_bucket(seed: str, low: int, high: int, step: int = 1000) -> float:
    digest = hashlib.sha256(seed.encode('utf-8')).hexdigest()
    n = int(digest[:8], 16)
    span = max(high - low, step)
    raw = low + (n % span)
    return float((raw // step) * step)


def _privacy_mask_payload(payload: dict) -> dict:
    masked = json.loads(json.dumps(payload or {}))
    items = list((masked.get('history') or {}).get('items') or [])

    income_total = 0.0
    expense_total = 0.0
    for item in items:
        entry_type = (item.get('entry_type') or '').strip()
        seed = f"{entry_type}:{item.get('id')}:{item.get('date')}"
        if entry_type == 'income':
            shown = _privacy_bucket(seed, 10000, 150000)
            item['charged_amount'] = shown
            item['amount'] = shown
            item['paid_amount'] = shown
            item['due_amount'] = 0.0
            if 'amount_paid' in item:
                item['amount_paid'] = shown
            if 'remaining_amount' in item:
                item['remaining_amount'] = 0.0
            if 'effective_amount' in item:
                item['effective_amount'] = shown
            income_total += shown
        else:
            shown = _privacy_bucket(seed, 10000, 100000)
            item['charged_amount'] = shown
            item['amount'] = shown
            item['paid_amount'] = shown
            item['due_amount'] = 0.0
            if 'amount_paid' in item:
                item['amount_paid'] = shown
            if 'remaining_amount' in item:
                item['remaining_amount'] = 0.0
            if 'effective_amount' in item:
                item['effective_amount'] = shown
            expense_total += shown

    if 'history' in masked:
        masked['history']['items'] = items
        masked['history']['totals'] = {
            'income_total': income_total,
            'income_paid_total': income_total,
            'income_due_total': 0.0,
            'expenses_total': expense_total,
            'expenses_charged_total': expense_total,
            'expenses_paid_total': expense_total,
            'expenses_due_total': 0.0,
            'net_total': income_total - expense_total,
            'net_paid_total': income_total - expense_total,
            'net_due_total': 0.0,
            'projected_income_total': 0.0,
        }
        masked['history']['accounting'] = {
            'income': {
                'paid_total': income_total,
                'due_total': 0.0,
                'expected_total': income_total,
                'paid_by_member': {'Family': income_total},
                'due_by_member': {'Family': 0.0},
            },
            'expenses': {
                'charged_total': expense_total,
                'paid_total': expense_total,
                'due_total': 0.0,
                'expected_total': expense_total,
                'paid_by_member': {'Family': expense_total},
                'due_by_member': {'Family': 0.0},
            },
            'net': {
                'paid_total': income_total - expense_total,
                'expected_total': income_total - expense_total,
                'due_total': 0.0,
            },
            'members': [
                {
                    'name': 'Family',
                    'income_paid': income_total,
                    'income_due': 0.0,
                    'income_expected': income_total,
                    'expense_charged': expense_total,
                    'expense_paid': expense_total,
                    'expense_due': 0.0,
                    'expense_expected': expense_total,
                    'net_paid': income_total - expense_total,
                    'net_expected': income_total - expense_total,
                }
            ],
        }

    if 'summary' in masked:
        masked['summary']['total_this_month'] = income_total - expense_total
        masked['summary']['per_payer'] = {'Family': expense_total}
        masked['summary']['per_category'] = {'Household': expense_total}
        masked['summary']['top_category'] = 'Household'

    if 'by_date' in masked:
        for _, block in (masked.get('by_date') or {}).items():
            rows = block.get('entries') or []
            day_total = 0.0
            for idx, row in enumerate(rows):
                seed = f"bydate:{row.get('id')}:{row.get('date')}:{idx}"
                shown = _privacy_bucket(seed, 10000, 120000, 1000)
                row['amount'] = shown
                row['charged_amount'] = shown
                row['paid_amount'] = shown
                row['due_amount'] = 0.0
                if 'amount_paid' in row:
                    row['amount_paid'] = shown
                if 'remaining_amount' in row:
                    row['remaining_amount'] = 0.0
                if 'effective_amount' in row:
                    row['effective_amount'] = shown
                if 'unit_price' in row and row.get('unit_price') is not None:
                    row['unit_price'] = shown
                day_total += shown
            block['total'] = day_total
    return masked


def _is_admin_user(user: str) -> bool:
    name = sanitize_text(user or '')
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    return name in admin_aliases


def _can_manage_shared_expense(user: str, expense: SharedExpense) -> bool:
    u = sanitize_text(user or '')
    return _is_admin_user(u) or u == (expense.paid_by or '')


def _is_known_family_member(user: str) -> bool:
    name = sanitize_text(user or '').strip()
    if not name:
        return False
    if _is_admin_user(name):
        return True
    if FamilyMember.query.filter_by(name=name).first() is not None:
        return True

    cfg_members = current_app.config.get('HOMEHUB_CONFIG', {}).get('family_members') or []
    config_names: set[str] = set()
    for item in cfg_members:
        if isinstance(item, dict):
            raw = sanitize_text(item.get('name', '')).strip()
        else:
            raw = sanitize_text(str(item or '')).strip()
        if raw:
            config_names.add(raw)

    return name in config_names


def _get_family_member_names() -> list[str]:
    names: set[str] = set()
    for member in FamilyMember.query.all():
        clean = sanitize_text(member.name or '').strip()
        if clean:
            names.add(clean)

    cfg_members = current_app.config.get('HOMEHUB_CONFIG', {}).get('family_members') or []
    for item in cfg_members:
        if isinstance(item, dict):
            raw = sanitize_text(item.get('name', '')).strip()
        else:
            raw = sanitize_text(str(item or '')).strip()
        if raw:
            names.add(raw)

    admin_name = sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator')).strip()
    if admin_name:
        names.add(admin_name)
    names.add('Administrator')

    return sorted(names, key=lambda n: n.lower())


def _month_start_end(y: int, m: int) -> tuple[date, date]:
    month_start = date(y, m, 1)
    last_day = _calendar.monthrange(y, m)[1]
    month_end = date(y, m, last_day)
    return month_start, month_end


def _parse_year_month_query() -> tuple[int, int]:
    today = date.today()
    y, m = today.year, today.month
    year_q = request.args.get('year')
    month_q = request.args.get('month')
    try:
        if year_q is not None:
            y = int(year_q)
        if month_q is not None:
            m = int(month_q)
    except (ValueError, TypeError):
        current_app.logger.warning('Invalid year/month query params', extra={'year': year_q, 'month': month_q})
        y, m = today.year, today.month
    if m < 1 or m > 12:
        raise ValueError('month must be between 1 and 12')
    return y, m


def _entry_remaining_amount(entry: ExpenseEntry) -> float:
    amount = float(entry.amount or 0)
    paid = float(getattr(entry, 'amount_paid', 0) or 0)
    return max(amount - paid, 0.0)


def _entry_paid_amount(entry: ExpenseEntry) -> float:
    amount = max(float(entry.amount or 0), 0.0)
    if not entry.recurring_id:
        return amount
    paid = max(float(getattr(entry, 'amount_paid', 0) or 0), 0.0)
    return min(paid, amount)


def _entry_due_amount(entry: ExpenseEntry) -> float:
    if not entry.recurring_id:
        return 0.0
    return _entry_remaining_amount(entry)


def _expense_breakdown_for_entries(entries: list[ExpenseEntry]) -> dict[str, float]:
    charged_total = 0.0
    paid_total = 0.0
    due_total = 0.0
    for entry in entries:
        amount = max(float(entry.amount or 0), 0.0)
        charged_total += amount
        paid_total += _entry_paid_amount(entry)
        due_total += _entry_due_amount(entry)
    return {
        'charged_total': charged_total,
        'paid_total': paid_total,
        'due_total': due_total,
    }


def _recalculate_recurring_series_balances(recurring_id: int) -> None:
    """Rebuild monthly amounts from base charge + unpaid carryover.

    This prevents historical carryover inflation when older entries are paid later.
    """
    rule = RecurringExpense.query.get(recurring_id)
    if not rule:
        return

    entries = (
        ExpenseEntry.query
        .filter(ExpenseEntry.recurring_id == recurring_id)
        .order_by(ExpenseEntry.date.asc(), ExpenseEntry.id.asc())
        .all()
    )
    if not entries:
        return

    qty = float(rule.default_quantity or 1.0)
    if qty <= 0:
        qty = 1.0
    base_amount = max(float(rule.unit_price or 0.0) * qty, 0.0)

    carryover = max(float(getattr(rule, 'starting_balance', 0.0) or 0.0), 0.0)
    prev_entry_id: int | None = None

    for idx, row in enumerate(entries):
        paid = max(float(getattr(row, 'amount_paid', 0.0) or 0.0), 0.0)
        scheduled_amount = base_amount + carryover
        row.amount = max(scheduled_amount, paid)
        row.amount_paid = min(paid, row.amount)

        # Track where this row's balance originated for UI/debug visibility.
        if idx == 0:
            row.carryover_from_entry_id = None
        else:
            row.carryover_from_entry_id = prev_entry_id if carryover > 0.0001 else None

        remaining = max(float(row.amount or 0.0) - float(row.amount_paid or 0.0), 0.0)
        carryover = remaining
        prev_entry_id = row.id


def _add_amount(bucket: dict[str, float], key: str, amount: float) -> None:
    clean_key = (key or '').strip()
    if not clean_key:
        return
    bucket[clean_key] = float(bucket.get(clean_key, 0.0)) + float(amount or 0.0)


def _aggregate_month_accounting(
    items: list[dict],
    member_filter: str | None = None,
    family_only: bool = True,
) -> dict:
    member_filter = (member_filter or '').strip()

    income_paid_total = 0.0
    income_due_total = 0.0
    expense_charged_total = 0.0
    expense_paid_total = 0.0
    expense_due_total = 0.0

    income_paid_by_member: dict[str, float] = {}
    income_due_by_member: dict[str, float] = {}
    expense_paid_by_member: dict[str, float] = {}
    expense_due_by_member: dict[str, float] = {}

    members_rollup: dict[str, dict[str, float]] = {}

    def _member_bucket(name: str) -> dict[str, float]:
        clean = (name or '').strip()
        if not clean:
            clean = 'Unknown'
        if clean not in members_rollup:
            members_rollup[clean] = {
                'income_paid': 0.0,
                'income_due': 0.0,
                'expense_charged': 0.0,
                'expense_paid': 0.0,
                'expense_due': 0.0,
            }
        return members_rollup[clean]

    for item in items:
        include_in_family_total = bool(item.get('include_in_family_total', True))
        if family_only and not include_in_family_total:
            continue

        entry_type = (item.get('entry_type') or '').strip()
        paid_amount = max(float(item.get('paid_amount', 0.0) or 0.0), 0.0)
        due_amount = max(float(item.get('due_amount', 0.0) or 0.0), 0.0)
        charged_amount = max(float(item.get('charged_amount', 0.0) or 0.0), 0.0)

        if entry_type == 'income':
            recipient = (item.get('recipient') or '').strip()
            if member_filter and recipient != member_filter:
                continue
            income_paid_total += paid_amount
            income_due_total += due_amount
            _add_amount(income_paid_by_member, recipient, paid_amount)
            _add_amount(income_due_by_member, recipient, due_amount)
            member_row = _member_bucket(recipient)
            member_row['income_paid'] += paid_amount
            member_row['income_due'] += due_amount
            continue

        if entry_type in {'expense', 'debt_due'}:
            payer = (item.get('paid_by') or item.get('payer') or item.get('debtor') or '').strip()
            if member_filter and payer != member_filter:
                continue
            expense_charged_total += charged_amount
            expense_paid_total += paid_amount
            expense_due_total += due_amount
            _add_amount(expense_paid_by_member, payer, paid_amount)
            _add_amount(expense_due_by_member, payer, due_amount)
            member_row = _member_bucket(payer)
            member_row['expense_charged'] += charged_amount
            member_row['expense_paid'] += paid_amount
            member_row['expense_due'] += due_amount

    members = []
    for name, vals in sorted(members_rollup.items(), key=lambda kv: kv[0].lower()):
        income_expected = vals['income_paid'] + vals['income_due']
        expense_expected = vals['expense_paid'] + vals['expense_due']
        members.append({
            'name': name,
            'income_paid': round(vals['income_paid'], 2),
            'income_due': round(vals['income_due'], 2),
            'income_expected': round(income_expected, 2),
            'expense_charged': round(vals['expense_charged'], 2),
            'expense_paid': round(vals['expense_paid'], 2),
            'expense_due': round(vals['expense_due'], 2),
            'expense_expected': round(expense_expected, 2),
            'net_paid': round(vals['income_paid'] - vals['expense_paid'], 2),
            'net_expected': round(income_expected - expense_expected, 2),
        })

    income_expected_total = income_paid_total + income_due_total
    expense_expected_total = expense_paid_total + expense_due_total

    return {
        'income': {
            'paid_total': income_paid_total,
            'due_total': income_due_total,
            'expected_total': income_expected_total,
            'paid_by_member': income_paid_by_member,
            'due_by_member': income_due_by_member,
        },
        'expenses': {
            'charged_total': expense_charged_total,
            'paid_total': expense_paid_total,
            'due_total': expense_due_total,
            'expected_total': expense_expected_total,
            'paid_by_member': expense_paid_by_member,
            'due_by_member': expense_due_by_member,
        },
        'net': {
            'paid_total': income_paid_total - expense_paid_total,
            'expected_total': income_expected_total - expense_expected_total,
            'due_total': income_due_total - expense_due_total,
        },
        'members': members,
    }


def _collapse_shared_bill_items(items: list[dict]) -> list[dict]:
    """Collapse split recurring household bills into a single display row.

    The underlying entries remain separate for payment/ledger tracking; this only
    creates a cleaner view model for calendar/day/month history.
    """
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    passthrough: list[dict] = []

    for item in items:
        entry_type = (item.get('entry_type') or '').strip()
        is_candidate = (
            entry_type == 'expense'
            and bool(item.get('recurring'))
            and bool(item.get('include_in_family_total', True))
            and not bool(item.get('is_projected', False))
            and bool((item.get('title') or '').strip())
            and bool((item.get('date') or '').strip())
        )
        if not is_candidate:
            passthrough.append(item)
            continue

        shared_group_id = (item.get('shared_group_id') or '').strip()
        if shared_group_id:
            key = (
                (item.get('date') or '').strip(),
                f"group:{shared_group_id}",
                '',
            )
        else:
            key = (
                (item.get('date') or '').strip(),
                (item.get('title') or '').strip().lower(),
                (item.get('category') or '').strip().lower(),
            )
        grouped[key].append(item)

    collapsed: list[dict] = []
    for _, rows in grouped.items():
        payers = {(r.get('payer') or r.get('paid_by') or '').strip() for r in rows if (r.get('payer') or r.get('paid_by') or '').strip()}
        if len(rows) < 2 or len(payers) < 2:
            collapsed.extend(rows)
            continue

        rows_sorted = sorted(rows, key=lambda r: ((r.get('payer') or '').lower(), str(r.get('id') or '')))
        charged_total = sum(float(r.get('charged_amount', r.get('amount', 0)) or 0.0) for r in rows_sorted)
        paid_total = sum(float(r.get('paid_amount', r.get('amount_paid', 0)) or 0.0) for r in rows_sorted)
        due_total = sum(float(r.get('due_amount', r.get('remaining_amount', 0)) or 0.0) for r in rows_sorted)

        participants = []
        for r in rows_sorted:
            charged = float(r.get('charged_amount', r.get('amount', 0)) or 0.0)
            paid = float(r.get('paid_amount', r.get('amount_paid', 0)) or 0.0)
            due = float(r.get('due_amount', r.get('remaining_amount', 0)) or 0.0)
            participants.append({
                'entry_id': r.get('id'),
                'payer': (r.get('payer') or r.get('paid_by') or '').strip(),
                'charged_amount': charged,
                'paid_amount': paid,
                'due_amount': due,
                'recurring_id': r.get('recurring_id'),
                'paid': bool(due <= 0.0001),
            })

        first = rows_sorted[0]
        shared_key = f"shared-{first.get('date')}-{(first.get('title') or '').strip().lower()}-{(first.get('category') or '').strip().lower()}"
        collapsed.append({
            'entry_type': 'shared_bill',
            'is_shared_bill': True,
            'id': shared_key,
            'date': first.get('date'),
            'title': first.get('title'),
            'category': first.get('category'),
            'shared_group_id': first.get('shared_group_id'),
            'amount': due_total if due_total > 0.0001 else paid_total,
            'charged_amount': charged_total,
            'paid_amount': paid_total,
            'due_amount': due_total,
            'effective_amount': paid_total + due_total,
            'paid': bool(due_total <= 0.0001),
            'recurring': True,
            'include_in_family_total': True,
            'participants': participants,
            'shared_bill_key': shared_key,
            'is_projected': False,
        })

    merged = passthrough + collapsed
    merged.sort(key=lambda row: ((row.get('date') or ''), str(row.get('id') or '')), reverse=True)
    return merged


def _estimate_monthly_principal_for_debt(debt_id: int, fallback: float = 0.0) -> float:
    window_start = date.today() - timedelta(days=90)
    recent = DebtPayment.query.filter(
        DebtPayment.debt_id == debt_id,
        DebtPayment.payment_date >= window_start,
    ).all()
    if recent:
        principal_total = sum(float((p.principal if p.principal is not None else p.amount_paid) or 0) for p in recent)
        monthly = principal_total / 3.0
        if monthly > 0:
            return monthly

    last_payment = (
        DebtPayment.query
        .filter(DebtPayment.debt_id == debt_id)
        .order_by(DebtPayment.payment_date.desc(), DebtPayment.id.desc())
        .first()
    )
    if last_payment:
        principal = float((last_payment.principal if last_payment.principal is not None else last_payment.amount_paid) or 0)
        if principal > 0:
            return principal

    return max(float(fallback or 0), 0.0)


def _estimate_payoff_projection(balance: float, monthly_principal: float) -> tuple[int | None, str | None]:
    balance = max(float(balance or 0), 0.0)
    monthly_principal = max(float(monthly_principal or 0), 0.0)
    if balance <= 0:
        return 0, date.today().isoformat()
    if monthly_principal <= 0:
        return None, None

    months = int(math.ceil(balance / monthly_principal))
    estimated_days = months * 30
    payoff_date = (date.today() + timedelta(days=estimated_days)).isoformat()
    return months, payoff_date


def _months_until(target_date: date | None, from_date: date | None = None) -> int | None:
    if not target_date:
        return None
    from_date = from_date or date.today()
    months = (target_date.year - from_date.year) * 12 + (target_date.month - from_date.month)
    if target_date.day > from_date.day:
        months += 1
    return max(months, 1)


def _amortized_monthly_payment(balance: float, apr_percent: float, months: int) -> float | None:
    balance = max(float(balance or 0), 0.0)
    months = int(months or 0)
    if balance <= 0 or months <= 0:
        return None
    apr = max(float(apr_percent or 0), 0.0)
    monthly_rate = apr / 100.0 / 12.0
    if monthly_rate <= 0:
        return balance / months
    denom = 1 - math.pow(1 + monthly_rate, -months)
    if denom <= 0:
        return None
    return balance * monthly_rate / denom


def _payoff_months_from_payment(balance: float, apr_percent: float, monthly_payment: float) -> int | None:
    balance = max(float(balance or 0), 0.0)
    payment = max(float(monthly_payment or 0), 0.0)
    if balance <= 0:
        return 0
    if payment <= 0:
        return None

    apr = max(float(apr_percent or 0), 0.0)
    monthly_rate = apr / 100.0 / 12.0
    if monthly_rate <= 0:
        return int(math.ceil(balance / payment))

    if payment <= balance * monthly_rate:
        return None

    try:
        months = -math.log(1 - (monthly_rate * balance / payment)) / math.log(1 + monthly_rate)
    except (ValueError, ZeroDivisionError):
        return None
    if months < 0:
        return None
    return int(math.ceil(months))


def _expected_balance_after_payments(balance: float, apr_percent: float, monthly_payment: float, periods: int) -> float:
    remaining = max(float(balance or 0), 0.0)
    apr = max(float(apr_percent or 0), 0.0)
    payment = max(float(monthly_payment or 0), 0.0)
    periods = max(int(periods or 0), 0)
    if remaining <= 0 or payment <= 0 or periods <= 0:
        return remaining

    monthly_rate = apr / 100.0 / 12.0
    for _ in range(periods):
        if remaining <= 0:
            return 0.0
        interest = remaining * monthly_rate if monthly_rate > 0 else 0.0
        principal_paid = payment - interest
        if principal_paid <= 0:
            return remaining
        remaining = max(remaining - principal_paid, 0.0)
    return remaining


def _debt_priority_metrics(debt: DebtAccount, ref_date: date | None = None) -> dict:
    ref_date = ref_date or date.today()
    created_date = (debt.created_at.date() if getattr(debt, 'created_at', None) else ref_date)
    created_date = min(created_date, ref_date)

    balance = max(float(debt.current_balance or 0), 0.0)
    apr = max(float(debt.interest_rate or 0), 0.0)
    due_date = debt.payoff_date
    due_day = int((due_date.day if due_date else created_date.day) or 1)

    months_to_due = _months_until(due_date, ref_date)
    scheduled_payment = _amortized_monthly_payment(balance, apr, months_to_due or 0) if months_to_due else None
    if scheduled_payment is None or scheduled_payment <= 0:
        fallback_principal = _estimate_monthly_principal_for_debt(debt.id)
        fallback_interest = (apr / 100.0 / 12.0) * balance
        scheduled_payment = max(fallback_principal + fallback_interest, 0.0)

    payments_remaining = _payoff_months_from_payment(balance, apr, scheduled_payment)

    months_elapsed = (ref_date.year - created_date.year) * 12 + (ref_date.month - created_date.month)
    if ref_date.day < due_day:
        months_elapsed -= 1
    months_elapsed = max(months_elapsed, 0)

    original_balance = max(float(debt.initial_amount or 0), balance)
    expected_balance_today = _expected_balance_after_payments(original_balance, apr, scheduled_payment, months_elapsed)
    past_due_amount = max(balance - expected_balance_today, 0.0)
    missed_payments_estimate = int(math.ceil(past_due_amount / scheduled_payment)) if scheduled_payment > 0 and past_due_amount > 0 else 0

    if ref_date.day <= due_day:
        next_due_date = date(ref_date.year, ref_date.month, min(due_day, _calendar.monthrange(ref_date.year, ref_date.month)[1]))
    else:
        ny = ref_date.year + (1 if ref_date.month == 12 else 0)
        nm = 1 if ref_date.month == 12 else ref_date.month + 1
        next_due_date = date(ny, nm, min(due_day, _calendar.monthrange(ny, nm)[1]))

    days_to_target_due = (due_date - ref_date).days if due_date else None

    score = 0.0
    if past_due_amount > 0:
        score += 50 + min((past_due_amount / max(scheduled_payment, 1.0)) * 10, 30)
    score += min(apr, 35)
    if days_to_target_due is not None:
        if days_to_target_due < 0:
            score += 25
        elif days_to_target_due <= 30:
            score += 15
        elif days_to_target_due <= 90:
            score += 8
    score += min(balance / 1000.0, 20)

    if score >= 70:
        priority_band = 'high'
    elif score >= 40:
        priority_band = 'medium'
    else:
        priority_band = 'low'

    return {
        'scheduled_payment': scheduled_payment,
        'payments_remaining': payments_remaining,
        'past_due_amount': past_due_amount,
        'missed_payments_estimate': missed_payments_estimate,
        'priority_score': round(score, 2),
        'priority_band': priority_band,
        'next_due_date': next_due_date,
        'days_to_target_due': days_to_target_due,
        'due_day': due_day,
    }


def _family_income_expenses_for_month(y: int, m: int) -> tuple[float, float]:
    snapshot = _monthly_finance_snapshot(y, m)
    return (
        float(snapshot['family']['income']['expected_total']),
        float(snapshot['family']['expenses']['expected_total']),
    )


def _generate_recurring_entries_until(today: date | None = None, auto_commit: bool = True) -> None:
    today = today or date.today()
    recs = RecurringExpense.query.all()
    recalculation_ids: set[int] = set()
    for r in recs:
        recalculation_ids.add(int(r.id))
        start = r.start_date or today
        # Generate from rule start date; don't clamp to effective_from so rule owns entire range
        base_day = (r.start_date or today).day
        last = r.last_generated_date

        def next_date(d: date) -> date:
            if r.frequency == 'daily':
                return d + timedelta(days=1)
            if r.frequency == 'weekly':
                return d + timedelta(weeks=1)
            if r.frequency == 'biweekly':
                return d + timedelta(weeks=2)
            mode = getattr(r, 'monthly_mode', 'day_of_month') or 'day_of_month'
            if mode == 'calendar':
                ny = d.year + (1 if d.month == 12 else 0)
                nm = 1 if d.month == 12 else d.month + 1
                return date(ny, nm, 1)
            else:
                ny = d.year + (1 if d.month == 12 else 0)
                nm = 1 if d.month == 12 else d.month + 1
                last_dom = _calendar.monthrange(ny, nm)[1]
                day = min(base_day, last_dom)
                return date(ny, nm, day)

        if last is None or (last and last < start):
            if r.frequency in ('daily', 'weekly', 'biweekly'):
                d = start
            else:
                mode = getattr(r, 'monthly_mode', 'day_of_month') or 'day_of_month'
                if mode == 'calendar':
                    if start.day == 1:
                        d = start
                    else:
                        ny = start.year + (1 if start.month == 12 else 0)
                        nm = 1 if start.month == 12 else start.month + 1
                        d = date(ny, nm, 1)
                else:
                    d = start
        else:
            d = next_date(last)

        while d <= today and (not r.end_date or d <= r.end_date):
            exists = ExpenseEntry.query.filter_by(date=d, recurring_id=r.id).first()
            if not exists:
                qty = r.default_quantity or 1.0
                base_amt = (r.unit_price or 0.0) * qty
                prev_entry = (
                    ExpenseEntry.query
                    .filter(ExpenseEntry.recurring_id == r.id, ExpenseEntry.date < d)
                    .order_by(ExpenseEntry.date.desc(), ExpenseEntry.id.desc())
                    .first()
                )
                prev_paid = float(getattr(prev_entry, 'amount_paid', 0.0) or 0.0) if prev_entry else 0.0
                prev_amt = float(prev_entry.amount or 0.0) if prev_entry else 0.0
                carryover = max(prev_amt - prev_paid, 0.0) if prev_entry else 0.0
                amt = base_amt + carryover
                created = ExpenseEntry(
                    date=d,
                    title=r.title,
                    category=getattr(r, 'category', None),
                    unit_price=r.unit_price,
                    quantity=qty,
                    amount=amt,
                    amount_paid=0.0,
                    payer=r.creator,
                    include_in_family_total=bool(getattr(r, 'include_in_family_total', True)),
                    recurring_id=r.id,
                    shared_group_id=getattr(r, 'shared_group_id', None),
                    carryover_from_entry_id=(prev_entry.id if (prev_entry and carryover > 0) else None),
                )
                db.session.add(created)
                if carryover > 0:
                    log_activity(
                        actor=r.creator or 'Family',
                        action='money.bill.carryover',
                        summary=f"carried over unpaid balance for {r.title}: ${carryover:.2f} (new total ${amt:.2f})",
                        entity_type='expense_entry',
                        entity_id=created.id,
                        visibility='family',
                        metadata={
                            'recurring_id': r.id,
                            'carryover_amount': round(carryover, 2),
                            'new_total': round(amt, 2),
                            'due_date': d.isoformat(),
                            'previous_due_date': prev_entry.date.isoformat() if prev_entry and prev_entry.date else None,
                        },
                    )
            r.last_generated_date = d
            d = next_date(d)

    for recurring_id in sorted(recalculation_ids):
        _recalculate_recurring_series_balances(recurring_id)
    if auto_commit:
        db.session.commit()


def _next_monthly_date(base_date: date, target_day: int) -> date:
    ny = base_date.year + (1 if base_date.month == 12 else 0)
    nm = 1 if base_date.month == 12 else base_date.month + 1
    last_dom = _calendar.monthrange(ny, nm)[1]
    return date(ny, nm, min(max(target_day, 1), last_dom))


def _initial_income_due_date(schedule: RecurringIncome) -> date:
    start = schedule.start_date
    if schedule.frequency == 'monthly':
        target_day = schedule.payday_day or start.day
        last_dom = _calendar.monthrange(start.year, start.month)[1]
        due = date(start.year, start.month, min(max(target_day, 1), last_dom))
        if due < start:
            return _next_monthly_date(due, target_day)
        return due
    return start


def _next_income_due_date(schedule: RecurringIncome, current_due: date) -> date:
    freq = (schedule.frequency or 'biweekly').lower()
    if freq == 'weekly':
        return current_due + timedelta(days=7)
    if freq == 'biweekly':
        return current_due + timedelta(days=14)
    target_day = schedule.payday_day or schedule.start_date.day
    return _next_monthly_date(current_due, target_day)


def _parse_optional_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except Exception:
        return None


def _parse_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _next_due_on_or_after(schedule: RecurringIncome, ref_date: date) -> date:
    if schedule.last_generated_date:
        due = _next_income_due_date(schedule, schedule.last_generated_date)
    else:
        due = _initial_income_due_date(schedule)

    safety = 0
    while due < ref_date and safety < 5000:
        due = _next_income_due_date(schedule, due)
        safety += 1
    return due


def _generate_recurring_income_until(today: date | None = None) -> None:
    today = today or date.today()
    schedules = RecurringIncome.query.filter_by(active=True).all()
    changed = False

    for schedule in schedules:
        if not schedule.start_date:
            continue
        if schedule.end_date and schedule.end_date < schedule.start_date:
            continue

        if schedule.last_generated_date:
            due = _next_income_due_date(schedule, schedule.last_generated_date)
        else:
            due = _initial_income_due_date(schedule)

        while due <= today and (not schedule.end_date or due <= schedule.end_date):
            exists = IncomeEntry.query.filter_by(recurring_income_id=schedule.id, date=due).first()
            if not exists:
                db.session.add(IncomeEntry(
                    date=due,
                    title=schedule.title,
                    source=schedule.source,
                    amount=float(schedule.expected_amount or 0),
                    recipient=schedule.recipient,
                    recurring_income_id=schedule.id,
                    auto_generated=True,
                    needs_confirmation=True,
                    include_in_family_total=bool(getattr(schedule, 'include_in_family_total', True)),
                ))
                changed = True
            schedule.last_generated_date = due
            due = _next_income_due_date(schedule, due)

    if changed:
        db.session.commit()
    else:
        try:
            db.session.flush()
        except Exception:
            db.session.rollback()


def _dedupe_income_entries() -> int:
    rows = IncomeEntry.query.order_by(IncomeEntry.id.asc()).all()
    seen: set[tuple] = set()
    to_delete: list[int] = []

    for row in rows:
        key = (
            row.date.isoformat() if row.date else None,
            (row.title or '').strip().lower(),
            (row.source or '').strip().lower(),
            round(float(row.amount or 0), 2),
            (row.recipient or '').strip().lower(),
            int(row.recurring_income_id or 0),
        )
        if key in seen:
            to_delete.append(row.id)
        else:
            seen.add(key)

    if not to_delete:
        return 0

    for row_id in to_delete:
        row = IncomeEntry.query.get(row_id)
        if row:
            db.session.delete(row)
    db.session.commit()
    return len(to_delete)


def _dedupe_recurring_income_schedules() -> int:
    rows = RecurringIncome.query.order_by(RecurringIncome.id.asc()).all()
    grouped: dict[tuple[str, str, str], list[RecurringIncome]] = {}
    disabled = 0

    for row in rows:
        key = (
            (row.recipient or '').strip().lower(),
            (row.source or '').strip().lower(),
            (row.title or '').strip().lower(),
        )
        grouped.setdefault(key, []).append(row)

    for _, schedules in grouped.items():
        active_rows = [r for r in schedules if r.active]
        if len(active_rows) <= 1:
            continue

        # Keep the newest active schedule as the canonical one and disable older actives.
        active_rows.sort(key=lambda r: (r.id or 0), reverse=True)
        for duplicate in active_rows[1:]:
            duplicate.active = False
            disabled += 1

    if disabled:
        db.session.commit()
    return disabled


def _load_expense_settings() -> dict:
    settings = {'currency': '$', 'categories': []}
    try:
        rows = db.session.execute(db.text("SELECT key, value FROM app_setting WHERE key IN ('categories')"))
        data = {k: v for k, v in rows}
        if data.get('categories'):
            settings['categories'] = [c.strip() for c in data['categories'].split(',') if c.strip()]
    except Exception:
        # Best-effort: fall back to default settings but log for diagnostics
        current_app.logger.exception('Failed to load expense settings; using defaults')
    return settings


def _manual_hub_savings_total() -> float:
    total = (
        db.session.query(db.func.coalesce(db.func.sum(GoalContribution.amount), 0.0))
        .join(FinancialGoal, FinancialGoal.id == GoalContribution.goal_id)
        .filter(FinancialGoal.active == True)
        .filter(db.func.lower(FinancialGoal.category) == 'savings')
        .filter(GoalContribution.notes.like(f"{HUB_SAVINGS_NOTE_PREFIX}%"))
        .scalar()
    )
    return float(total or 0.0)


def _collect_projected_income_occurrences(
    period_start: date,
    period_end: date,
    existing_incomes: list[IncomeEntry] | None = None,
) -> tuple[float, list[dict]]:
    schedules = RecurringIncome.query.filter_by(active=True).all()
    if existing_incomes is None:
        existing_incomes = IncomeEntry.query.filter(
            IncomeEntry.date >= period_start,
            IncomeEntry.date <= period_end,
        ).all()

    existing_keys = {
        (int(row.recurring_income_id or 0), row.date.isoformat() if row.date else None)
        for row in existing_incomes
        if row.recurring_income_id and row.date
    }

    projected_total = 0.0
    projected_items: list[dict] = []

    for schedule in schedules:
        if not schedule.start_date:
            continue
        if schedule.end_date and schedule.end_date < period_start:
            continue

        due = _next_due_on_or_after(schedule, period_start)
        safety = 0
        while due <= period_end and safety < 5000:
            if schedule.end_date and due > schedule.end_date:
                break

            key = (int(schedule.id or 0), due.isoformat())
            if key not in existing_keys:
                amount = float(schedule.expected_amount or 0)
                include_in_family_total = bool(getattr(schedule, 'include_in_family_total', True))
                if include_in_family_total:
                    projected_total += amount
                projected_items.append({
                    'entry_type': 'income',
                    'id': f"projected-{schedule.id}-{due.isoformat()}",
                    'date': due.isoformat(),
                    'title': schedule.title,
                    'amount': amount,
                    'charged_amount': amount,
                    'paid_amount': 0.0,
                    'due_amount': amount,
                    'source': schedule.source,
                    'recipient': schedule.recipient,
                    'paid_by': schedule.recipient,
                    'include_in_family_total': include_in_family_total,
                    'needs_confirmation': True,
                    'is_projected': True,
                    'projected_from_schedule': True,
                })

            due = _next_income_due_date(schedule, due)
            safety += 1

    return projected_total, projected_items


def _build_month_history(y: int, m: int) -> dict:
    month_start, month_end = _month_start_end(y, m)

    expenses = (
        ExpenseEntry.query
        .filter(ExpenseEntry.date >= month_start, ExpenseEntry.date <= month_end)
        .order_by(ExpenseEntry.date.desc(), ExpenseEntry.id.desc())
        .all()
    )
    incomes = (
        IncomeEntry.query
        .filter(IncomeEntry.date >= month_start, IncomeEntry.date <= month_end)
        .order_by(IncomeEntry.date.desc(), IncomeEntry.id.desc())
        .all()
    )

    items: list[dict] = []
    expenses_total = 0.0
    expenses_charged_total = 0.0
    expenses_paid_total = 0.0
    expenses_due_total = 0.0
    income_total = 0.0
    income_paid_total = 0.0
    income_due_total = 0.0
    today = date.today()

    for row in expenses:
        amount = float(row.amount or 0)
        amount_paid = float(getattr(row, 'amount_paid', 0.0) or 0.0)
        remaining_amount = max(amount - amount_paid, 0.0)
        paid_amount = _entry_paid_amount(row)
        due_amount = _entry_due_amount(row)
        effective_amount = paid_amount + due_amount

        expenses_total += effective_amount
        expenses_charged_total += amount
        expenses_paid_total += paid_amount
        expenses_due_total += due_amount
        items.append({
            'entry_type': 'expense',
            'id': row.id,
            'date': row.date.isoformat() if row.date else None,
            'title': row.title,
            'amount': amount,
            'charged_amount': amount,
            'paid_amount': paid_amount,
            'due_amount': due_amount,
            'effective_amount': effective_amount,
            'amount_paid': amount_paid,
            'remaining_amount': remaining_amount,
            'paid': bool(amount > 0 and remaining_amount <= 0.0001),
            'category': row.category,
            'payer': row.payer,
            'paid_by': row.payer,
            'recurring': bool(row.recurring_id),
            'recurring_id': row.recurring_id,
            'shared_group_id': getattr(row, 'shared_group_id', None),
            'include_in_family_total': bool(getattr(row, 'include_in_family_total', True)),
            'is_projected': False,
        })

    for row in incomes:
        amount = float(row.amount or 0)
        paid_amount = amount if not bool(row.needs_confirmation) else 0.0
        due_amount = amount if bool(row.needs_confirmation) else 0.0
        income_total += amount
        income_paid_total += paid_amount
        income_due_total += due_amount
        items.append({
            'entry_type': 'income',
            'id': row.id,
            'date': row.date.isoformat() if row.date else None,
            'title': row.title,
            'amount': amount,
            'charged_amount': amount,
            'paid_amount': paid_amount,
            'due_amount': due_amount,
            'source': row.source,
            'category': getattr(row, 'category', None),
            'recipient': row.recipient,
            'paid_by': row.recipient,
            'include_in_family_total': bool(row.include_in_family_total),
            'needs_confirmation': bool(row.needs_confirmation),
            'is_projected': bool(row.date and row.date > today and row.needs_confirmation),
        })

    projected_income_total, projected_items = _collect_projected_income_occurrences(
        month_start,
        month_end,
        existing_incomes=incomes,
    )
    income_total += projected_income_total
    income_due_total += projected_income_total
    items.extend(projected_items)

    active_debts = DebtAccount.query.filter_by(active=True).all()
    for debt in active_debts:
        metrics = _debt_priority_metrics(debt, ref_date=date.today())
        scheduled_payment = float(metrics.get('scheduled_payment') or 0)
        if scheduled_payment <= 0:
            continue
        due_day = int(metrics.get('due_day') or 1)
        last_dom = _calendar.monthrange(y, m)[1]
        due_date = date(y, m, min(due_day, last_dom))
        if due_date < month_start or due_date > month_end:
            continue

        paid_this_month = (
            db.session.query(db.func.coalesce(db.func.sum(DebtPayment.amount_paid), 0.0))
            .filter(
                DebtPayment.debt_id == debt.id,
                DebtPayment.payment_date >= month_start,
                DebtPayment.payment_date <= month_end,
            )
            .scalar()
        ) or 0.0

        amount_due = max(scheduled_payment - float(paid_this_month or 0), 0.0)
        if amount_due <= 0 and float(metrics.get('past_due_amount') or 0) <= 0:
            continue

        expenses_total += float(paid_this_month or 0) + amount_due
        expenses_charged_total += scheduled_payment
        expenses_paid_total += float(paid_this_month or 0)
        expenses_due_total += amount_due
        items.append({
            'entry_type': 'debt_due',
            'id': f"debt-{debt.id}-{due_date.isoformat()}",
            'date': due_date.isoformat(),
            'title': debt.description,
            'amount': amount_due,
            'charged_amount': scheduled_payment,
            'paid_amount': float(paid_this_month or 0),
            'due_amount': amount_due,
            'effective_amount': amount_due,
            'scheduled_payment': scheduled_payment,
            'past_due_amount': float(metrics.get('past_due_amount') or 0),
            'missed_payments_estimate': int(metrics.get('missed_payments_estimate') or 0),
            'priority_score': float(metrics.get('priority_score') or 0),
            'priority_band': metrics.get('priority_band') or 'low',
            'apr': float(debt.interest_rate or 0),
            'debtor': debt.debtor,
            'paid_by': debt.debtor,
            'creditor': debt.creditor,
            'debt_id': debt.id,
            'paid': amount_due <= 0.0001,
            'include_in_family_total': True,
            'is_projected': True,
        })

    items.sort(key=lambda row: ((row.get('date') or ''), str(row.get('id') or '')), reverse=True)

    accounting = _aggregate_month_accounting(items)
    display_items = _collapse_shared_bill_items(items)

    return {
        'period': {'year': y, 'month': m},
        'items': display_items,
        'totals': {
            'income_total': income_total,
            'income_paid_total': income_paid_total,
            'income_due_total': income_due_total,
            'expenses_total': expenses_total,
            'expenses_charged_total': expenses_charged_total,
            'expenses_paid_total': expenses_paid_total,
            'expenses_due_total': expenses_due_total,
            'net_total': income_total - expenses_total,
            'net_paid_total': income_paid_total - expenses_paid_total,
            'net_due_total': income_due_total - expenses_due_total,
            'projected_income_total': projected_income_total,
        },
        'accounting': accounting,
    }


def _monthly_finance_snapshot(y: int, m: int, member: str | None = None) -> dict:
    """Single source-of-truth month accounting used by summary/collab/analytics."""
    history = _build_month_history(y, m)
    items = history.get('items', [])
    family_accounting = history.get('accounting') or _aggregate_month_accounting(items)

    clean_member = (member or '').strip()
    if clean_member:
        member_accounting = _aggregate_month_accounting(
            items,
            member_filter=clean_member,
            family_only=False,
        )
    else:
        member_accounting = {
            'income': {'paid_total': 0.0, 'due_total': 0.0, 'expected_total': 0.0, 'paid_by_member': {}, 'due_by_member': {}},
            'expenses': {'charged_total': 0.0, 'paid_total': 0.0, 'due_total': 0.0, 'expected_total': 0.0, 'paid_by_member': {}, 'due_by_member': {}},
            'net': {'paid_total': 0.0, 'expected_total': 0.0, 'due_total': 0.0},
            'members': [],
        }

    budget = FamilyBudget.query.filter_by(month=m, year=y).first()
    budget_categories = _budget_category_spent_map(y, m)
    budget_spent = float(sum(budget_categories.values()))
    budget_total = float(budget.total_amount) if budget else 0.0

    return {
        'period': {'year': y, 'month': m},
        'history': history,
        'family': family_accounting,
        'member': member_accounting,
        'budget': {
            'row': budget,
            'total': budget_total,
            'spent': budget_spent,
            'remaining': (budget_total - budget_spent) if budget else 0.0,
            'has_budget': bool(budget),
            'categories': budget_categories,
        },
    }


def _build_month_payload(y: int, m: int) -> dict:
    month_start, month_end = _month_start_end(y, m)

    q_entries = (
        ExpenseEntry.query
        .filter(ExpenseEntry.date >= month_start, ExpenseEntry.date <= month_end)
        .order_by(ExpenseEntry.date.asc(), ExpenseEntry.timestamp.asc())
        .all()
    )

    by_date: dict[str, dict] = {}
    total = 0.0
    per_payer: dict[str, float] = {}
    per_category: dict[str, float] = {}
    for e in q_entries:
        ds = e.date.strftime('%Y-%m-%d')
        by_date.setdefault(ds, {'total': 0.0, 'entries': []})
        amount = float(e.amount or 0)
        effective_amount = _entry_paid_amount(e) + _entry_due_amount(e)
        by_date[ds]['total'] += effective_amount
        total += effective_amount
        per_payer[e.payer or ''] = per_payer.get(e.payer or '', 0.0) + effective_amount
        if e.category:
            per_category[e.category] = per_category.get(e.category, 0.0) + effective_amount
        by_date[ds]['entries'].append({
            'id': e.id,
            'title': e.title,
            'category': e.category,
            'unit_price': float(e.unit_price) if e.unit_price is not None else None,
            'amount': amount,
            'charged_amount': amount,
            'paid_amount': _entry_paid_amount(e),
            'due_amount': _entry_due_amount(e),
            'effective_amount': effective_amount,
            'amount_paid': float(getattr(e, 'amount_paid', 0.0) or 0.0),
            'remaining_amount': max(float(e.amount or 0) - float(getattr(e, 'amount_paid', 0.0) or 0.0), 0.0),
            'quantity': float(e.quantity or 0) if e.quantity is not None else None,
            'recurring': bool(e.recurring_id),
            'recurring_id': e.recurring_id,
            'shared_group_id': getattr(e, 'shared_group_id', None),
            'paid': bool((float(e.amount or 0) > 0) and (max(float(e.amount or 0) - float(getattr(e, 'amount_paid', 0.0) or 0.0), 0.0) <= 0.0001)),
            'payer': e.payer or '',
            'paid_by': e.payer or '',
            'include_in_family_total': bool(getattr(e, 'include_in_family_total', True)),
        })

    active_debts = DebtAccount.query.filter_by(active=True).all()
    month_start, month_end = _month_start_end(y, m)
    for debt in active_debts:
        metrics = _debt_priority_metrics(debt, ref_date=date.today())
        scheduled_payment = float(metrics.get('scheduled_payment') or 0)
        if scheduled_payment <= 0:
            continue

        due_day = int(metrics.get('due_day') or 1)
        last_dom = _calendar.monthrange(y, m)[1]
        debt_due_date = date(y, m, min(due_day, last_dom))
        if debt_due_date < month_start or debt_due_date > month_end:
            continue

        paid_this_month = (
            db.session.query(db.func.coalesce(db.func.sum(DebtPayment.amount_paid), 0.0))
            .filter(
                DebtPayment.debt_id == debt.id,
                DebtPayment.payment_date >= month_start,
                DebtPayment.payment_date <= month_end,
            )
            .scalar()
        ) or 0.0

        amount_due = max(scheduled_payment - float(paid_this_month or 0), 0.0)
        if amount_due <= 0 and float(metrics.get('past_due_amount') or 0) <= 0:
            continue

        ds = debt_due_date.strftime('%Y-%m-%d')
        by_date.setdefault(ds, {'total': 0.0, 'entries': []})
        by_date[ds]['entries'].append({
            'id': f"debt-{debt.id}-{ds}",
            'entry_type': 'debt_due',
            'debt_id': debt.id,
            'title': debt.description,
            'category': 'Debt Payment',
            'unit_price': None,
            'amount': amount_due,
            'charged_amount': scheduled_payment,
            'paid_amount': float(paid_this_month or 0),
            'due_amount': amount_due,
            'scheduled_payment': scheduled_payment,
            'amount_paid': float(paid_this_month or 0),
            'remaining_amount': amount_due,
            'past_due_amount': float(metrics.get('past_due_amount') or 0),
            'missed_payments_estimate': int(metrics.get('missed_payments_estimate') or 0),
            'priority_score': float(metrics.get('priority_score') or 0),
            'priority_band': metrics.get('priority_band') or 'low',
            'apr': float(debt.interest_rate or 0),
            'quantity': None,
            'recurring': True,
            'recurring_id': None,
            'paid': amount_due <= 0.0001,
            'payer': debt.debtor or '',
            'paid_by': debt.debtor or '',
            'creditor': debt.creditor or '',
            'is_debt_due': True,
            'include_in_family_total': True,
        })

    for ds in list(by_date.keys()):
        day_rows = by_date.get(ds, {}).get('entries', [])
        if not day_rows:
            continue
        by_date[ds]['entries'] = _collapse_shared_bill_items(day_rows)

    top_category = None
    if per_category:
        top_category = max(per_category.items(), key=lambda kv: kv[1])[0]

    settings = _load_expense_settings()
    payload = {
        'by_date': by_date,
        'history': _build_month_history(y, m),
        'summary': {
            'total_this_month': total,
            'per_payer': per_payer,
            'per_category': per_category,
            'top_category': top_category,
        },
        'year': y,
        'month': m,
        'settings': settings,
    }
    return payload


@main_bp.route('/expenses', methods=['GET', 'POST'])
def expenses():
    today = date.today()

    try:
        selected_year = int(request.args.get('y') or today.year)
        selected_month = int(request.args.get('m') or today.month)
    except Exception:
        selected_year, selected_month = today.year, today.month

    _, selected_month_end = _month_start_end(selected_year, selected_month)
    generation_target = max(today, selected_month_end)

    # Ensure recurring entries are generated through the selected month so future views are complete.
    _generate_recurring_entries_until(generation_target)
    _generate_recurring_income_until(today)
    _dedupe_recurring_income_schedules()
    _dedupe_income_entries()

    if request.method == 'POST':
        form_type = request.form.get('form_type')
        if form_type == 'recurring':
            title = bleach.clean(request.form.get('title',''))
            unit_price = float(request.form.get('unit_price') or 0)
            default_quantity = float(request.form.get('default_quantity') or 1)
            frequency = bleach.clean(request.form.get('frequency','daily'))
            monthly_mode = bleach.clean(request.form.get('monthly_mode','day_of_month'))
            category = bleach.clean(request.form.get('category',''))
            starting_balance = float(request.form.get('starting_balance') or 0)
            start_date = request.form.get('start_date')
            end_date = request.form.get('end_date')
            creator = bleach.clean(request.form.get('creator',''))
            sd = datetime.strptime(start_date, '%Y-%m-%d').date() if start_date else date.today()
            ed = datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else None
            recurring_rule = RecurringExpense(
                title=title,
                unit_price=unit_price,
                default_quantity=default_quantity,
                frequency=frequency,
                monthly_mode=monthly_mode,
                category=category,
                start_date=sd,
                end_date=ed,
                starting_balance=max(starting_balance, 0.0),
                creator=creator,
                effective_from=sd,
            )
            db.session.add(recurring_rule)
            db.session.commit()

            bill_amount = float(unit_price or 0) * float(default_quantity or 1)
            log_activity(
                actor=creator or 'Family',
                action='money.bill.add',
                summary=f"added a household bill: {title} (${bill_amount:.2f})",
                entity_type='recurring_expense',
                entity_id=recurring_rule.id,
                visibility='family',
                metadata={
                    'category': category,
                    'frequency': frequency,
                    'start_date': sd.isoformat() if sd else None,
                    'starting_balance': round(max(starting_balance, 0.0), 2),
                },
            )

            flash('Recurring expense added.', 'success')
            y = request.args.get('y') or today.year
            m = request.args.get('m') or today.month
            sel = request.args.get('sel')
            return redirect(url_for('main.expenses', y=y, m=m, sel=sel))
        else:
            title = bleach.clean(request.form.get('title',''))
            amount = float(request.form.get('amount') or 0)
            category = bleach.clean(request.form.get('category') or '')
            payer = bleach.clean(request.form.get('payer') or '')
            is_personal = (request.form.get('is_personal') or '').lower() in ('1', 'true', 'on', 'yes')
            date_s = request.form.get('date')
            d = datetime.strptime(date_s, '%Y-%m-%d').date() if date_s else date.today()
            unit_price = request.form.get('unit_price'); quantity = request.form.get('quantity')
            up = float(unit_price) if unit_price else None
            q = float(quantity) if quantity else None
            category = _apply_expense_category_rules(title=title, description=title, fallback=category)
            entry = ExpenseEntry(
                date=d,
                title=title,
                category=category,
                unit_price=up,
                quantity=q,
                amount=amount,
                payer=payer,
                include_in_family_total=(not is_personal),
            )
            db.session.add(entry)
            _sync_budget_for_entry_date(d)
            db.session.commit()

            if not is_personal:
                log_activity(
                    actor=payer or 'Family',
                    action='money.expense.add',
                    summary=f"added an expense: {title} (${amount:.2f})",
                    entity_type='expense_entry',
                    entity_id=entry.id,
                    visibility='family',
                    metadata={'category': category, 'date': d.isoformat()},
                )
            flash('Expense added.', 'success')
            y = request.args.get('y') or d.year
            m = request.args.get('m') or d.month
            sel = request.args.get('sel') or d.strftime('%Y-%m-%d')
            return redirect(url_for('main.expenses', y=y, m=m, sel=sel))

    y, m = selected_year, selected_month

    payload = _build_month_payload(y, m)
    privacy_mode = _is_privacy_mode()
    if privacy_mode:
        payload = _privacy_mask_payload(payload)
    rules = RecurringExpense.query.order_by(RecurringExpense.timestamp.desc()).all()
    recurring_income_schedules = RecurringIncome.query.filter_by(active=True).order_by(RecurringIncome.created_at.desc()).all()
    recurring_income_cards = []
    for schedule in recurring_income_schedules:
        next_due = _next_due_on_or_after(schedule, today) if schedule.active else None
        pending_count = IncomeEntry.query.filter(
            IncomeEntry.recurring_income_id == schedule.id,
            IncomeEntry.needs_confirmation == True,
        ).count()
        recurring_income_cards.append({
            'id': schedule.id,
            'title': schedule.title,
            'source': schedule.source,
            'recipient': schedule.recipient,
            'expected_amount': float(schedule.expected_amount or 0),
            'frequency': schedule.frequency,
            'start_date': schedule.start_date,
            'next_due': next_due,
            'active': bool(schedule.active),
            'pending_count': int(pending_count),
            'created_by': schedule.created_by,
        })
    
    # Get all income entries for the requested month
    month_start, month_end = _month_start_end(y, m)
    income_entries = IncomeEntry.query.filter(
        IncomeEntry.date >= month_start,
        IncomeEntry.date <= month_end
    ).order_by(IncomeEntry.date.desc()).all()

    if privacy_mode:
        masked_rows = []
        for idx, row in enumerate(income_entries):
            fake = _privacy_bucket(f"income-entry:{row.id}:{idx}:{row.date}", 10000, 100000)
            masked_rows.append(SimpleNamespace(
                id=row.id,
                date=row.date,
                title=row.title,
                source=row.source,
                amount=fake,
                recipient=row.recipient,
                include_in_family_total=row.include_in_family_total,
                needs_confirmation=row.needs_confirmation,
            ))
        income_entries = masked_rows
    
    config = current_app.config['HOMEHUB_CONFIG']
    return render_template(
        'expenses.html',
        rules=rules,
        recurring_income_schedules=recurring_income_schedules,
        recurring_income_cards=recurring_income_cards,
        config=config,
        expenses_json=json.dumps(payload),
        income_entries=income_entries,
        privacy_mode=privacy_mode,
        today=today,
        now=datetime.utcnow(),
    )




@main_bp.route('/expenses/income/<int:income_id>/toggle-family', methods=['POST'])
def toggle_income_family_inclusion(income_id):
    """Toggle whether income is included in family total"""
    data = request.get_json() or {}
    user = sanitize_text(data.get('user', ''))
    
    try:
        income = IncomeEntry.query.get_or_404(income_id)
        income.include_in_family_total = not income.include_in_family_total
        db.session.commit()
        
        return jsonify({
            'success': True,
            'include_in_family_total': income.include_in_family_total,
            'message': f'Income {"included" if income.include_in_family_total else "excluded"} from family total'
        }), 200
    except Exception as e:
        logger.error(f"Error toggling income inclusion: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@main_bp.route('/expenses/settings', methods=['POST'])
def expenses_settings():
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    if user != admin_name:
        flash('Only admin can update settings.', 'error')
        return redirect(url_for('main.expenses'))
    currency = '$'
    categories = sanitize_text(request.form.get('categories', ''))
    try:
        db.session.execute(db.text("INSERT INTO app_setting(key,value) VALUES('currency', :v) ON CONFLICT(key) DO UPDATE SET value=excluded.value"), {"v": currency})
        db.session.execute(db.text("INSERT INTO app_setting(key,value) VALUES('categories', :v) ON CONFLICT(key) DO UPDATE SET value=excluded.value"), {"v": categories})
        db.session.commit()
        flash('Settings saved.', 'success')
    except Exception:
        flash('Failed to save settings.', 'error')
    today = date.today()
    y = request.args.get('y') or today.year
    m = request.args.get('m') or today.month
    sel = request.args.get('sel')
    return redirect(url_for('main.expenses', y=y, m=m, sel=sel, open='recurring'))


@main_bp.route('/expenses/delete/<int:entry_id>', methods=['POST'])
def delete_expense_entry(entry_id):
    entry = ExpenseEntry.query.get_or_404(entry_id)
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if not (user in admin_aliases or user == (entry.payer or '')):
        flash('Not allowed to delete entry.', 'error')
        return redirect(url_for('main.expenses'))
    affected_date = entry.date
    db.session.delete(entry)
    db.session.flush()
    _sync_budget_for_entry_date(affected_date)
    db.session.commit()
    log_activity(
        actor=user,
        action='money.expense.delete',
        summary=f"removed an expense: {entry.title} (${float(entry.amount or 0):.2f})",
        entity_type='expense_entry',
        entity_id=entry_id,
        visibility='family',
        metadata={'date': entry.date.isoformat() if entry.date else None},
    )
    flash('Expense deleted.', 'success')
    # Preserve view
    today = date.today()
    y = request.args.get('y') or today.year
    m = request.args.get('m') or today.month
    sel = request.args.get('sel')
    return redirect(url_for('main.expenses', y=y, m=m, sel=sel))


@main_bp.route('/expenses/income', methods=['POST'])
def add_income():
    """Add income entry"""
    title = bleach.clean(request.form.get('title', ''))
    amount = float(request.form.get('amount') or 0)
    source = bleach.clean(request.form.get('source') or '')
    category = bleach.clean(request.form.get('category') or '')
    recipient = bleach.clean(request.form.get('recipient') or '')
    is_personal = (request.form.get('is_personal') or '').lower() in ('1', 'true', 'on', 'yes')
    date_s = request.form.get('date')
    d = datetime.strptime(date_s, '%Y-%m-%d').date() if date_s else date.today()
    
    existing = IncomeEntry.query.filter(
        IncomeEntry.date == d,
        IncomeEntry.title == title,
        IncomeEntry.source == source,
        IncomeEntry.category == (category or None),
        IncomeEntry.recipient == recipient,
        db.func.round(IncomeEntry.amount, 2) == round(amount, 2),
    ).first()
    if existing:
        flash('That income entry already exists for this pay date.', 'warning')
        y = request.args.get('y') or d.year
        m = request.args.get('m') or d.month
        return redirect(url_for('main.expenses', y=y, m=m))

    income = IncomeEntry(
        date=d,
        title=title,
        source=source,
        category=(category or None),
        amount=amount,
        recipient=recipient,
        include_in_family_total=(not is_personal),
    )
    db.session.add(income)
    db.session.commit()

    if not is_personal:
        log_activity(
            actor=recipient or 'Family',
            action='money.income.add',
            summary=f"added income: {title} (${amount:.2f})",
            entity_type='income_entry',
            entity_id=income.id,
            visibility='family',
            metadata={'source': source, 'category': (category or None), 'date': d.isoformat()},
        )
    flash('Income added.', 'success')
    
    # Redirect back to expenses page
    today = date.today()
    y = request.args.get('y') or d.year
    m = request.args.get('m') or d.month
    return redirect(url_for('main.expenses', y=y, m=m))


@main_bp.route('/expenses/income/schedule', methods=['POST'])
def add_income_schedule():
    title = bleach.clean(request.form.get('title', ''))
    source = bleach.clean(request.form.get('source', ''))
    recipient = bleach.clean(request.form.get('recipient', ''))
    created_by = bleach.clean(request.form.get('user', ''))
    frequency = bleach.clean(request.form.get('frequency', 'biweekly')).lower()
    amount = float(request.form.get('expected_amount') or 0)
    start_date_s = request.form.get('start_date')
    payday_day = request.form.get('payday_day')

    if frequency not in ('weekly', 'biweekly', 'monthly'):
        frequency = 'biweekly'

    if not title or not recipient or amount <= 0:
        flash('Missing schedule details.', 'error')
        return redirect(url_for('main.expenses'))

    start_date = datetime.strptime(start_date_s, '%Y-%m-%d').date() if start_date_s else date.today()
    day_num = int(payday_day) if payday_day not in (None, '') else None
    if frequency == 'monthly' and not day_num:
        day_num = start_date.day

    existing_schedule = RecurringIncome.query.filter(
        RecurringIncome.active == True,
        RecurringIncome.title == title,
        RecurringIncome.source == source,
        RecurringIncome.recipient == recipient,
        db.func.round(RecurringIncome.expected_amount, 2) == round(amount, 2),
        RecurringIncome.frequency == frequency,
        RecurringIncome.start_date == start_date,
    ).first()
    if existing_schedule:
        flash('That pay schedule already exists.', 'warning')
        return redirect(url_for('main.expenses'))

    schedule = RecurringIncome(
        title=title,
        source=source,
        recipient=recipient,
        expected_amount=amount,
        frequency=frequency,
        start_date=start_date,
        payday_day=day_num,
        active=True,
        created_by=created_by or recipient,
    )
    db.session.add(schedule)
    db.session.commit()

    _generate_recurring_income_until(date.today())
    flash('Income schedule added. Payday entries will now auto-generate for confirmation.', 'success')
    return redirect(url_for('main.expenses'))


@main_bp.route('/api/expenses/income/pending-confirmations', methods=['GET'])
def get_pending_income_confirmations():
    member = sanitize_text(request.args.get('member', '') or '')
    if not member:
        return jsonify({'ok': True, 'items': []})

    as_of = _parse_optional_date(request.args.get('as_of')) or date.today()
    rows = IncomeEntry.query.filter(
        IncomeEntry.needs_confirmation == True,
        IncomeEntry.date <= as_of,
        IncomeEntry.recipient == member,
    ).order_by(IncomeEntry.date.asc(), IncomeEntry.id.asc()).all()

    privacy_mode = _is_privacy_mode()
    items = []
    for idx, r in enumerate(rows):
        amount_value = float(r.amount or 0)
        if privacy_mode:
            amount_value = _privacy_bucket(f"pending-income:{r.id}:{idx}", 10000, 100000, 1000)
        items.append({
            'id': r.id,
            'date': r.date.isoformat() if r.date else None,
            'title': r.title,
            'source': r.source,
            'amount': amount_value,
            'recipient': r.recipient,
        })

    return jsonify({
        'ok': True,
        'items': items
    })


@main_bp.route('/expenses/income/confirm/<int:income_id>', methods=['POST'])
def confirm_income_entry(income_id: int):
    income = IncomeEntry.query.get_or_404(income_id)
    data = request.get_json(silent=True) or {}
    user = sanitize_text(data.get('user') or request.form.get('user') or '')
    amount_raw = data.get('amount') if 'amount' in data else request.form.get('amount')

    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if not (user in admin_aliases or user == (income.recipient or '')):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    if amount_raw not in (None, ''):
        try:
            amount_val = float(amount_raw)
            if amount_val > 0:
                income.amount = amount_val
        except Exception:
            pass

    income.needs_confirmation = False
    income.confirmed_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'ok': True, 'message': 'Income confirmed'})


@main_bp.route('/expenses/income/cleanup-legacy', methods=['POST'])
def cleanup_legacy_income_entries():
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if user not in admin_aliases:
        flash('Only admin can run income cleanup.', 'error')
        return redirect(url_for('main.expenses'))

    removed = 0
    for row in IncomeEntry.query.all():
        title = (row.title or '').strip().lower()
        source = (row.source or '').strip().lower()
        if title in {'timeline income', 'pay day'} or source == 'test':
            db.session.delete(row)
            removed += 1

    removed += _dedupe_income_entries()
    db.session.commit()
    flash(f'Income cleanup completed. Removed {removed} legacy/duplicate entries.', 'success')
    return redirect(url_for('main.expenses'))


@main_bp.route('/expenses/income/schedule/delete/<int:schedule_id>', methods=['POST'])
def delete_income_schedule(schedule_id: int):
    schedule = RecurringIncome.query.get_or_404(schedule_id)
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if user not in admin_aliases and user != (schedule.recipient or ''):
        flash('Not allowed to delete this pay schedule.', 'error')
        return redirect(url_for('main.expenses'))

    schedule.active = False
    db.session.commit()
    flash('Pay schedule disabled.', 'success')
    return redirect(url_for('main.expenses'))


@main_bp.route('/expenses/income/delete/<int:income_id>', methods=['POST'])
def delete_income_entry(income_id: int):
    income = IncomeEntry.query.get_or_404(income_id)
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if not (user in admin_aliases or user == (income.recipient or '')):
        flash('Not allowed to delete income.', 'error')
        return redirect(url_for('main.expenses'))

    title = income.title
    amount = float(income.amount or 0)
    recipient = income.recipient or ''
    d = income.date
    db.session.delete(income)
    db.session.commit()
    log_activity(
        actor=user,
        action='money.income.delete',
        summary=f"removed income: {title} (${amount:.2f})",
        entity_type='income_entry',
        entity_id=income_id,
        visibility='family',
        metadata={'recipient': recipient, 'date': d.isoformat() if d else None},
    )
    flash('Income deleted.', 'success')

    today = date.today()
    y = request.args.get('y') or today.year
    m = request.args.get('m') or today.month
    sel = request.args.get('sel')
    return redirect(url_for('main.expenses', y=y, m=m, sel=sel))


@main_bp.route('/expenses/edit/<int:entry_id>', methods=['POST'])
def edit_expense_entry(entry_id):
    entry = ExpenseEntry.query.get_or_404(entry_id)
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if not (user in admin_aliases or user == (entry.payer or '')):
        flash('Not allowed to edit entry.', 'error')
        return redirect(url_for('main.expenses'))
    # Update fields
    entry.title = bleach.clean(request.form.get('title', entry.title))
    date_s = request.form.get('date')
    if date_s:
        entry.date = datetime.strptime(date_s, '%Y-%m-%d').date()
    cat_raw = request.form.get('category')
    if cat_raw is not None:
        entry.category = bleach.clean(cat_raw or '') or None
    entry.payer = bleach.clean(request.form.get('payer', entry.payer or ''))
    up = request.form.get('unit_price')
    q = request.form.get('quantity')
    amt = request.form.get('amount')
    entry.unit_price = float(up) if up not in (None, '') else entry.unit_price
    entry.quantity = float(q) if q not in (None, '') else entry.quantity
    entry.amount = float(amt) if amt not in (None, '') else entry.amount
    db.session.commit()
    flash('Expense updated.', 'success')
    # Preserve view
    today = date.today()
    y = request.args.get('y') or entry.date.year
    m = request.args.get('m') or entry.date.month
    sel = request.args.get('sel') or entry.date.strftime('%Y-%m-%d')
    return redirect(url_for('main.expenses', y=y, m=m, sel=sel))


@main_bp.route('/expenses/bulk-delete', methods=['POST'])
def bulk_delete_expenses():
    user = sanitize_text(request.form.get('user', ''))
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    ids = request.form.getlist('ids')
    if not ids:
        flash('No entries selected.', 'warning')
        return redirect(url_for('main.expenses'))
    deleted = 0
    for entry_id in ids:
        try:
            entry = ExpenseEntry.query.get(int(entry_id))
            if entry and (user in admin_aliases or user == (entry.payer or '')):
                db.session.delete(entry)
                deleted += 1
        except Exception:
            continue
    db.session.commit()
    flash(f'{deleted} expense(s) deleted.', 'success')
    # Preserve view
    today = date.today()
    y = request.args.get('y') or today.year
    m = request.args.get('m') or today.month
    sel = request.args.get('sel')
    return redirect(url_for('main.expenses', y=y, m=m, sel=sel))


@main_bp.route('/api/expenses/month', methods=['GET'])
def api_expenses_month():
    try:
        y, m = _parse_year_month_query()
    except ValueError:
        return jsonify({'error': 'Invalid month parameter. Must be an integer between 1 and 12.'}), 400
    _, month_end = _month_start_end(y, m)
    _generate_recurring_entries_until(max(date.today(), month_end))
    _generate_recurring_income_until(date.today())
    payload = _build_month_payload(y, m)
    if _is_privacy_mode():
        payload = _privacy_mask_payload(payload)
    return jsonify(payload)


@main_bp.route('/api/expenses/all', methods=['GET'])
def api_expenses_all():
    """Return a clean, filterable list of expense entries for explorer-style UI."""
    start_date = _parse_optional_date(request.args.get('start_date'))
    end_date = _parse_optional_date(request.args.get('end_date'))
    include_future = _parse_bool(request.args.get('include_future'), default=False)
    status_filter = sanitize_text((request.args.get('status') or 'all')).strip().lower()
    payer_filter = sanitize_text((request.args.get('payer') or '')).strip()
    category_filter = sanitize_text((request.args.get('category') or '')).strip()
    query_text = sanitize_text((request.args.get('q') or '')).strip().lower()

    try:
        limit = int(request.args.get('limit') or 250)
    except Exception:
        limit = 250
    limit = max(25, min(limit, 1000))

    q = ExpenseEntry.query
    if start_date:
        q = q.filter(ExpenseEntry.date >= start_date)
    if end_date:
        q = q.filter(ExpenseEntry.date <= end_date)
    if not include_future:
        q = q.filter(ExpenseEntry.date <= date.today())
    if payer_filter:
        q = q.filter(ExpenseEntry.payer == payer_filter)
    if category_filter:
        q = q.filter(ExpenseEntry.category == category_filter)
    if query_text:
        like = f"%{query_text}%"
        q = q.filter(db.or_(
            ExpenseEntry.title.ilike(like),
            ExpenseEntry.category.ilike(like),
            ExpenseEntry.payer.ilike(like),
        ))

    rows = (
        q.order_by(ExpenseEntry.date.desc(), ExpenseEntry.id.desc())
        .limit(limit)
        .all()
    )

    items: list[dict] = []
    total_charged = 0.0
    total_paid = 0.0
    total_due = 0.0
    privacy_mode = _is_privacy_mode()

    for idx, row in enumerate(rows):
        charged_amount = max(float(row.amount or 0.0), 0.0)
        paid_amount = max(_entry_paid_amount(row), 0.0)
        due_amount = max(_entry_due_amount(row), 0.0)
        recurring = bool(row.recurring_id)

        if privacy_mode:
            masked = _privacy_bucket(f"all-expense:{row.id}:{idx}", 10000, 150000, 1000)
            charged_amount = masked
            paid_amount = masked
            due_amount = 0.0

        if recurring:
            if due_amount <= 0.0001:
                status = 'paid'
            elif paid_amount > 0.0001:
                status = 'partial'
            else:
                status = 'due'
        else:
            status = 'paid'

        if status_filter in {'due', 'partial', 'paid'} and status != status_filter:
            continue

        total_charged += charged_amount
        total_paid += paid_amount
        total_due += due_amount

        items.append({
            'id': row.id,
            'date': row.date.isoformat() if row.date else None,
            'title': row.title,
            'category': row.category,
            'payer': row.payer,
            'amount': charged_amount,
            'charged_amount': charged_amount,
            'paid_amount': paid_amount,
            'due_amount': due_amount,
            'status': status,
            'recurring': recurring,
            'recurring_id': row.recurring_id,
            'quantity': float(row.quantity) if row.quantity is not None else None,
            'unit_price': float(row.unit_price) if row.unit_price is not None else None,
            'timestamp': row.timestamp.isoformat() if row.timestamp else None,
            'include_in_family_total': bool(getattr(row, 'include_in_family_total', True)),
        })

    payers = sorted({sanitize_text(i.get('payer') or '').strip() for i in items if sanitize_text(i.get('payer') or '').strip()}, key=lambda x: x.lower())
    categories = sorted({sanitize_text(i.get('category') or '').strip() for i in items if sanitize_text(i.get('category') or '').strip()}, key=lambda x: x.lower())

    return jsonify({
        'ok': True,
        'filters': {
            'start_date': start_date.isoformat() if start_date else None,
            'end_date': end_date.isoformat() if end_date else None,
            'include_future': include_future,
            'status': status_filter or 'all',
            'payer': payer_filter,
            'category': category_filter,
            'q': query_text,
            'limit': limit,
        },
        'summary': {
            'count': len(items),
            'charged_total': round(total_charged, 2),
            'paid_total': round(total_paid, 2),
            'due_total': round(total_due, 2),
        },
        'payers': payers,
        'categories': categories,
        'items': items,
    })


@main_bp.route('/api/expenses/quick-add', methods=['POST'])
def api_expenses_quick_add():
    """Quick calendar-first workflow: type -> recurrence -> amount -> save."""
    config = current_app.config.get('HOMEHUB_CONFIG', {})
    if config.get('password_hash') and not session.get('authed'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    entry_type = sanitize_text(data.get('entry_type', '')).lower()
    recurrence = sanitize_text(data.get('recurrence', '')).lower()
    frequency = sanitize_text(data.get('frequency', '')).lower()
    scope = sanitize_text(data.get('scope', 'family')).lower()
    add_to_timeline = _parse_bool(data.get('add_to_timeline'), default=True)
    user = sanitize_text(data.get('user', '')) or 'Family'
    title = sanitize_text(data.get('title', ''))
    category = sanitize_text(data.get('category', ''))
    date_s = sanitize_text(data.get('date', ''))

    if entry_type not in {'expense', 'income'}:
        return jsonify({'ok': False, 'error': 'entry_type must be income or expense'}), 400
    if recurrence not in {'one-time', 'recurring'}:
        return jsonify({'ok': False, 'error': 'recurrence must be one-time or recurring'}), 400
    if scope not in {'family', 'personal'}:
        return jsonify({'ok': False, 'error': 'scope must be family or personal'}), 400
    if recurrence == 'recurring':
        if frequency not in {'weekly', 'biweekly', 'monthly'}:
            return jsonify({'ok': False, 'error': 'frequency must be weekly, biweekly, or monthly'}), 400

    try:
        amount = float(data.get('amount') or 0)
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid amount'}), 400
    if amount <= 0:
        return jsonify({'ok': False, 'error': 'Amount must be greater than 0'}), 400

    try:
        entry_date = datetime.strptime(date_s, '%Y-%m-%d').date() if date_s else date.today()
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid date format (expected YYYY-MM-DD)'}), 400

    try:
        if entry_type == 'expense' and recurrence == 'one-time':
            entry = ExpenseEntry(
                date=entry_date,
                title=title or 'Expense',
                category=category or 'General',
                amount=amount,
                payer=user,
                include_in_family_total=(scope == 'family'),
            )
            db.session.add(entry)
            _sync_budget_for_entry_date(entry_date)
            db.session.commit()

            if add_to_timeline:
                log_activity(
                    actor=user,
                    action='money.expense.add',
                    summary=f"added an expense: {entry.title} (${amount:.2f})",
                    entity_type='expense_entry',
                    entity_id=entry.id,
                    visibility='family',
                    metadata={
                        'category': entry.category,
                        'date': entry_date.isoformat(),
                        'scope': scope,
                    },
                )
            return jsonify({'ok': True, 'message': 'Expense added', 'id': entry.id})

        if entry_type == 'expense' and recurrence == 'recurring':
            starting_balance = float(data.get('starting_balance', 0) or 0)
            rule = RecurringExpense(
                title=title or 'Recurring Expense',
                unit_price=amount,
                default_quantity=1.0,
                frequency=frequency,
                monthly_mode='day_of_month',
                category=category or 'General',
                start_date=entry_date,
                end_date=None,
                starting_balance=max(starting_balance, 0.0),
                creator=user,
                include_in_family_total=(scope == 'family'),
                effective_from=entry_date,
            )
            db.session.add(rule)
            db.session.flush()

            # Create first occurrence immediately so calendar/history update at once.
            existing_first = ExpenseEntry.query.filter_by(recurring_id=rule.id, date=entry_date).first()
            if not existing_first:
                first_entry = ExpenseEntry(
                    date=entry_date,
                    title=rule.title,
                    category=rule.category,
                    unit_price=amount,
                    quantity=1.0,
                    amount=amount,
                    payer=user,
                    include_in_family_total=(scope == 'family'),
                    recurring_id=rule.id,
                )
                db.session.add(first_entry)
            _sync_budget_for_entry_date(entry_date)

            db.session.commit()

            if add_to_timeline:
                log_activity(
                    actor=user,
                    action='money.bill.add',
                    summary=f"added a household bill: {rule.title} (${amount:.2f})",
                    entity_type='recurring_expense',
                    entity_id=rule.id,
                    visibility='family',
                    metadata={
                        'category': rule.category,
                        'frequency': rule.frequency,
                        'start_date': entry_date.isoformat(),
                        'starting_balance': round(max(starting_balance, 0.0), 2),
                        'scope': scope,
                    },
                )
            return jsonify({'ok': True, 'message': 'Recurring expense added', 'id': rule.id})

        if entry_type == 'income' and recurrence == 'one-time':
            income = IncomeEntry(
                date=entry_date,
                title=title or 'Income',
                source='Manual',
                category=category or None,
                amount=amount,
                recipient=user,
                include_in_family_total=(scope == 'family'),
            )
            db.session.add(income)
            db.session.commit()

            if add_to_timeline:
                log_activity(
                    actor=user,
                    action='money.income.add',
                    summary=f"added income: {income.title} (${amount:.2f})",
                    entity_type='income_entry',
                    entity_id=income.id,
                    visibility='family',
                    metadata={
                        'source': income.source,
                        'category': income.category,
                        'date': entry_date.isoformat(),
                        'scope': scope,
                    },
                )
            return jsonify({'ok': True, 'message': 'Income added', 'id': income.id})

        schedule = RecurringIncome(
            title=title or 'Pay Schedule',
            source=category or 'Manual',
            recipient=user,
            expected_amount=amount,
            frequency=frequency,
            start_date=entry_date,
            payday_day=(entry_date.day if frequency == 'monthly' else None),
            include_in_family_total=(scope == 'family'),
            active=True,
            created_by=user,
        )
        db.session.add(schedule)
        db.session.commit()
        _generate_recurring_income_until(date.today())

        if add_to_timeline:
            log_activity(
                actor=user,
                action='money.income.schedule.add',
                summary=f"added pay schedule: {schedule.title} (${amount:.2f})",
                entity_type='recurring_income',
                entity_id=schedule.id,
                visibility='family',
                metadata={
                    'source': schedule.source,
                    'category': category or None,
                    'frequency': schedule.frequency,
                    'start_date': entry_date.isoformat(),
                    'recipient': schedule.recipient,
                    'scope': scope,
                },
            )
        return jsonify({'ok': True, 'message': 'Pay schedule added', 'id': schedule.id})
    except Exception as exc:
        db.session.rollback()
        logger.exception('Quick add failed: %s', exc)
        return jsonify({'ok': False, 'error': 'Could not save quick add item'}), 500


@main_bp.route('/api/expenses/finance/summary', methods=['GET'])
def api_finance_summary():
    """Return family + personal (current member) finance totals for a given month.

    Query params:
      - year, month: target period (defaults to current)
      - member: member name for personal summary (optional)
    """
    try:
        y, m = _parse_year_month_query()
    except ValueError:
        return jsonify({'error': 'Invalid month parameter. Must be an integer between 1 and 12.'}), 400

    _, month_end = _month_start_end(y, m)
    _generate_recurring_entries_until(max(date.today(), month_end))
    _generate_recurring_income_until(date.today())

    member = sanitize_text(request.args.get('member', '') or '')
    snapshot = _monthly_finance_snapshot(y, m, member=member)
    family_accounting = snapshot.get('family', {})
    member_accounting = snapshot.get('member', {})

    family_income_paid_total = float(family_accounting.get('income', {}).get('paid_total', 0.0))
    family_income_due_total = float(family_accounting.get('income', {}).get('due_total', 0.0))
    family_income_total = float(family_accounting.get('income', {}).get('expected_total', 0.0))
    family_expenses_paid_total = float(family_accounting.get('expenses', {}).get('paid_total', 0.0))
    family_expenses_due_total = float(family_accounting.get('expenses', {}).get('due_total', 0.0))
    family_expenses_charged_total = float(family_accounting.get('expenses', {}).get('charged_total', 0.0))
    family_expenses_total = float(family_accounting.get('expenses', {}).get('expected_total', 0.0))
    shared_expenses_total = 0.0

    # Family budget
    budget_total = float(snapshot.get('budget', {}).get('total', 0.0))
    budget_spent = float(snapshot.get('budget', {}).get('spent', 0.0))
    budget_remaining = float(snapshot.get('budget', {}).get('remaining', 0.0))
    has_budget = bool(snapshot.get('budget', {}).get('has_budget', False))

    # Member/personal
    member_expenses_charged_total = float(member_accounting.get('expenses', {}).get('charged_total', 0.0))
    member_expenses_paid_total = float(member_accounting.get('expenses', {}).get('paid_total', 0.0))
    member_expenses_due_total = float(member_accounting.get('expenses', {}).get('due_total', 0.0))
    member_expenses_total = float(member_accounting.get('expenses', {}).get('expected_total', 0.0))
    member_income_paid_total = float(member_accounting.get('income', {}).get('paid_total', 0.0))
    member_income_due_total = float(member_accounting.get('income', {}).get('due_total', 0.0))
    member_income_total = float(member_accounting.get('income', {}).get('expected_total', 0.0))

    response = {
        'period': {'year': y, 'month': m},
        'family': {
            'income_total': family_income_total,
            'income_paid_total': family_income_paid_total,
            'income_due_total': family_income_due_total,
            'expenses_total': family_expenses_total,
            'expenses_paid_total': family_expenses_paid_total,
            'expenses_due_total': family_expenses_due_total,
            'expenses_charged_total': family_expenses_charged_total,
            'shared_expenses_total': shared_expenses_total,
            'net_total': family_income_total - (family_expenses_total + shared_expenses_total),
            'net_paid_total': family_income_paid_total - family_expenses_paid_total,
            'income_paid_by_member': family_accounting.get('income', {}).get('paid_by_member', {}),
            'income_due_by_member': family_accounting.get('income', {}).get('due_by_member', {}),
            'expenses_paid_by_member': family_accounting.get('expenses', {}).get('paid_by_member', {}),
            'expenses_due_by_member': family_accounting.get('expenses', {}).get('due_by_member', {}),
            'budget': {
                'total': budget_total,
                'spent': budget_spent,
                'remaining': budget_remaining,
                'has_budget': has_budget,
            }
        },
        'member': {
            'name': member,
            'income_total': member_income_total,
            'income_paid_total': member_income_paid_total,
            'income_due_total': member_income_due_total,
            'expenses_total': member_expenses_total,
            'expenses_paid_total': member_expenses_paid_total,
            'expenses_due_total': member_expenses_due_total,
            'expenses_charged_total': member_expenses_charged_total,
            'net_total': member_income_total - member_expenses_total,
            'net_paid_total': member_income_paid_total - member_expenses_paid_total,
        }
    }

    if _is_privacy_mode():
        base_seed = f"finance:{y}:{m}:{member or 'family'}"
        fam_income = _privacy_bucket(base_seed + ':fam:income', 100000, 1200000, 10000)
        fam_expenses = _privacy_bucket(base_seed + ':fam:expense', 10000, 700000, 10000)
        fam_net = fam_income - fam_expenses
        response['family'].update({
            'income_total': fam_income,
            'income_paid_total': fam_income,
            'income_due_total': 0.0,
            'expenses_total': fam_expenses,
            'expenses_paid_total': fam_expenses,
            'expenses_due_total': 0.0,
            'expenses_charged_total': fam_expenses,
            'shared_expenses_total': 0.0,
            'net_total': fam_net,
            'net_paid_total': fam_net,
            'income_paid_by_member': {'Family': fam_income},
            'income_due_by_member': {'Family': 0.0},
            'expenses_paid_by_member': {'Family': fam_expenses},
            'expenses_due_by_member': {'Family': 0.0},
        })
        if response['family'].get('budget', {}).get('has_budget'):
            budget_total = float(response['family']['budget'].get('total') or 0.0)
            if budget_total <= 0:
                budget_total = _privacy_bucket(base_seed + ':budget', 100000, 900000, 10000)
            response['family']['budget']['total'] = budget_total
            response['family']['budget']['spent'] = min(fam_expenses, budget_total)
            response['family']['budget']['remaining'] = max(budget_total - response['family']['budget']['spent'], 0.0)

        member_income = _privacy_bucket(base_seed + ':member:income', 10000, 200000, 1000)
        member_expenses = _privacy_bucket(base_seed + ':member:expense', 10000, 180000, 1000)
        response['member'].update({
            'income_total': member_income,
            'income_paid_total': member_income,
            'income_due_total': 0.0,
            'expenses_total': member_expenses,
            'expenses_paid_total': member_expenses,
            'expenses_due_total': 0.0,
            'expenses_charged_total': member_expenses,
            'net_total': member_income - member_expenses,
            'net_paid_total': member_income - member_expenses,
        })

    return jsonify(response)


@main_bp.route('/api/expenses/collaboration', methods=['GET'])
def api_expenses_collaboration():
    """Return collaborative finance insights for charts and family balancing."""
    try:
        y, m = _parse_year_month_query()
    except ValueError:
        return jsonify({'error': 'Invalid month parameter. Must be an integer between 1 and 12.'}), 400

    _, month_end = _month_start_end(y, m)
    _generate_recurring_entries_until(max(date.today(), month_end))
    _generate_recurring_income_until(date.today())

    months_back = request.args.get('months', 6, type=int)
    months_back = max(3, min(months_back or 6, 12))

    def shift_month(year_value: int, month_value: int, delta: int) -> tuple[int, int]:
        idx = (year_value * 12 + (month_value - 1)) + delta
        new_year = idx // 12
        new_month = (idx % 12) + 1
        return new_year, new_month

    trend = []
    for offset in range(-(months_back - 1), 1):
        yy, mm = shift_month(y, m, offset)
        month_history = _build_month_history(yy, mm)
        acc = month_history.get('accounting', {})
        income_total = float(acc.get('income', {}).get('expected_total', 0.0))
        expense_total = float(acc.get('expenses', {}).get('expected_total', 0.0))
        income_paid_total = float(acc.get('income', {}).get('paid_total', 0.0))
        expense_paid_total = float(acc.get('expenses', {}).get('paid_total', 0.0))
        trend.append({
            'year': yy,
            'month': mm,
            'label': date(yy, mm, 1).strftime('%b %Y'),
            'income': income_total,
            'income_paid': income_paid_total,
            'expenses': expense_total,
            'expenses_paid': expense_paid_total,
            'net': income_total - expense_total,
            'net_paid': income_paid_total - expense_paid_total,
        })

    month_snapshot = _monthly_finance_snapshot(y, m)
    month_history = month_snapshot.get('history', {})
    month_items = month_history.get('items', [])
    month_accounting = month_snapshot.get('family', {})

    shared_income_actual = float(month_accounting.get('income', {}).get('paid_total', 0.0))
    shared_income_projected = float(month_accounting.get('income', {}).get('due_total', 0.0))
    shared_income_total = float(month_accounting.get('income', {}).get('expected_total', 0.0))
    shared_expenses_total = float(month_accounting.get('expenses', {}).get('expected_total', 0.0))
    shared_savings_total = _manual_hub_savings_total()

    category_totals: dict[str, float] = {}
    for item in month_items:
        if not bool(item.get('include_in_family_total', True)):
            continue
        if (item.get('entry_type') or '') not in {'expense', 'debt_due'}:
            continue
        category = (item.get('category') or 'Uncategorized').strip() or 'Uncategorized'
        category_totals[category] = category_totals.get(category, 0.0) + float(
            (item.get('paid_amount', 0.0) or 0.0) + (item.get('due_amount', 0.0) or 0.0)
        )

    settlements = []

    member_rows = []
    for row in month_accounting.get('members', []):
        member_rows.append({
            'name': row.get('name', ''),
            'income': float(row.get('income_expected', 0.0) or 0.0),
            'income_actual': float(row.get('income_paid', 0.0) or 0.0),
            'projected_income': float(row.get('income_due', 0.0) or 0.0),
            'income_projected_total': float(row.get('income_expected', 0.0) or 0.0),
            'expenses': float(row.get('expense_expected', 0.0) or 0.0),
            'expenses_paid': float(row.get('expense_paid', 0.0) or 0.0),
            'expenses_due': float(row.get('expense_due', 0.0) or 0.0),
            'shared_balance': 0.0,
            'net': float(row.get('net_expected', 0.0) or 0.0),
            'net_actual': float(row.get('net_paid', 0.0) or 0.0),
            'net_projected': float(row.get('net_expected', 0.0) or 0.0),
        })
    member_rows.sort(key=lambda row: row['name'].lower())

    shared_recent = []
    recent_expense_items = [
        x for x in month_items
        if bool(x.get('include_in_family_total', True)) and (x.get('entry_type') or '') in {'expense', 'debt_due'}
    ]
    for expense in sorted(recent_expense_items, key=lambda x: x.get('date') or '', reverse=True)[:10]:
        shared_recent.append({
            'id': expense.get('id'),
            'description': expense.get('title'),
            'total_amount': float(expense.get('due_amount', 0.0) or 0.0),
            'paid_by': expense.get('paid_by') or expense.get('payer') or expense.get('debtor'),
            'date': expense.get('date'),
            'category': expense.get('category'),
            'recurring': bool(expense.get('recurring')),
            'settled': True,
        })

    response = {
        'period': {'year': y, 'month': m},
        'summary': {
            'shared_income_total': round(shared_income_total, 2),
            'shared_income_actual': round(shared_income_actual, 2),
            'shared_income_projected': round(shared_income_projected, 2),
            'shared_expenses_total': round(shared_expenses_total, 2),
            'shared_expenses_paid_total': round(float(month_accounting.get('expenses', {}).get('paid_total', 0.0)), 2),
            'shared_expenses_due_total': round(float(month_accounting.get('expenses', {}).get('due_total', 0.0)), 2),
            'savings_total': round(shared_savings_total, 2),
            'window_days': None,
            'window_label': 'Selected month',
        },
        'accounting': month_accounting,
        'trend': trend,
        'categories': [
            {'name': category, 'amount': round(amount, 2)}
            for category, amount in sorted(category_totals.items(), key=lambda kv: kv[1], reverse=True)
        ],
        'members': member_rows,
        'settlements': sorted(settlements, key=lambda x: x['amount'], reverse=True),
        'shared_recent': shared_recent,
    }

    if _is_privacy_mode():
        seed = f"collab:{y}:{m}"
        fake_income = _privacy_bucket(seed + ':income', 100000, 1200000, 10000)
        fake_expenses = _privacy_bucket(seed + ':expenses', 10000, 800000, 10000)
        fake_savings = _privacy_bucket(seed + ':savings', 10000, 400000, 10000)
        response['summary'].update({
            'shared_income_total': fake_income,
            'shared_income_actual': fake_income,
            'shared_income_projected': 0.0,
            'shared_expenses_total': fake_expenses,
            'shared_expenses_paid_total': fake_expenses,
            'shared_expenses_due_total': 0.0,
            'savings_total': fake_savings,
        })
        response['categories'] = [
            {'name': 'Housing', 'amount': round(fake_expenses * 0.4, 2)},
            {'name': 'Food', 'amount': round(fake_expenses * 0.3, 2)},
            {'name': 'Transport', 'amount': round(fake_expenses * 0.2, 2)},
            {'name': 'Other', 'amount': round(fake_expenses * 0.1, 2)},
        ]
        response['members'] = [
            {
                'name': row.get('name') or f"Member {idx+1}",
                'income': round(fake_income / max(len(member_rows), 1), 2),
                'income_actual': round(fake_income / max(len(member_rows), 1), 2),
                'projected_income': 0.0,
                'income_projected_total': round(fake_income / max(len(member_rows), 1), 2),
                'expenses': round(fake_expenses / max(len(member_rows), 1), 2),
                'expenses_paid': round(fake_expenses / max(len(member_rows), 1), 2),
                'expenses_due': 0.0,
                'shared_balance': 0.0,
                'net': round((fake_income - fake_expenses) / max(len(member_rows), 1), 2),
                'net_actual': round((fake_income - fake_expenses) / max(len(member_rows), 1), 2),
                'net_projected': round((fake_income - fake_expenses) / max(len(member_rows), 1), 2),
            }
            for idx, row in enumerate(member_rows or [{'name': 'Family'}])
        ]
        response['shared_recent'] = []
        response['settlements'] = []
        response['accounting'] = {
            'income': {
                'paid_total': fake_income,
                'due_total': 0.0,
                'expected_total': fake_income,
                'paid_by_member': {'Family': fake_income},
                'due_by_member': {'Family': 0.0},
            },
            'expenses': {
                'charged_total': fake_expenses,
                'paid_total': fake_expenses,
                'due_total': 0.0,
                'expected_total': fake_expenses,
                'paid_by_member': {'Family': fake_expenses},
                'due_by_member': {'Family': 0.0},
            },
            'net': {
                'paid_total': fake_income - fake_expenses,
                'expected_total': fake_income - fake_expenses,
                'due_total': 0.0,
            },
            'members': response['members'],
        }
        response['trend'] = [
            {
                'year': point.get('year'),
                'month': point.get('month'),
                'label': point.get('label'),
                'income': _privacy_bucket(f"{seed}:trend:income:{point.get('year')}:{point.get('month')}", 100000, 1200000, 10000),
                'income_paid': _privacy_bucket(f"{seed}:trend:income:{point.get('year')}:{point.get('month')}", 100000, 1200000, 10000),
                'expenses': _privacy_bucket(f"{seed}:trend:expense:{point.get('year')}:{point.get('month')}", 10000, 800000, 10000),
                'expenses_paid': _privacy_bucket(f"{seed}:trend:expense:{point.get('year')}:{point.get('month')}", 10000, 800000, 10000),
                'net': _privacy_bucket(f"{seed}:trend:net:{point.get('year')}:{point.get('month')}", -200000, 400000, 10000),
                'net_paid': _privacy_bucket(f"{seed}:trend:net:{point.get('year')}:{point.get('month')}", -200000, 400000, 10000),
            }
            for point in trend
        ]

    return jsonify(response)


@main_bp.route('/api/expenses/debt-workflow', methods=['GET'])
def api_debt_workflow_snapshot():
    """Return debt and due snapshots for daily decision support."""
    try:
        y, m = _parse_year_month_query()
    except ValueError:
        return jsonify({'error': 'Invalid month parameter. Must be an integer between 1 and 12.'}), 400

    today = date.today()
    window_days = request.args.get('window_days', 30, type=int)
    if window_days not in {7, 30, 90, 365}:
        window_days = 30

    horizon_7 = today + timedelta(days=7)
    horizon_14 = today + timedelta(days=14)
    horizon_30 = today + timedelta(days=30)
    horizon_90 = today + timedelta(days=90)
    horizon_365 = today + timedelta(days=365)
    horizon_window = today + timedelta(days=window_days)
    _, selected_month_end = _month_start_end(y, m)

    _generate_recurring_entries_until(max(selected_month_end, horizon_365))
    _generate_recurring_income_until(today)

    active_debts = DebtAccount.query.filter_by(active=True).order_by(DebtAccount.created_at.desc()).all()
    total_outstanding_debt = sum(float(d.current_balance or 0) for d in active_debts)

    due_entries = ExpenseEntry.query.filter(
        ExpenseEntry.recurring_id.isnot(None),
        ExpenseEntry.date >= today,
        ExpenseEntry.date <= horizon_365,
    ).order_by(ExpenseEntry.date.asc(), ExpenseEntry.id.asc()).all()
    due_entries = [e for e in due_entries if _entry_remaining_amount(e) > 0.0001]

    due_7_total = sum(_entry_remaining_amount(e) for e in due_entries if e.date <= horizon_7)
    due_14_total = sum(_entry_remaining_amount(e) for e in due_entries if e.date <= horizon_14)
    due_30_total = sum(_entry_remaining_amount(e) for e in due_entries if e.date <= horizon_30)
    due_90_total = sum(_entry_remaining_amount(e) for e in due_entries if e.date <= horizon_90)
    due_365_total = sum(_entry_remaining_amount(e) for e in due_entries if e.date <= horizon_365)
    due_window_total = sum(_entry_remaining_amount(e) for e in due_entries if e.date <= horizon_window)

    month_start, month_end = _month_start_end(y, m)
    month_recurring = ExpenseEntry.query.filter(
        ExpenseEntry.recurring_id.isnot(None),
        ExpenseEntry.date >= month_start,
        ExpenseEntry.date <= month_end,
    ).all()
    unpaid_recurring_this_month = sum(_entry_remaining_amount(e) for e in month_recurring)

    family_income_total, family_expenses_total = _family_income_expenses_for_month(y, m)
    free_cash_this_month = family_income_total - family_expenses_total

    debts_payload = []
    total_past_due = 0.0
    high_priority_count = 0
    for debt in active_debts:
        apr = float(debt.interest_rate or 0)
        current_balance = float(debt.current_balance or 0)
        metrics = _debt_priority_metrics(debt, ref_date=today)
        scheduled_payment = float(metrics.get('scheduled_payment') or 0)
        months_remaining = metrics.get('payments_remaining')
        payoff_estimate = (today + timedelta(days=(months_remaining or 0) * 30)).isoformat() if months_remaining is not None else None
        monthly_principal = max(scheduled_payment - ((apr / 100.0 / 12.0) * current_balance), 0.0)
        past_due_amount = float(metrics.get('past_due_amount') or 0)
        priority_band = metrics.get('priority_band') or 'low'
        if past_due_amount > 0:
            total_past_due += past_due_amount
        if priority_band == 'high':
            high_priority_count += 1
        last_payment = (
            DebtPayment.query
            .filter(DebtPayment.debt_id == debt.id)
            .order_by(DebtPayment.payment_date.desc(), DebtPayment.id.desc())
            .first()
        )
        debts_payload.append({
            'id': debt.id,
            'description': debt.description,
            'debtor': debt.debtor,
            'creditor': debt.creditor,
            'initial_amount': float(debt.initial_amount or 0),
            'current_balance': current_balance,
            'interest_rate': apr,
            'payoff_date': debt.payoff_date.isoformat() if debt.payoff_date else None,
            'progress': (float(debt.initial_amount or 0) - float(debt.current_balance or 0)) / float(debt.initial_amount or 1) * 100 if float(debt.initial_amount or 0) > 0 else 0,
            'last_payment_amount': float(last_payment.amount_paid or 0) if last_payment else 0,
            'avg_monthly_principal': monthly_principal,
            'scheduled_monthly_payment': float(scheduled_payment or 0),
            'estimated_months_remaining': months_remaining,
            'estimated_payoff_date': payoff_estimate,
            'past_due_amount': past_due_amount,
            'missed_payments_estimate': int(metrics.get('missed_payments_estimate') or 0),
            'priority_score': float(metrics.get('priority_score') or 0),
            'priority_band': priority_band,
            'next_due_date': metrics.get('next_due_date').isoformat() if metrics.get('next_due_date') else None,
            'days_to_target_due': metrics.get('days_to_target_due'),
        })

    debts_payload.sort(key=lambda d: (d.get('priority_score') or 0), reverse=True)

    due_items_payload = []
    due_entries_for_window = [entry for entry in due_entries if entry.date <= horizon_window]
    for entry in due_entries_for_window[:15]:
        due_items_payload.append({
            'entry_id': entry.id,
            'recurring_id': entry.recurring_id,
            'title': entry.title,
            'date': entry.date.isoformat() if entry.date else None,
            'payer': entry.payer,
            'category': getattr(entry, 'category', None),
            'remaining_amount': _entry_remaining_amount(entry),
        })

    response = {
        'period': {'year': y, 'month': m},
        'snapshot': {
            'total_outstanding_debt': total_outstanding_debt,
            'due_in_7_days': due_7_total,
            'due_in_14_days': due_14_total,
            'due_in_30_days': due_30_total,
            'due_in_90_days': due_90_total,
            'due_in_365_days': due_365_total,
            'due_in_window_days': due_window_total,
            'window_days': window_days,
            'unpaid_recurring_this_month': unpaid_recurring_this_month,
            'free_cash_this_month': free_cash_this_month,
            'total_past_due_debt': total_past_due,
            'high_priority_debts': high_priority_count,
        },
        'debts': debts_payload,
        'due_items': due_items_payload,
    }

    if _is_privacy_mode():
        seed = f"debt:{y}:{m}"
        fake_due_30 = _privacy_bucket(seed + ':due30', 10000, 500000, 10000)
        fake_debt = _privacy_bucket(seed + ':total', 100000, 1000000, 10000)
        response['snapshot'].update({
            'total_outstanding_debt': fake_debt,
            'due_in_7_days': round(fake_due_30 * 0.25, 2),
            'due_in_14_days': round(fake_due_30 * 0.5, 2),
            'due_in_30_days': fake_due_30,
            'due_in_90_days': round(fake_due_30 * 2, 2),
            'due_in_365_days': round(fake_due_30 * 6, 2),
            'due_in_window_days': fake_due_30,
            'unpaid_recurring_this_month': fake_due_30,
            'free_cash_this_month': _privacy_bucket(seed + ':cash', 10000, 300000, 10000),
            'total_past_due_debt': round(fake_due_30 * 0.2, 2),
            'high_priority_debts': 1,
        })
        response['debts'] = []
        response['due_items'] = []

    return jsonify(response)


@main_bp.route('/api/expenses/debt-workflow/impact', methods=['POST'])
def api_debt_workflow_impact():
    """Preview impact of an early/partial payment for debt or recurring bills."""
    data = request.get_json() or {}
    target_type = sanitize_text(data.get('target_type', '')).lower()
    target_id = data.get('target_id')
    payment_amount = float(data.get('payment_amount', 0) or 0)

    if payment_amount <= 0:
        return jsonify({'error': 'Payment amount must be positive'}), 400

    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid target id'}), 400

    today = date.today()
    y = data.get('year', today.year)
    m = data.get('month', today.month)
    try:
        y = int(y)
        m = int(m)
        if m < 1 or m > 12:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid year/month'}), 400

    _generate_recurring_entries_until(today + timedelta(days=30))
    family_income_total, family_expenses_total = _family_income_expenses_for_month(y, m)
    free_cash_before = family_income_total - family_expenses_total

    if target_type == 'debt':
        debt = DebtAccount.query.get(target_id)
        if not debt or not debt.active:
            return jsonify({'error': 'Debt not found'}), 404

        current_balance = float(debt.current_balance or 0)
        apr = float(debt.interest_rate or 0)
        applied_amount = min(payment_amount, current_balance)
        new_balance = max(current_balance - applied_amount, 0.0)

        months_to_due = _months_until(debt.payoff_date)
        if months_to_due:
            scheduled_payment = _amortized_monthly_payment(current_balance, apr, months_to_due)
        else:
            scheduled_payment = None

        if scheduled_payment is None or scheduled_payment <= 0:
            fallback_principal = _estimate_monthly_principal_for_debt(debt.id)
            approx_interest = (apr / 100.0 / 12.0) * current_balance
            scheduled_payment = max(fallback_principal + approx_interest, 0.0)

        before_months = _payoff_months_from_payment(current_balance, apr, scheduled_payment)
        after_months = _payoff_months_from_payment(new_balance, apr, scheduled_payment)
        before_payoff = (date.today() + timedelta(days=(before_months or 0) * 30)).isoformat() if before_months is not None else None
        after_payoff = (date.today() + timedelta(days=(after_months or 0) * 30)).isoformat() if after_months is not None else None
        months_saved = None
        if before_months is not None and after_months is not None:
            months_saved = max(before_months - after_months, 0)

        return jsonify({
            'target_type': 'debt',
            'target_label': debt.description,
            'apr': apr,
            'target_due_date': debt.payoff_date.isoformat() if debt.payoff_date else None,
            'scheduled_monthly_payment': scheduled_payment,
            'payment_amount': payment_amount,
            'applied_amount': applied_amount,
            'before': {
                'balance': current_balance,
                'estimated_months_remaining': before_months,
                'estimated_payoff_date': before_payoff,
            },
            'after': {
                'balance': new_balance,
                'estimated_months_remaining': after_months,
                'estimated_payoff_date': after_payoff,
            },
            'change': {
                'balance_reduction': current_balance - new_balance,
                'months_saved': months_saved,
            },
            'cashflow': {
                'month_free_cash_before': free_cash_before,
                'month_free_cash_after': free_cash_before - applied_amount,
            }
        })

    if target_type == 'recurring_expense':
        entry = ExpenseEntry.query.get(target_id)
        if not entry or not entry.recurring_id:
            return jsonify({'error': 'Recurring expense not found'}), 404

        remaining_before = _entry_remaining_amount(entry)
        if remaining_before <= 0:
            return jsonify({'error': 'This bill is already fully paid'}), 400

        applied_amount = min(payment_amount, remaining_before)
        remaining_after = max(remaining_before - applied_amount, 0.0)
        paid_before = float(getattr(entry, 'amount_paid', 0) or 0)

        horizon_30 = today + timedelta(days=30)
        due_entries = ExpenseEntry.query.filter(
            ExpenseEntry.recurring_id.isnot(None),
            ExpenseEntry.date >= today,
            ExpenseEntry.date <= horizon_30,
        ).all()
        due_entries = [e for e in due_entries if _entry_remaining_amount(e) > 0.0001]
        due_before = sum(_entry_remaining_amount(e) for e in due_entries)
        due_after = max(due_before - applied_amount, 0.0)

        return jsonify({
            'target_type': 'recurring_expense',
            'target_label': entry.title,
            'payment_amount': payment_amount,
            'applied_amount': applied_amount,
            'before': {
                'remaining_balance': remaining_before,
                'amount_paid': paid_before,
            },
            'after': {
                'remaining_balance': remaining_after,
                'amount_paid': paid_before + applied_amount,
                'fully_paid': remaining_after <= 0.0001,
            },
            'change': {
                'remaining_reduction': remaining_before - remaining_after,
                'due_30_days_before': due_before,
                'due_30_days_after': due_after,
            },
            'cashflow': {
                'month_free_cash_before': free_cash_before,
                'month_free_cash_after': free_cash_before - applied_amount,
            }
        })

    return jsonify({'error': 'Unsupported target type'}), 400


@main_bp.route('/api/expenses/entries/<int:entry_id>', methods=['DELETE'])
def api_delete_expense_entry(entry_id: int):
    """Delete an ExpenseEntry (single instance or full recurring series)."""
    config = current_app.config.get('HOMEHUB_CONFIG', {})
    if config.get('password_hash') and not session.get('authed'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    entry = ExpenseEntry.query.get_or_404(entry_id)
    data = request.get_json(silent=True) or {}
    user = sanitize_text(data.get('user', ''))
    delete_mode = sanitize_text(data.get('delete_mode', 'instance')).lower() or 'instance'

    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if not (user in admin_aliases or user == (entry.payer or '')):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    try:
        recurring_id = entry.recurring_id
        if recurring_id and delete_mode == 'all':
            affected_dates = {row.date for row in ExpenseEntry.query.filter_by(recurring_id=recurring_id).all() if row.date}
            deleted_count = ExpenseEntry.query.filter_by(recurring_id=recurring_id).delete(synchronize_session=False)
            RecurringBillPayment.query.filter_by(recurring_expense_id=recurring_id).delete(synchronize_session=False)
            rule = RecurringExpense.query.get(recurring_id)
            if rule:
                db.session.delete(rule)
            for dt in sorted(affected_dates):
                _sync_budget_for_entry_date(dt)
            message = f'Deleted recurring series ({deleted_count} entr{"y" if deleted_count == 1 else "ies"})'
        else:
            affected_date = entry.date
            db.session.delete(entry)
            _sync_budget_for_entry_date(affected_date)
            message = 'Expense deleted'
        db.session.commit()
        return jsonify({'ok': True, 'message': message}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting expense entry: {e}")
        return jsonify({'ok': False, 'error': 'Failed to delete'}), 500


@main_bp.route('/api/expenses/entries/<int:entry_id>/paid', methods=['POST'])
def api_mark_expense_entry_paid(entry_id: int):
    """Apply payment to recurring expense entry (supports partial/full)."""
    config = current_app.config.get('HOMEHUB_CONFIG', {})
    if config.get('password_hash') and not session.get('authed'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    entry = ExpenseEntry.query.get_or_404(entry_id)
    data = request.get_json(silent=True) or {}
    user = sanitize_text(data.get('user', '')).strip() or sanitize_text(session.get('current_member', '')).strip()
    paid_flag = bool(data.get('paid', True))

    if not _is_known_family_member(user):
        return jsonify({'ok': False, 'error': 'Only family members can update bill payments'}), 403

    if not entry.recurring_id:
        return jsonify({'ok': False, 'error': 'Only recurring expenses can be marked paid'}), 400

    try:
        total_amount = float(entry.amount or 0.0)
        already_paid = float(getattr(entry, 'amount_paid', 0.0) or 0.0)
        remaining_before = max(total_amount - already_paid, 0.0)

        payment_raw = data.get('payment_amount', None)
        payment_amount = None
        if payment_raw not in (None, ''):
            try:
                payment_amount = float(payment_raw)
            except Exception:
                return jsonify({'ok': False, 'error': 'Invalid payment amount'}), 400

        payment_date_raw = sanitize_text(data.get('payment_date', '') or '')
        payment_date = _parse_optional_date(payment_date_raw) or date.today()
        remaining_due_date_raw = sanitize_text(data.get('remaining_due_date', '') or '')
        remaining_due_date = _parse_optional_date(remaining_due_date_raw)

        if not paid_flag:
            entry.amount_paid = 0.0
            action_type = 'unpaid'
            paid_now = 0.0
        else:
            if payment_amount is None:
                paid_now = remaining_before
            else:
                paid_now = payment_amount
            if paid_now <= 0:
                return jsonify({'ok': False, 'error': 'Payment amount must be greater than 0'}), 400
            if paid_now > remaining_before:
                paid_now = remaining_before
            entry.amount_paid = min(total_amount, already_paid + paid_now)
            remaining_after = max(total_amount - float(entry.amount_paid or 0.0), 0.0)
            action_type = 'full' if remaining_after <= 0.0001 else 'partial'

        moved_remaining_to = None
        if paid_flag and action_type == 'partial' and remaining_due_date and remaining_after > 0.0001:
            if remaining_due_date < payment_date:
                return jsonify({'ok': False, 'error': 'Remaining due date must be on or after payment date'}), 400

            target = ExpenseEntry.query.filter_by(recurring_id=entry.recurring_id, date=remaining_due_date).first()
            if target and target.id != entry.id:
                target.amount = float(target.amount or 0.0) + remaining_after
            else:
                target = ExpenseEntry(
                    date=remaining_due_date,
                    title=entry.title,
                    category=entry.category,
                    unit_price=entry.unit_price,
                    quantity=entry.quantity,
                    amount=remaining_after,
                    amount_paid=0.0,
                    payer=entry.payer,
                    include_in_family_total=bool(getattr(entry, 'include_in_family_total', True)),
                    recurring_id=entry.recurring_id,
                    shared_group_id=getattr(entry, 'shared_group_id', None),
                )
                db.session.add(target)

            entry.amount = max(float(entry.amount or 0.0) - remaining_after, 0.0)
            entry.amount_paid = min(float(entry.amount_paid or 0.0), float(entry.amount or 0.0))
            remaining_after = max(float(entry.amount or 0.0) - float(entry.amount_paid or 0.0), 0.0)
            action_type = 'rescheduled_partial'
            moved_remaining_to = remaining_due_date

        total_amount = float(entry.amount or 0.0)

        # Keep monthly payment table in sync for backward compatibility.
        year = int(entry.date.year) if entry.date else date.today().year
        month = int(entry.date.month) if entry.date else date.today().month
        row = RecurringBillPayment.query.filter_by(
            recurring_expense_id=entry.recurring_id,
            year=year,
            month=month,
        ).first()
        if not row:
            row = RecurringBillPayment(
                recurring_expense_id=entry.recurring_id,
                year=year,
                month=month,
            )
            db.session.add(row)
        row.is_paid = bool(max(total_amount - float(entry.amount_paid or 0.0), 0.0) <= 0.0001)
        has_any_payment = bool(float(entry.amount_paid or 0.0) > 0.0001)
        row.paid_by = (user or entry.payer) if has_any_payment else None
        row.paid_date = datetime.combine(payment_date, datetime.min.time()) if has_any_payment else None

        _recalculate_recurring_series_balances(int(entry.recurring_id))

        _sync_budget_for_entry_date(entry.date)
        if moved_remaining_to:
            _sync_budget_for_entry_date(moved_remaining_to)

        db.session.commit()

        remaining_amount = max(total_amount - float(entry.amount_paid or 0.0), 0.0)
        if action_type in {'partial', 'rescheduled_partial'}:
            action_name = 'money.bill.pay.partial' if action_type == 'partial' else 'money.bill.pay.rescheduled'
            summary = (
                f"partially paid bill {entry.title}: ${paid_now:.2f} paid, ${remaining_amount:.2f} remaining"
                if action_type == 'partial'
                else f"paid ${paid_now:.2f} toward {entry.title} and moved ${max(total_amount - float(entry.amount or 0.0), 0.0):.2f} to {moved_remaining_to.isoformat() if moved_remaining_to else 'a new due date'}"
            )
            log_activity(
                actor=user or entry.payer or 'Family',
                action=action_name,
                summary=summary,
                entity_type='expense_entry',
                entity_id=entry.id,
                visibility='family',
                metadata={
                    'recurring_id': entry.recurring_id,
                    'paid_now': round(paid_now, 2),
                    'remaining_amount': round(remaining_amount, 2),
                    'payment_date': payment_date.isoformat(),
                    'remaining_due_date': moved_remaining_to.isoformat() if moved_remaining_to else None,
                    'entry_date': entry.date.isoformat() if entry.date else None,
                },
            )
        elif action_type == 'full':
            log_activity(
                actor=user or entry.payer or 'Family',
                action='money.bill.pay.full',
                summary=f"fully paid bill {entry.title}: ${total_amount:.2f}",
                entity_type='expense_entry',
                entity_id=entry.id,
                visibility='family',
                metadata={
                    'recurring_id': entry.recurring_id,
                    'payment_date': payment_date.isoformat(),
                    'entry_date': entry.date.isoformat() if entry.date else None,
                },
            )
        else:
            log_activity(
                actor=user or entry.payer or 'Family',
                action='money.bill.pay.reset',
                summary=f"reopened bill payment for {entry.title}",
                entity_type='expense_entry',
                entity_id=entry.id,
                visibility='family',
                metadata={
                    'recurring_id': entry.recurring_id,
                    'entry_date': entry.date.isoformat() if entry.date else None,
                },
            )

        return jsonify({
            'ok': True,
            'paid': bool(remaining_amount <= 0.0001),
            'paid_now': round(float(paid_now or 0.0), 2),
            'paid_by': user or entry.payer or '',
            'amount_paid': round(float(entry.amount_paid or 0.0), 2),
            'remaining_amount': round(remaining_amount, 2),
            'payment_date': payment_date.isoformat(),
            'remaining_due_date': moved_remaining_to.isoformat() if moved_remaining_to else None,
            'message': (
                'Marked unpaid'
                if action_type == 'unpaid'
                else (
                    'Bill fully paid'
                    if action_type == 'full'
                    else (
                        f'Partial payment saved (${remaining_amount:.2f} remaining)'
                        if action_type == 'partial'
                        else f'Partial payment saved and remaining moved to {moved_remaining_to.isoformat() if moved_remaining_to else "another date"}'
                    )
                )
            ),
        }), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating paid state for entry {entry_id}: {e}")
        return jsonify({'ok': False, 'error': 'Failed to update paid state'}), 500


@main_bp.route('/api/expenses/recurring/<int:recurring_id>/amount', methods=['POST'])
def api_update_recurring_amount(recurring_id: int):
    """Adjust recurring bill amount from an effective date onward."""
    config = current_app.config.get('HOMEHUB_CONFIG', {})
    if config.get('password_hash') and not session.get('authed'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    rule = RecurringExpense.query.get_or_404(recurring_id)
    data = request.get_json(silent=True) or {}
    user = sanitize_text(data.get('user', '') or '')

    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if not (user in admin_aliases or user == (rule.creator or '')):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    try:
        new_amount = float(data.get('new_amount', data.get('amount', 0)) or 0)
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid amount'}), 400
    if new_amount <= 0:
        return jsonify({'ok': False, 'error': 'Amount must be greater than zero'}), 400

    try:
        starting_balance = float(data.get('starting_balance', getattr(rule, 'starting_balance', 0.0) or 0.0) or 0.0)
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid starting balance'}), 400
    if starting_balance < 0:
        return jsonify({'ok': False, 'error': 'Starting balance cannot be negative'}), 400

    effective_date = _parse_optional_date(sanitize_text(data.get('effective_date', '') or '')) or date.today()

    qty = float(rule.default_quantity or 1.0)
    if qty <= 0:
        qty = 1.0
    rule.unit_price = new_amount / qty
    rule.effective_from = effective_date
    rule.starting_balance = starting_balance

    touched = 0
    future_rows = ExpenseEntry.query.filter(
        ExpenseEntry.recurring_id == recurring_id,
        ExpenseEntry.date >= effective_date,
    ).all()
    touched_dates: set[date] = set()
    for row in future_rows:
        paid = float(getattr(row, 'amount_paid', 0.0) or 0.0)
        row.amount = max(new_amount, paid)
        if row.date:
            touched_dates.add(row.date)
        touched += 1

    _recalculate_recurring_series_balances(recurring_id)

    for dt in touched_dates:
        _sync_budget_for_entry_date(dt)

    db.session.commit()

    log_activity(
        actor=user or rule.creator or 'Family',
        action='money.bill.amount.adjust',
        summary=f"updated recurring amount for {rule.title} to ${new_amount:.2f} effective {effective_date.isoformat()}",
        entity_type='recurring_expense',
        entity_id=rule.id,
        visibility='family',
        metadata={
            'new_amount': round(new_amount, 2),
            'starting_balance': round(starting_balance, 2),
            'effective_date': effective_date.isoformat(),
            'updated_entries': touched,
        },
    )

    return jsonify({
        'ok': True,
        'message': f'Recurring amount updated to ${new_amount:.2f}',
        'updated_entries': touched,
        'effective_date': effective_date.isoformat(),
        'starting_balance': round(starting_balance, 2),
    })


@main_bp.route('/api/expenses/recurring/<int:recurring_id>/history', methods=['GET'])
def api_recurring_bill_history(recurring_id: int):
    """Bill history focused on actionable as-of balance and recent activity."""
    config = current_app.config.get('HOMEHUB_CONFIG', {})
    if config.get('password_hash') and not session.get('authed'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    rule = RecurringExpense.query.get_or_404(recurring_id)
    as_of = _parse_optional_date(request.args.get('as_of')) or date.today()
    try:
        months_limit = int(request.args.get('months') or 6)
    except Exception:
        months_limit = 6
    months_limit = max(1, min(months_limit, 24))

    events: list[dict] = []
    recent_payments: list[dict] = []

    shared_group_id = sanitize_text(getattr(rule, 'shared_group_id', '') or '').strip()
    shared_bill_payload = {
        'is_shared': False,
    }

    starting_balance = float(getattr(rule, 'starting_balance', 0.0) or 0.0)
    current_amount = round(float(rule.unit_price or 0.0) * float(rule.default_quantity or 1.0), 2)
    if starting_balance > 0.0001:
        events.append({
            'date': (rule.start_date.isoformat() if rule.start_date else (rule.effective_from.isoformat() if rule.effective_from else None)),
            'event_type': 'carryover',
            'label': 'Previous balance',
            'direction': 'debit',
            'amount': round(starting_balance, 2),
            'meta': {},
        })

    charge_rows = (
        ExpenseEntry.query
        .filter(ExpenseEntry.recurring_id == recurring_id, ExpenseEntry.date <= as_of)
        .order_by(ExpenseEntry.date.asc(), ExpenseEntry.id.asc())
        .all()
    )
    previous_remaining = max(starting_balance, 0.0)
    for row in charge_rows:
        row_total = max(float(row.amount or 0.0), 0.0)
        # Recurring rows store carryover in amount; only add the incremental monthly charge.
        charge_amount = max(row_total - previous_remaining, 0.0)
        if charge_amount <= 0.0001:
            previous_remaining = max(row_total - float(getattr(row, 'amount_paid', 0.0) or 0.0), 0.0)
            continue
        events.append({
            'date': row.date.isoformat() if row.date else None,
            'event_type': 'charge',
            'label': 'New charge',
            'direction': 'debit',
            'amount': round(charge_amount, 2),
            'meta': {'entry_id': row.id},
        })
        previous_remaining = max(row_total - float(getattr(row, 'amount_paid', 0.0) or 0.0), 0.0)

    payment_logs = (
        ActivityLog.query
        .filter(ActivityLog.action.in_([
            'money.bill.pay.partial',
            'money.bill.pay.rescheduled',
            'money.bill.pay.full',
        ]))
        .order_by(ActivityLog.timestamp.asc(), ActivityLog.id.asc())
        .all()
    )
    for row in payment_logs:
        meta = {}
        try:
            meta = json.loads(row.meta_json or '{}') if row.meta_json else {}
        except Exception:
            meta = {}
        if str(meta.get('recurring_id') or '') != str(recurring_id):
            continue

        payment_date_raw = sanitize_text(meta.get('payment_date', '') or '') or (row.timestamp.date().isoformat() if row.timestamp else '')
        payment_date = _parse_optional_date(payment_date_raw)
        if payment_date and payment_date > as_of:
            continue

        paid_amount = float(meta.get('paid_now', meta.get('payment_amount', 0.0)) or 0.0)
        if paid_amount <= 0.0001:
            continue

        recent_payments.append({
            'payer': sanitize_text(row.actor or '') or 'Family',
            'amount': round(paid_amount, 2),
            'payment_date': payment_date.isoformat() if payment_date else payment_date_raw,
            'recorded_at': row.timestamp.isoformat() if row.timestamp else None,
            'bill_date': sanitize_text(meta.get('entry_date', '') or '') or None,
        })

        events.append({
            'date': payment_date.isoformat() if payment_date else payment_date_raw,
            'event_type': 'payment',
            'label': f"Payment by {sanitize_text(row.actor or '') or 'Family'}",
            'direction': 'credit',
            'amount': round(paid_amount, 2),
            'meta': {
                'action': row.action,
                'payer': sanitize_text(row.actor or '') or 'Family',
                'remaining_due_date': meta.get('remaining_due_date'),
                'entry_date': meta.get('entry_date'),
            },
        })

    if shared_group_id:
        group_rules = (
            RecurringExpense.query
            .filter(RecurringExpense.shared_group_id == shared_group_id)
            .order_by(RecurringExpense.creator.asc(), RecurringExpense.id.asc())
            .all()
        )
        if group_rules:
            shared_bill = None
            shared_bill_id = None
            if shared_group_id.startswith('srb:'):
                try:
                    shared_bill_id = int(shared_group_id.split(':', 1)[1])
                except Exception:
                    shared_bill_id = None
                if shared_bill_id:
                    shared_bill = SharedRecurringBill.query.get(shared_bill_id)

            total_monthly_amount = 0.0
            total_due_as_of = 0.0
            total_paid_as_of = 0.0
            shared_next_due_date = None
            shared_next_due_amount = 0.0
            participants: list[dict] = []

            for group_rule in group_rules:
                split_amount = round(float(group_rule.unit_price or 0.0) * float(group_rule.default_quantity or 1.0), 2)
                total_monthly_amount += split_amount

                latest_entry = (
                    ExpenseEntry.query
                    .filter(ExpenseEntry.recurring_id == group_rule.id, ExpenseEntry.date <= as_of)
                    .order_by(ExpenseEntry.date.desc(), ExpenseEntry.id.desc())
                    .first()
                )
                as_of_due = max(float(getattr(group_rule, 'starting_balance', 0.0) or 0.0), 0.0)
                as_of_paid = 0.0
                as_of_entry_date = None
                if latest_entry:
                    as_of_due = max(float(latest_entry.amount or 0.0) - float(getattr(latest_entry, 'amount_paid', 0.0) or 0.0), 0.0)
                    as_of_paid = max(float(getattr(latest_entry, 'amount_paid', 0.0) or 0.0), 0.0)
                    as_of_entry_date = latest_entry.date.isoformat() if latest_entry.date else None

                next_entry = (
                    ExpenseEntry.query
                    .filter(ExpenseEntry.recurring_id == group_rule.id, ExpenseEntry.date > as_of)
                    .order_by(ExpenseEntry.date.asc(), ExpenseEntry.id.asc())
                    .first()
                )

                next_due_date = next_entry.date.isoformat() if next_entry and next_entry.date else None
                next_due_amount = split_amount
                if next_entry:
                    next_due_amount = round(max(float(next_entry.amount or 0.0) - as_of_due, 0.0), 2)
                    if next_due_amount <= 0.0001:
                        next_due_amount = split_amount

                total_due_as_of += as_of_due
                total_paid_as_of += as_of_paid

                if next_due_date:
                    if (shared_next_due_date is None) or (next_due_date < shared_next_due_date):
                        shared_next_due_date = next_due_date

                participants.append({
                    'member_name': sanitize_text(group_rule.creator or '') or 'Family',
                    'recurring_id': group_rule.id,
                    'is_current_rule': bool(group_rule.id == recurring_id),
                    'split_amount': round(split_amount, 2),
                    'as_of_due': round(as_of_due, 2),
                    'as_of_paid': round(as_of_paid, 2),
                    'as_of_entry_date': as_of_entry_date,
                    'next_due_date': next_due_date,
                    'next_due_amount': round(next_due_amount, 2),
                })

            if shared_next_due_date:
                for p in participants:
                    if p.get('next_due_date') == shared_next_due_date:
                        shared_next_due_amount += float(p.get('next_due_amount') or 0.0)

            participants = sorted(participants, key=lambda p: sanitize_text(p.get('member_name', '') or '').lower())
            current_member = next((p for p in participants if p.get('is_current_rule')), None)

            shared_bill_payload = {
                'is_shared': True,
                'shared_group_id': shared_group_id,
                'bill_id': shared_bill.id if shared_bill else shared_bill_id,
                'title': (shared_bill.title if shared_bill else rule.title),
                'frequency': (shared_bill.frequency if shared_bill else rule.frequency),
                'start_date': (shared_bill.start_date.isoformat() if shared_bill and shared_bill.start_date else (rule.start_date.isoformat() if rule.start_date else None)),
                'total_amount': round(float(shared_bill.total_amount or 0.0), 2) if shared_bill else round(total_monthly_amount, 2),
                'member_name': current_member.get('member_name') if current_member else (sanitize_text(rule.creator or '') or 'Family'),
                'member_split_amount': round(float(current_member.get('split_amount') or 0.0), 2) if current_member else round(current_amount, 2),
                'total_due_as_of': round(total_due_as_of, 2),
                'total_paid_as_of': round(total_paid_as_of, 2),
                'next_due_date': shared_next_due_date,
                'next_due_amount': round(shared_next_due_amount, 2),
                'participants': participants,
            }

    def _sort_key(item: dict):
        raw = sanitize_text(item.get('date', '') or '')
        try:
            d = datetime.strptime(raw, '%Y-%m-%d').date()
        except Exception:
            d = date(1900, 1, 1)
        direction_rank = 0 if item.get('direction') == 'debit' else 1
        return (d, direction_rank)

    events = sorted(events, key=_sort_key)

    monthly_ledger: list[dict] = []
    month_index: dict[str, int] = {}

    def _month_key_label(raw_date: str):
        try:
            parsed = datetime.strptime(raw_date, '%Y-%m-%d').date()
            return parsed.strftime('%Y-%m'), parsed.strftime('%B %Y')
        except Exception:
            return 'unknown', 'Unknown month'

    running_balance = 0.0
    for item in events:
        amount = float(item.get('amount') or 0.0)
        date_raw = sanitize_text(item.get('date', '') or '')
        key, label = _month_key_label(date_raw)

        month_pos = month_index.get(key)
        if month_pos is None:
            month_index[key] = len(monthly_ledger)
            monthly_ledger.append({
                'month': key,
                'label': label,
                'opening_balance': round(running_balance, 2),
                'invoice_total': 0.0,
                'payment_total': 0.0,
                'closing_balance': round(running_balance, 2),
                'status': 'paid' if running_balance <= 0.0001 else 'outstanding',
                'events': [],
            })
            month_pos = month_index[key]

        if item.get('direction') == 'debit':
            running_balance += amount
            monthly_ledger[month_pos]['invoice_total'] += amount
        else:
            running_balance = max(running_balance - amount, 0.0)
            monthly_ledger[month_pos]['payment_total'] += amount

        item['running_balance'] = round(running_balance, 2)

        monthly_ledger[month_pos]['closing_balance'] = round(running_balance, 2)
        monthly_ledger[month_pos]['status'] = 'paid' if running_balance <= 0.0001 else 'outstanding'
        monthly_ledger[month_pos]['events'].append({
            'date': item.get('date'),
            'event_type': item.get('event_type'),
            'label': item.get('label'),
            'direction': item.get('direction'),
            'amount': round(amount, 2),
            'running_balance': round(running_balance, 2),
            'meta': item.get('meta') or {},
        })

    for month in monthly_ledger:
        month['invoice_total'] = round(float(month.get('invoice_total') or 0.0), 2)
        month['payment_total'] = round(float(month.get('payment_total') or 0.0), 2)

    monthly_ledger = sorted(monthly_ledger, key=lambda m: m.get('month', ''), reverse=True)
    visible_monthly_ledger = monthly_ledger[:months_limit]

    total_invoices_all = sum(float(m.get('invoice_total') or 0.0) for m in monthly_ledger)
    total_payments_all = sum(float(m.get('payment_total') or 0.0) for m in monthly_ledger)
    months_outstanding_all = sum(1 for m in monthly_ledger if float(m.get('closing_balance') or 0.0) > 0.0001)

    total_invoices_visible = sum(float(m.get('invoice_total') or 0.0) for m in visible_monthly_ledger)
    total_payments_visible = sum(float(m.get('payment_total') or 0.0) for m in visible_monthly_ledger)
    months_outstanding_visible = sum(1 for m in visible_monthly_ledger if float(m.get('closing_balance') or 0.0) > 0.0001)

    recent_payments = sorted(
        recent_payments,
        key=lambda p: ((p.get('payment_date') or ''), (p.get('recorded_at') or '')),
        reverse=True,
    )[:20]

    next_due_entry = (
        ExpenseEntry.query
        .filter(ExpenseEntry.recurring_id == recurring_id, ExpenseEntry.date > as_of)
        .order_by(ExpenseEntry.date.asc(), ExpenseEntry.id.asc())
        .first()
    )
    next_due_date = next_due_entry.date.isoformat() if next_due_entry and next_due_entry.date else None
    next_due_amount = 0.0
    if next_due_entry:
        next_due_amount = round(max(float(next_due_entry.amount or 0.0) - previous_remaining, 0.0), 2)

    return jsonify({
        'ok': True,
        'as_of_date': as_of.isoformat(),
        'months_limit': months_limit,
        'recurring': {
            'id': rule.id,
            'title': rule.title,
            'current_amount': current_amount,
            'starting_balance': round(starting_balance, 2),
            'next_due_date': next_due_date,
            'next_due_amount': next_due_amount,
        },
        'events': events,
        'monthly_ledger': visible_monthly_ledger,
        'recent_payments': recent_payments,
        'ledger_summary': {
            'months_count': len(visible_monthly_ledger),
            'months_outstanding': months_outstanding_visible,
            'invoices_total': round(total_invoices_visible, 2),
            'payments_total': round(total_payments_visible, 2),
            'all_time': {
                'months_count': len(monthly_ledger),
                'months_outstanding': months_outstanding_all,
                'invoices_total': round(total_invoices_all, 2),
                'payments_total': round(total_payments_all, 2),
            },
        },
        'overall_balance': round(running_balance, 2),
        'shared_bill': shared_bill_payload,
    })


@main_bp.route('/api/expenses/income/<int:income_id>', methods=['DELETE'])
def api_delete_income_entry(income_id: int):
    """Delete a single IncomeEntry quickly (AJAX-friendly)."""
    config = current_app.config.get('HOMEHUB_CONFIG', {})
    if config.get('password_hash') and not session.get('authed'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    income = IncomeEntry.query.get_or_404(income_id)
    data = request.get_json(silent=True) or {}
    user = sanitize_text(data.get('user', ''))

    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if not (user in admin_aliases or user == (income.recipient or '')):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    try:
        db.session.delete(income)
        db.session.commit()
        return jsonify({'ok': True, 'message': 'Income deleted'}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting income entry: {e}")
        return jsonify({'ok': False, 'error': 'Failed to delete'}), 500


# ==================== AUTHENTICATION HELPERS ====================

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


# ==================== FAMILY BUDGETS ====================

@main_bp.route('/expenses/budgets', methods=['GET'])
def list_budgets():
    """Get all family budgets (paginated)"""
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    today = date.today()
    current_month = today.month
    current_year = today.year
    
    budgets = FamilyBudget.query.order_by(
        FamilyBudget.year.desc(),
        FamilyBudget.month.desc()
    ).paginate(page=page, per_page=per_page)

    result = []
    for budget in budgets.items:
        _refresh_budget_category_spent_for_period(int(budget.year), int(budget.month))
        spent_map = _budget_category_spent_map(int(budget.year), int(budget.month))
        spent_total = float(sum(spent_map.values()))
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


@main_bp.route('/expenses/budgets', methods=['POST'])
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


@main_bp.route('/expenses/budgets/<int:budget_id>', methods=['GET'])
def get_budget(budget_id):
    """Get detailed budget with categories and spending"""
    budget = FamilyBudget.query.get_or_404(budget_id)
    _refresh_budget_category_spent_for_period(int(budget.year), int(budget.month))
    spent_map = _budget_category_spent_map(int(budget.year), int(budget.month))
    
    categories = []
    total_spent = 0
    for cat in budget.categories:
        cat_spent = float(spent_map.get((cat.category_name or '').strip() or 'Uncategorized', 0.0))
        cat_data = {
            'id': cat.id,
            'name': cat.category_name,
            'allocated': float(cat.allocated_amount),
            'spent': cat_spent,
            'remaining': float(cat.allocated_amount) - cat_spent,
            'percentage': (cat_spent / float(cat.allocated_amount) * 100) if cat.allocated_amount > 0 else 0,
        }
        categories.append(cat_data)
        total_spent += cat_spent
    
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


@main_bp.route('/expenses/budgets/<int:budget_id>', methods=['PUT'])
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


@main_bp.route('/expenses/budgets/<int:budget_id>/categories', methods=['POST'])
@_require_auth()
def add_budget_category(budget_id):
    """Add or update a category allocation"""
    budget = FamilyBudget.query.get_or_404(budget_id)
    data = request.get_json() or {}
    
    category_name = sanitize_text(data.get('category_name', ''))
    allocated_amount = float(data.get('allocated_amount', 0))
    
    if not category_name or allocated_amount <= 0:
        return jsonify({'error': 'Invalid category or amount'}), 400
    
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


@main_bp.route('/expenses/budgets/<int:budget_id>/categories/<int:cat_id>', methods=['PUT'])
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


@main_bp.route('/expenses/budgets/<int:budget_id>/categories/<int:cat_id>', methods=['DELETE'])
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

@main_bp.route('/expenses/shared-expenses', methods=['GET'])
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
            'receipt_filename': getattr(exp, 'receipt_filename', None),
            'receipt_url': f"/uploads/{exp.receipt_filename}" if getattr(exp, 'receipt_filename', None) else None,
        })
    
    return jsonify({'expenses': result})


@main_bp.route('/expenses/shared-expenses', methods=['POST'])
@_require_auth()
def create_shared_expense():
    """Create a shared expense and split it among participants"""
    data = request.get_json() or {}
    
    description = sanitize_text(data.get('description', ''))
    total_amount = float(data.get('total_amount', 0))
    paid_by = sanitize_text(data.get('paid_by', ''))
    expense_date = data.get('date')
    category = data.get('category', '')
    participants = data.get('participants', [])
    track_in_budgets = bool(data.get('track_in_budgets', False))
    recurring_monthly = bool(data.get('recurring_monthly', False))
    
    if not description or total_amount <= 0 or not paid_by:
        return jsonify({'error': 'Missing required fields'}), 400
    
    try:
        expense_date = datetime.strptime(expense_date, '%Y-%m-%d').date() if expense_date else date.today()
    except:
        expense_date = date.today()
    
    participants_total = sum(p.get('amount', 0) for p in participants)
    if abs(participants_total - total_amount) > 0.01:
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

    created_entry_ids = []
    if track_in_budgets:
        for part in participants:
            member_name = sanitize_text(part.get('name', '') or '')
            share_amount = float(part.get('amount', 0) or 0)
            if not member_name or share_amount <= 0:
                continue

            recurring_rule_id = None
            if recurring_monthly:
                recurring_rule = RecurringExpense(
                    title=description,
                    unit_price=share_amount,
                    default_quantity=1.0,
                    frequency='monthly',
                    category=sanitize_text(category or 'Rent'),
                    monthly_mode='day_of_month',
                    start_date=expense_date,
                    creator=member_name,
                    include_in_family_total=True,
                )
                db.session.add(recurring_rule)
                db.session.flush()
                recurring_rule_id = recurring_rule.id

            budget_entry = ExpenseEntry(
                date=expense_date,
                title=description,
                category=sanitize_text(category or 'Shared Bill'),
                unit_price=share_amount,
                quantity=1.0,
                amount=share_amount,
                amount_paid=0.0,
                payer=member_name,
                include_in_family_total=True,
                recurring_id=recurring_rule_id,
            )
            db.session.add(budget_entry)
            db.session.flush()
            created_entry_ids.append(budget_entry.id)

    if track_in_budgets:
        _sync_budget_for_entry_date(expense_date)

    db.session.commit()

    log_activity(
        actor=paid_by or 'Family',
        action='money.shared_expense.add',
        summary=f"added a shared expense: {description} (${total_amount:.2f})",
        entity_type='shared_expense',
        entity_id=expense.id,
        visibility='family',
        metadata={'category': category, 'date': expense_date.isoformat() if expense_date else None},
    )
    
    return jsonify({
        'id': expense.id,
        'message': 'Shared expense created',
        'budget_entries_created': len(created_entry_ids),
        'recurring_monthly': recurring_monthly,
    }), 201


@main_bp.route('/api/expenses/shared-recurring-bills', methods=['GET'])
def list_shared_recurring_bills():
    rows = (
        SharedRecurringBill.query
        .filter_by(active=True)
        .order_by(SharedRecurringBill.created_at.desc(), SharedRecurringBill.id.desc())
        .all()
    )
    result = []
    for row in rows:
        participants = (
            SharedRecurringBillParticipant.query
            .filter_by(bill_id=row.id)
            .order_by(SharedRecurringBillParticipant.member_name.asc())
            .all()
        )
        result.append({
            'id': row.id,
            'title': row.title,
            'category': row.category,
            'total_amount': float(row.total_amount or 0.0),
            'frequency': row.frequency,
            'start_date': row.start_date.isoformat() if row.start_date else None,
            'end_date': row.end_date.isoformat() if row.end_date else None,
            'active': bool(row.active),
            'created_by': row.created_by,
            'participants': [
                {
                    'member_name': p.member_name,
                    'amount': float(p.amount or 0.0),
                    'recurring_expense_id': p.recurring_expense_id,
                }
                for p in participants
            ],
        })
    return jsonify({'ok': True, 'items': result})


@main_bp.route('/api/expenses/shared-recurring-bills', methods=['POST'])
@_require_auth()
def create_shared_recurring_bill():
    data = request.get_json() or {}

    title = sanitize_text(data.get('title', '')).strip()
    category = sanitize_text(data.get('category', '')).strip() or 'Shared Bill'
    created_by = sanitize_text(data.get('created_by', '')).strip() or sanitize_text(session.get('current_member', '')).strip() or 'Administrator'
    frequency = sanitize_text(data.get('frequency', 'monthly')).strip().lower() or 'monthly'
    start_date_raw = sanitize_text(data.get('start_date', '')).strip()
    end_date_raw = sanitize_text(data.get('end_date', '')).strip()
    participants_raw = data.get('participants', []) or []

    try:
        total_amount = float(data.get('total_amount', 0) or 0)
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid total amount'}), 400

    if not title or total_amount <= 0:
        return jsonify({'ok': False, 'error': 'Title and total amount are required'}), 400
    if frequency not in {'weekly', 'biweekly', 'monthly'}:
        return jsonify({'ok': False, 'error': 'Frequency must be weekly, biweekly, or monthly'}), 400

    start_date = _parse_optional_date(start_date_raw)
    end_date = _parse_optional_date(end_date_raw)
    if not start_date:
        start_date = date.today()
    if end_date and end_date < start_date:
        return jsonify({'ok': False, 'error': 'End date cannot be before start date'}), 400

    if not isinstance(participants_raw, list) or len(participants_raw) < 2:
        return jsonify({'ok': False, 'error': 'Add at least two participant splits'}), 400

    known_members = set(_get_family_member_names())
    participants: list[dict] = []
    seen_names: set[str] = set()
    split_total = 0.0

    for item in participants_raw:
        name = sanitize_text((item or {}).get('member_name', '')).strip()
        try:
            amount = float((item or {}).get('amount', 0) or 0)
        except Exception:
            return jsonify({'ok': False, 'error': f'Invalid split amount for {name or "participant"}'}), 400
        if not name:
            continue
        if name in seen_names:
            return jsonify({'ok': False, 'error': f'Duplicate participant: {name}'}), 400
        if amount <= 0:
            return jsonify({'ok': False, 'error': f'Split amount must be > 0 for {name}'}), 400
        if known_members and name not in known_members:
            return jsonify({'ok': False, 'error': f'{name} is not a known family member'}), 400
        seen_names.add(name)
        split_total += amount
        participants.append({'member_name': name, 'amount': amount})

    if len(participants) < 2:
        return jsonify({'ok': False, 'error': 'Add at least two valid participant splits'}), 400

    if abs(split_total - total_amount) > 0.01:
        return jsonify({'ok': False, 'error': 'Participant split total must match bill total'}), 400

    bill = SharedRecurringBill(
        title=title,
        category=category,
        total_amount=round(total_amount, 2),
        frequency=frequency,
        start_date=start_date,
        end_date=end_date,
        active=True,
        created_by=created_by,
    )
    db.session.add(bill)
    db.session.flush()

    shared_group_id = f"srb:{bill.id}"
    for part in participants:
        rule = RecurringExpense(
            title=title,
            unit_price=float(part['amount']),
            default_quantity=1.0,
            frequency=frequency,
            category=category,
            monthly_mode='day_of_month',
            start_date=start_date,
            end_date=end_date,
            creator=part['member_name'],
            include_in_family_total=True,
            shared_group_id=shared_group_id,
            effective_from=start_date,
            starting_balance=0.0,
        )
        db.session.add(rule)
        db.session.flush()

        participant_row = SharedRecurringBillParticipant(
            bill_id=bill.id,
            member_name=part['member_name'],
            amount=float(part['amount']),
            recurring_expense_id=rule.id,
        )
        db.session.add(participant_row)
        # Create first occurrence immediately so the calendar reflects the split now.
        first_exists = ExpenseEntry.query.filter_by(recurring_id=rule.id, date=start_date).first()
        if not first_exists:
            db.session.add(ExpenseEntry(
                date=start_date,
                title=title,
                category=category,
                unit_price=float(part['amount']),
                quantity=1.0,
                amount=float(part['amount']),
                amount_paid=0.0,
                payer=part['member_name'],
                include_in_family_total=True,
                recurring_id=rule.id,
                shared_group_id=shared_group_id,
            ))

    # Keep this endpoint atomic: avoid nested commit inside recurring generation.
    _generate_recurring_entries_until(max(date.today(), start_date), auto_commit=False)
    _sync_budget_for_entry_date(start_date)

    log_activity(
        actor=created_by,
        action='money.shared_bill.recurring.add',
        summary=f"created shared recurring bill {title} (${total_amount:.2f})",
        entity_type='shared_recurring_bill',
        entity_id=bill.id,
        visibility='family',
        metadata={
            'frequency': frequency,
            'category': category,
            'start_date': start_date.isoformat() if start_date else None,
            'end_date': end_date.isoformat() if end_date else None,
            'participants': participants,
            'shared_group_id': shared_group_id,
        },
    )

    db.session.commit()

    return jsonify({
        'ok': True,
        'id': bill.id,
        'message': 'Shared recurring bill created',
        'bill': {
            'id': bill.id,
            'title': bill.title,
            'category': bill.category,
            'total_amount': float(bill.total_amount or 0.0),
            'frequency': bill.frequency,
            'start_date': bill.start_date.isoformat() if bill.start_date else None,
            'end_date': bill.end_date.isoformat() if bill.end_date else None,
            'participants': participants,
            'shared_group_id': shared_group_id,
        }
    }), 201


@main_bp.route('/expenses/shared-expenses/<int:expense_id>', methods=['PUT'])
@_require_auth()
def update_shared_expense(expense_id):
    """Update shared expense"""
    expense = SharedExpense.query.get_or_404(expense_id)
    data = request.get_json() or {}
    user = sanitize_text(data.get('user', '') or '')

    if not _can_manage_shared_expense(user, expense):
        return jsonify({'error': 'Not allowed'}), 403
    
    if 'description' in data:
        expense.description = sanitize_text(data['description'])
    if 'category' in data:
        expense.category = sanitize_text(data['category'])
    if 'date' in data:
        try:
            expense.date = datetime.strptime(data['date'], '%Y-%m-%d').date()
        except Exception:
            pass
    if 'settled' in data:
        expense.settled = bool(data['settled'])
    
    expense.updated_at = datetime.utcnow()
    db.session.commit()
    
    return jsonify({'message': 'Expense updated'})


@main_bp.route('/expenses/shared-expenses/<int:expense_id>/receipt', methods=['POST'])
@_require_auth()
def upload_shared_expense_receipt(expense_id):
    """Attach a receipt file to a shared expense."""
    expense = SharedExpense.query.get_or_404(expense_id)
    user = sanitize_text(request.form.get('user', '') or '')
    if not _can_manage_shared_expense(user, expense):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    file = request.files.get('receipt')
    if not file or not getattr(file, 'filename', ''):
        return jsonify({'ok': False, 'error': 'No receipt file provided'}), 400

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    uploads_dir = os.path.join(base_dir, 'uploads')
    os.makedirs(uploads_dir, exist_ok=True)

    safe_name = secure_filename(file.filename)
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in {'.jpg', '.jpeg', '.png', '.webp', '.pdf'}:
        return jsonify({'ok': False, 'error': 'Unsupported receipt file type'}), 400

    stored = f"shared_expense_{expense.id}_{uuid4().hex[:10]}{ext}"
    file.save(os.path.join(uploads_dir, stored))
    expense.receipt_filename = stored
    expense.updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'ok': True, 'receipt_filename': stored, 'receipt_url': f'/uploads/{stored}'})


@main_bp.route('/expenses/shared-expenses/<int:expense_id>', methods=['DELETE'])
@_require_auth()
def delete_shared_expense(expense_id):
    """Delete a shared expense"""
    expense = SharedExpense.query.get_or_404(expense_id)
    data = request.get_json(silent=True) or {}
    user = sanitize_text(data.get('user', '') or '')
    if not _can_manage_shared_expense(user, expense):
        return jsonify({'error': 'Not allowed'}), 403
    db.session.delete(expense)
    db.session.commit()
    
    return jsonify({'message': 'Expense deleted'})


# ==================== DEBT TRACKING ====================

@main_bp.route('/expenses/debts', methods=['GET'])
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


@main_bp.route('/expenses/debts', methods=['POST'])
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


@main_bp.route('/expenses/debts/<int:debt_id>/payments', methods=['POST'])
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
    
    debt.current_balance = max(0, debt.current_balance - principal)
    
    if debt.current_balance <= 0:
        debt.active = False
    
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


@main_bp.route('/expenses/debts/<int:debt_id>', methods=['DELETE'])
@_require_auth()
def delete_debt(debt_id):
    """Delete a debt account"""
    debt = DebtAccount.query.get_or_404(debt_id)
    
    DebtPayment.query.filter_by(debt_id=debt_id).delete()
    db.session.delete(debt)
    db.session.commit()
    
    return jsonify({'message': 'Debt deleted'})


# ==================== FINANCIAL GOALS ====================

@main_bp.route('/expenses/savings/add', methods=['POST'])
@_require_auth()
def add_savings_contribution():
    """Simple savings action for collaboration hub (explicit/manual only)."""
    data = request.get_json() or {}

    amount = float(data.get('amount', 0) or 0)
    contributor = sanitize_text(data.get('contributor', '') or '')
    notes = sanitize_text(data.get('notes', '') or '')
    contribution_date_raw = data.get('date')

    if amount <= 0:
        return jsonify({'error': 'Amount must be greater than zero'}), 400

    if not contributor:
        contributor = 'Administrator'

    try:
        contribution_date = datetime.strptime(contribution_date_raw, '%Y-%m-%d').date() if contribution_date_raw else date.today()
    except Exception:
        contribution_date = date.today()

    goal = (
        FinancialGoal.query
        .filter(FinancialGoal.active == True)
        .filter(db.func.lower(FinancialGoal.category) == 'savings')
        .order_by(FinancialGoal.updated_at.desc(), FinancialGoal.id.desc())
        .first()
    )

    if not goal:
        goal = FinancialGoal(
            goal_name='Family Savings',
            description='Manual savings contributions from Collaboration Hub',
            target_amount=1_000_000_000.0,
            current_amount=0.0,
            target_date=None,
            category='savings',
            priority=2,
            created_by=contributor,
            active=True,
        )
        db.session.add(goal)
        db.session.flush()

    goal.current_amount = float(goal.current_amount or 0) + amount

    tagged_notes = HUB_SAVINGS_NOTE_PREFIX if not notes else f"{HUB_SAVINGS_NOTE_PREFIX} {notes}"

    contribution = GoalContribution(
        goal_id=goal.id,
        contribution_date=contribution_date,
        amount=amount,
        contributor=contributor,
        notes=tagged_notes,
    )
    db.session.add(contribution)
    db.session.commit()

    savings_total = _manual_hub_savings_total()

    return jsonify({
        'ok': True,
        'message': 'Savings updated',
        'savings_total': round(savings_total, 2),
        'contribution': {
            'id': contribution.id,
            'amount': float(contribution.amount or 0),
            'date': contribution.contribution_date.isoformat() if contribution.contribution_date else None,
            'contributor': contribution.contributor,
        },
    }), 201


@main_bp.route('/expenses/savings/entries', methods=['GET'])
@_require_auth()
def list_savings_contributions_for_audit():
    """Admin audit endpoint for manual hub savings entries."""
    user = sanitize_text(request.args.get('user', '') or '')
    if not _is_admin_user(user):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    rows = (
        GoalContribution.query
        .join(FinancialGoal, FinancialGoal.id == GoalContribution.goal_id)
        .filter(FinancialGoal.active == True)
        .filter(db.func.lower(FinancialGoal.category) == 'savings')
        .filter(GoalContribution.notes.like(f"{HUB_SAVINGS_NOTE_PREFIX}%"))
        .order_by(GoalContribution.contribution_date.desc(), GoalContribution.id.desc())
        .all()
    )

    items = []
    for row in rows:
        note = (row.notes or '').strip()
        clean_note = note[len(HUB_SAVINGS_NOTE_PREFIX):].strip() if note.startswith(HUB_SAVINGS_NOTE_PREFIX) else note
        items.append({
            'id': row.id,
            'goal_id': row.goal_id,
            'date': row.contribution_date.isoformat() if row.contribution_date else None,
            'amount': float(row.amount or 0),
            'contributor': row.contributor,
            'notes': clean_note,
        })

    return jsonify({'ok': True, 'items': items})


@main_bp.route('/expenses/savings/entries/<int:contribution_id>', methods=['DELETE'])
@_require_auth()
def delete_savings_contribution_for_audit(contribution_id: int):
    """Admin-only removal for mistaken manual hub savings entries."""
    data = request.get_json(silent=True) or {}
    user = sanitize_text(data.get('user', '') or '')
    if not _is_admin_user(user):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    contribution = GoalContribution.query.get_or_404(contribution_id)
    goal = FinancialGoal.query.get(contribution.goal_id)

    note = (contribution.notes or '').strip()
    if not note.startswith(HUB_SAVINGS_NOTE_PREFIX):
        return jsonify({'ok': False, 'error': 'Only hub savings entries can be removed here'}), 400

    amount = float(contribution.amount or 0)
    if goal:
        goal.current_amount = max(0.0, float(goal.current_amount or 0) - amount)

    db.session.delete(contribution)
    db.session.commit()

    return jsonify({
        'ok': True,
        'message': 'Savings entry removed',
        'savings_total': round(_manual_hub_savings_total(), 2),
    })

@main_bp.route('/expenses/goals', methods=['GET'])
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


@main_bp.route('/expenses/goals', methods=['POST'])
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


@main_bp.route('/expenses/goals/<int:goal_id>/contributions', methods=['POST'])
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
    
    goal.current_amount = min(goal.target_amount, float(goal.current_amount) + amount)
    
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


@main_bp.route('/api/expenses/category-rules', methods=['GET'])
def list_category_rules():
    rows = (
        TransactionCategoryRule.query
        .order_by(TransactionCategoryRule.priority.asc(), TransactionCategoryRule.id.asc())
        .all()
    )
    return jsonify({
        'ok': True,
        'rules': [
            {
                'id': row.id,
                'rule_name': row.rule_name,
                'match_field': row.match_field,
                'match_type': row.match_type,
                'match_value': row.match_value,
                'category': row.category,
                'active': bool(row.active),
                'priority': int(row.priority or 100),
                'created_by': row.created_by,
            }
            for row in rows
        ]
    })


@main_bp.route('/api/expenses/category-rules', methods=['POST'])
@_require_auth()
def create_category_rule():
    data = request.get_json() or {}
    rule_name = sanitize_text(data.get('rule_name', '')).strip() or 'Rule'
    match_field = sanitize_text(data.get('match_field', 'merchant')).strip().lower()
    match_type = sanitize_text(data.get('match_type', 'contains')).strip().lower()
    match_value = sanitize_text(data.get('match_value', '')).strip()
    category = sanitize_text(data.get('category', '')).strip()
    created_by = sanitize_text(data.get('created_by', session.get('current_member', '') or 'Administrator')).strip() or 'Administrator'
    priority = int(data.get('priority', 100) or 100)

    if match_field not in {'merchant', 'description', 'title'}:
        return jsonify({'ok': False, 'error': 'match_field must be merchant, description, or title'}), 400
    if match_type not in {'contains', 'equals', 'startswith', 'endswith', 'normalized_equals'}:
        return jsonify({'ok': False, 'error': 'match_type must be contains, equals, startswith, endswith, or normalized_equals'}), 400
    if not match_value or not category:
        return jsonify({'ok': False, 'error': 'match_value and category are required'}), 400

    row = TransactionCategoryRule(
        rule_name=rule_name,
        match_field=match_field,
        match_type=match_type,
        match_value=match_value,
        category=category,
        active=bool(data.get('active', True)),
        priority=priority,
        created_by=created_by,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({'ok': True, 'id': row.id, 'message': 'Category rule created'}), 201


@main_bp.route('/api/expenses/category-rules/<int:rule_id>', methods=['DELETE'])
@_require_auth()
def delete_category_rule(rule_id: int):
    row = TransactionCategoryRule.query.get_or_404(rule_id)
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True, 'message': 'Category rule deleted'})


@main_bp.route('/api/expenses/plaid/transactions/<int:tx_id>/category', methods=['POST'])
@_require_auth()
def set_plaid_transaction_category(tx_id: int):
    """Override one Plaid transaction category and optionally persist a reusable rule."""
    row = PlaidTransaction.query.get_or_404(tx_id)
    data = request.get_json(silent=True) or {}
    category = sanitize_text(data.get('category', '')).strip()
    if not category:
        return jsonify({'ok': False, 'error': 'category is required'}), 400

    persist_override = bool(data.get('persist_override', True))
    created_by = sanitize_text(data.get('created_by', '')).strip() or sanitize_text(session.get('current_member', '')).strip() or 'Administrator'

    row.category = category
    rule_id = None
    if persist_override:
        merchant = sanitize_text(row.merchant or '').strip()
        description = sanitize_text(row.description or '').strip()
        if merchant:
            match_field = 'merchant'
            match_type = 'normalized_equals'
            match_value = merchant
        else:
            match_field = 'description'
            match_type = 'contains'
            match_value = description[:120]

        if match_value:
            existing = TransactionCategoryRule.query.filter_by(
                match_field=match_field,
                match_type=match_type,
                match_value=match_value,
                category=category,
                active=True,
            ).first()
            if existing:
                rule_id = existing.id
            else:
                rule = TransactionCategoryRule(
                    rule_name=f"Auto override: {merchant or description or 'Plaid transaction'}",
                    match_field=match_field,
                    match_type=match_type,
                    match_value=match_value,
                    category=category,
                    active=True,
                    priority=50,
                    created_by=created_by,
                )
                db.session.add(rule)
                db.session.flush()
                rule_id = rule.id

    db.session.commit()
    return jsonify({
        'ok': True,
        'message': 'Transaction category updated',
        'transaction_id': row.id,
        'category': row.category,
        'persist_override': persist_override,
        'rule_id': rule_id,
    })


# ==================== PLAID INTEGRATION ====================

@main_bp.route('/expenses/plaid/sdk.js', methods=['GET'])
@_require_auth()
def plaid_sdk_proxy():
    """Serve Plaid Link SDK via backend proxy to avoid browser-side CDN blocking."""
    sdk_url = 'https://cdn.plaid.com/link/v2/stable/link-initialize.js'
    try:
        resp = requests.get(sdk_url, timeout=15)
        resp.raise_for_status()
        proxy_resp = Response(resp.text, mimetype='application/javascript')
        proxy_resp.headers['Cache-Control'] = 'public, max-age=3600'
        proxy_resp.headers['X-Source'] = 'plaid-cdn-proxy'
        return proxy_resp
    except Exception as e:
        logger.error(f"Error fetching Plaid SDK via proxy: {e}")
        return jsonify({'error': 'Unable to load Plaid SDK'}), 502

@main_bp.route('/expenses/plaid/link-token', methods=['POST'])
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


@main_bp.route('/expenses/plaid/exchange-token', methods=['POST'])
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


@main_bp.route('/expenses/plaid/accounts', methods=['GET'])
def list_plaid_accounts():
    """Get linked Plaid accounts"""
    accounts = PlaidAccount.query.filter_by(active=True).all()
    privacy_mode = _is_privacy_mode()
    
    result = []
    for account in accounts:
        balance_value = float(account.balance) if account.balance else 0
        if privacy_mode:
            balance_value = _privacy_bucket(f"plaid-account:{account.id}", 1000, 120000, 1000)
        result.append({
            'id': account.id,
            'user': account.user,
            'account_name': account.account_name,
            'account_type': account.account_type,
            'balance': balance_value,
            'currency': account.currency,
            'synced_at': account.synced_at.isoformat() if account.synced_at else None,
        })
    
    return jsonify({'accounts': result})


@main_bp.route('/expenses/plaid/accounts/<int:account_id>/sync', methods=['POST'])
@_require_auth()
def sync_plaid_account(account_id):
    """Sync transactions and balances for a Plaid account"""
    account = PlaidAccount.query.get_or_404(account_id)
    
    if not plaid_service.is_configured():
        return jsonify({'error': 'Plaid is not configured'}), 503
    
    trans_count = plaid_service.sync_transactions(account_id)
    balance = plaid_service.sync_balances(account_id)
    
    return jsonify({
        'message': 'Sync completed',
        'transactions_synced': trans_count,
        'balance': balance,
    })


@main_bp.route('/expenses/plaid/accounts/<int:account_id>/transactions', methods=['GET'])
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
    privacy_mode = _is_privacy_mode()
    for trans in transactions:
        amount_value = float(trans.amount)
        description_value = trans.description
        merchant_value = trans.merchant
        if privacy_mode:
            amount_value = _privacy_bucket(f"plaid-tx:{trans.id}", 5, 500, 5)
            description_value = 'Transaction'
            merchant_value = None
        result.append({
            'id': trans.id,
            'date': trans.transaction_date.isoformat(),
            'amount': amount_value,
            'description': description_value,
            'merchant': merchant_value,
            'category': trans.category,
            'pending': trans.pending,
        })
    
    return jsonify({'transactions': result})


@main_bp.route('/expenses/plaid/accounts/<int:account_id>', methods=['DELETE'])
@_require_auth()
def disconnect_plaid_account(account_id):
    """Disconnect a Plaid account"""
    if plaid_service.disconnect_account(account_id):
        return jsonify({'message': 'Account disconnected'})
    else:
        return jsonify({'error': 'Failed to disconnect account'}), 500


# ==================== ANALYTICS & REPORTING ====================

@main_bp.route('/expenses/analytics/summary', methods=['GET'])
def get_analytics_summary():
    """Get comprehensive financial analytics summary"""
    try:
        y, m = _parse_year_month_query()
    except ValueError:
        return jsonify({'error': 'Invalid month parameter. Must be an integer between 1 and 12.'}), 400

    _, month_end = _month_start_end(y, m)
    _generate_recurring_entries_until(max(date.today(), month_end))
    _generate_recurring_income_until(date.today())

    month_snapshot = _monthly_finance_snapshot(y, m)
    month_history = month_snapshot.get('history', {})
    accounting = month_snapshot.get('family', {})
    expense_paid = float(accounting.get('expenses', {}).get('paid_total', 0.0))
    expense_due = float(accounting.get('expenses', {}).get('due_total', 0.0))
    expense_charged = float(accounting.get('expenses', {}).get('charged_total', 0.0))

    current_budget = month_snapshot.get('budget', {}).get('row')
    if current_budget:
        _refresh_budget_category_spent_for_period(int(y), int(m))

    active_debts = DebtAccount.query.filter_by(active=True).all()
    goals = FinancialGoal.query.filter_by(active=True).all()
    goal_total_target = sum(float(g.target_amount or 0) for g in goals)
    goal_total_current = sum(float(g.current_amount or 0) for g in goals)
    total_debt = sum(float(d.current_balance or 0) for d in active_debts)
    linked_accounts = PlaidAccount.query.filter_by(active=True).all()
    linked_balance = sum(float(a.balance or 0.0) for a in linked_accounts)

    response = {
        'period': f"{m}/{y}",
        'budget': {
            'total': float(current_budget.total_amount) if current_budget else 0,
            'spent': expense_paid + expense_due,
            'paid': expense_paid,
            'due': expense_due,
            'charged': expense_charged,
            'remaining': float(current_budget.total_amount) - (expense_paid + expense_due) if current_budget else 0,
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
            'individual': len([x for x in (month_history.get('items') or []) if (x.get('entry_type') or '') == 'expense']),
            'shared': len([x for x in (month_history.get('items') or []) if bool(x.get('is_shared_bill'))]),
        },
        'accounts': {
            'linked_count': len(linked_accounts),
            'linked_balance': linked_balance,
        },
        'position': {
            'net_cashflow': float(accounting.get('net', {}).get('expected_total', 0.0)),
            'net_worth_proxy': linked_balance - total_debt,
        },
    }

    if _is_privacy_mode():
        seed = f"analytics:{y}:{m}"
        fake_budget_total = _privacy_bucket(seed + ':budget', 100000, 800000, 10000)
        fake_budget_spent = _privacy_bucket(seed + ':spent', 10000, int(fake_budget_total), 10000)
        fake_debt = _privacy_bucket(seed + ':debt', 10000, 600000, 10000)
        fake_goal_target = _privacy_bucket(seed + ':goal-target', 100000, 1000000, 10000)
        fake_goal_current = min(_privacy_bucket(seed + ':goal-current', 10000, 900000, 10000), fake_goal_target)
        fake_balance = _privacy_bucket(seed + ':balance', 10000, 900000, 10000)
        response['budget'].update({
            'total': fake_budget_total,
            'spent': fake_budget_spent,
            'paid': fake_budget_spent,
            'due': 0.0,
            'charged': fake_budget_spent,
            'remaining': max(fake_budget_total - fake_budget_spent, 0.0),
        })
        response['debts']['total_balance'] = fake_debt
        response['goals'].update({
            'target_total': fake_goal_target,
            'current_total': fake_goal_current,
            'progress': (fake_goal_current / fake_goal_target * 100) if fake_goal_target > 0 else 0,
        })
        response['accounts']['linked_balance'] = fake_balance
        response['position'].update({
            'net_cashflow': fake_goal_current - fake_budget_spent,
            'net_worth_proxy': fake_balance - fake_debt,
        })

    return jsonify(response)


# ==================== PLAID ACCOUNT ANALYTICS ====================

@main_bp.route('/expenses/accounts/summary', methods=['GET'])
def get_accounts_summary():
    """Get summary of all linked Plaid accounts with balances"""
    accounts = PlaidAccount.query.filter_by(active=True).all()
    
    total_balance = 0
    privacy_mode = _is_privacy_mode()
    account_details = []
    
    for account in accounts:
        balance = float(account.balance) if account.balance else 0
        if privacy_mode:
            balance = _privacy_bucket(f"plaid-analytics:{account.id}", 1000, 200000, 1000)
        total_balance += balance
        
        # Get recent transactions for this account
        recent_trans = PlaidTransaction.query.filter_by(
            plaid_account_id=account.id
        ).order_by(PlaidTransaction.transaction_date.desc()).limit(5).all()
        
        account_details.append({
            'id': account.id,
            'name': account.account_name,
            'type': account.account_type,
            'subtype': account.account_subtype,
            'balance': balance,
            'currency': account.currency,
            'mask': account.account_id[-4:] if len(account.account_id) >= 4 else account.account_id,
            'synced_at': account.synced_at.isoformat() if account.synced_at else None,
            'recent_transactions': len(recent_trans),
        })
    
    return jsonify({
        'total_accounts': len(accounts),
        'total_balance': total_balance,
        'accounts': account_details,
    })


@main_bp.route('/expenses/accounts/<int:account_id>/summary', methods=['GET'])
def get_account_summary(account_id):
    """Get detailed summary for a specific Plaid account"""
    account = PlaidAccount.query.get_or_404(account_id)
    
    # Get transactions for various time periods
    today = date.today()
    month_start = date(today.year, today.month, 1)
    year_start = date(today.year, 1, 1)
    
    # Current month transactions
    month_trans = PlaidTransaction.query.filter(
        PlaidTransaction.plaid_account_id == account_id,
        PlaidTransaction.transaction_date >= month_start,
        PlaidTransaction.transaction_date <= today
    ).all()
    
    # Year-to-date transactions
    year_trans = PlaidTransaction.query.filter(
        PlaidTransaction.plaid_account_id == account_id,
        PlaidTransaction.transaction_date >= year_start,
        PlaidTransaction.transaction_date <= today
    ).all()
    
    # Category breakdown
    category_breakdown = {}
    for trans in month_trans:
        spend_amount = _plaid_spending_amount(trans, include_pending=False)
        if spend_amount <= 0:
            continue
        category = trans.category or 'Uncategorized'
        if category not in category_breakdown:
            category_breakdown[category] = {'count': 0, 'total': 0}
        category_breakdown[category]['count'] += 1
        category_breakdown[category]['total'] += spend_amount
    
    # Sort by total
    sorted_categories = sorted(
        category_breakdown.items(),
        key=lambda x: x[1]['total'],
        reverse=True
    )
    
    month_total = sum(_plaid_spending_amount(t, include_pending=False) for t in month_trans)
    year_total = sum(_plaid_spending_amount(t, include_pending=False) for t in year_trans)
    
    response = {
        'account': {
            'id': account.id,
            'name': account.account_name,
            'type': account.account_type,
            'balance': float(account.balance) if account.balance else 0,
            'currency': account.currency,
        },
        'period': {
            'month': {
                'total_transactions': len(month_trans),
                'total_spent': month_total,
                'average_transaction': month_total / len(month_trans) if month_trans else 0,
            },
            'year': {
                'total_transactions': len(year_trans),
                'total_spent': year_total,
                'average_transaction': year_total / len(year_trans) if year_trans else 0,
            }
        },
        'top_categories': [
            {
                'category': cat,
                'count': data['count'],
                'total': data['total'],
                'average': data['total'] / data['count']
            }
            for cat, data in sorted_categories[:10]
        ],
    }

    if _is_privacy_mode():
        seed = f"plaid-account-summary:{account.id}"
        fake_balance = _privacy_bucket(seed + ':balance', 1000, 150000, 1000)
        fake_month = _privacy_bucket(seed + ':month', 1000, 50000, 1000)
        fake_year = _privacy_bucket(seed + ':year', int(fake_month), 200000, 1000)
        response['account']['balance'] = fake_balance
        response['period']['month']['total_spent'] = fake_month
        response['period']['month']['average_transaction'] = fake_month / max(response['period']['month']['total_transactions'], 1)
        response['period']['year']['total_spent'] = fake_year
        response['period']['year']['average_transaction'] = fake_year / max(response['period']['year']['total_transactions'], 1)
        response['top_categories'] = []

    return jsonify(response)


@main_bp.route('/expenses/accounts/<int:account_id>/transactions', methods=['GET'])
def get_account_transactions(account_id):
    """Get paginated transactions for a specific account with filtering"""
    account = PlaidAccount.query.get_or_404(account_id)
    
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    category = request.args.get('category')
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    
    query = PlaidTransaction.query.filter_by(
        plaid_account_id=account_id
    ).order_by(PlaidTransaction.transaction_date.desc())
    
    if category:
        query = query.filter_by(category=category)
    
    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            query = query.filter(PlaidTransaction.transaction_date >= start_date)
        except ValueError:
            pass
    
    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            query = query.filter(PlaidTransaction.transaction_date <= end_date)
        except ValueError:
            pass
    
    paginated = query.paginate(page=page, per_page=per_page)
    
    transactions = []
    privacy_mode = _is_privacy_mode()
    for idx, trans in enumerate(paginated.items):
        amount_value = float(trans.amount)
        description_value = trans.description
        merchant_value = trans.merchant
        if privacy_mode:
            amount_value = _privacy_bucket(f"acct-tx:{trans.id}:{idx}", 5, 500, 5)
            description_value = 'Transaction'
            merchant_value = None
        transactions.append({
            'id': trans.id,
            'date': trans.transaction_date.isoformat(),
            'amount': amount_value,
            'description': description_value,
            'merchant': merchant_value,
            'category': trans.category,
            'pending': trans.pending,
        })
    
    return jsonify({
        'account': {
            'id': account.id,
            'name': account.account_name,
        },
        'transactions': transactions,
        'total': paginated.total,
        'pages': paginated.pages,
        'current_page': page,
    })


@main_bp.route('/expenses/accounts/<int:account_id>/expenses-by-category', methods=['GET'])
def get_account_expenses_by_category(account_id):
    """Get expense breakdown by category for an account"""
    account = PlaidAccount.query.get_or_404(account_id)
    
    # Get transactions for current month
    today = date.today()
    month_start = date(today.year, today.month, 1)
    
    transactions = PlaidTransaction.query.filter(
        PlaidTransaction.plaid_account_id == account_id,
        PlaidTransaction.transaction_date >= month_start,
        PlaidTransaction.transaction_date <= today
    ).all()
    
    # Build category breakdown
    categories = {}
    for trans in transactions:
        spend_amount = _plaid_spending_amount(trans, include_pending=False)
        if spend_amount <= 0:
            continue
        category = trans.category or 'Uncategorized'
        if category not in categories:
            categories[category] = {
                'count': 0,
                'total': 0,
                'transactions': []
            }
        categories[category]['count'] += 1
        categories[category]['total'] += spend_amount
        categories[category]['transactions'].append({
            'date': trans.transaction_date.isoformat(),
            'amount': spend_amount,
            'description': trans.description,
            'merchant': trans.merchant,
        })
    
    # Sort by total descending
    sorted_categories = sorted(
        categories.items(),
        key=lambda x: x[1]['total'],
        reverse=True
    )
    
    response = {
        'account': {
            'id': account.id,
            'name': account.account_name,
            'month': f"{today.year}-{today.month:02d}",
        },
        'categories': [
            {
                'name': cat,
                'count': data['count'],
                'total': data['total'],
                'average_per_transaction': data['total'] / data['count'],
                'percentage': (data['total'] / sum(c['total'] for c in categories.values()) * 100) if categories else 0,
            }
            for cat, data in sorted_categories
        ],
        'total_spent': sum(c['total'] for c in categories.values()),
    }

    if _is_privacy_mode():
        seed = f"acct-cat:{account.id}:{today.year}:{today.month}"
        fake_total = _privacy_bucket(seed, 1000, 70000, 1000)
        response['categories'] = [
            {'name': 'Household', 'count': 3, 'total': round(fake_total * 0.5, 2), 'average_per_transaction': round((fake_total * 0.5) / 3, 2), 'percentage': 50.0},
            {'name': 'Food', 'count': 2, 'total': round(fake_total * 0.3, 2), 'average_per_transaction': round((fake_total * 0.3) / 2, 2), 'percentage': 30.0},
            {'name': 'Other', 'count': 2, 'total': round(fake_total * 0.2, 2), 'average_per_transaction': round((fake_total * 0.2) / 2, 2), 'percentage': 20.0},
        ]
        response['total_spent'] = fake_total

    return jsonify(response)


@main_bp.route('/expenses/budget/family', methods=['GET', 'POST'])
@_require_auth()
def manage_family_budget():
    """Get or create family-wide budget"""
    if request.method == 'GET':
        today = date.today()
        budget = FamilyBudget.query.filter_by(
            month=today.month,
            year=today.year
        ).first()

        if budget:
            _refresh_budget_category_spent_for_period(int(budget.year), int(budget.month))
            spent_map = _budget_category_spent_map(int(budget.year), int(budget.month))
        else:
            spent_map = {}
        
        if not budget:
            return jsonify({'error': 'No budget for current month'}), 404
        
        categories = []
        total_spent = 0
        for cat in budget.categories:
            cat_spent = float(spent_map.get((cat.category_name or '').strip() or 'Uncategorized', 0.0))
            total_spent += cat_spent
            categories.append({
                'id': cat.id,
                'name': cat.category_name,
                'allocated': float(cat.allocated_amount),
                'spent': cat_spent,
                'remaining': float(cat.allocated_amount) - cat_spent,
                'percentage': (cat_spent / float(cat.allocated_amount) * 100) if cat.allocated_amount > 0 else 0,
            })
        
        return jsonify({
            'id': budget.id,
            'month': budget.month,
            'year': budget.year,
            'total_budget': float(budget.total_amount),
            'total_spent': total_spent,
            'total_remaining': float(budget.total_amount) - total_spent,
            'categories': categories,
        })
    
    # POST - Create new budget
    data = request.get_json() or {}
    today = date.today()
    month = int(data.get('month', today.month))
    year = int(data.get('year', today.year))
    total_amount = float(data.get('total_amount', 0))
    
    if total_amount <= 0:
        return jsonify({'error': 'Total amount must be positive'}), 400
    
    existing = FamilyBudget.query.filter_by(month=month, year=year).first()
    if existing:
        return jsonify({'error': 'Budget already exists for this month'}), 409
    
    budget = FamilyBudget(
        budget_name=f"Family Budget {month}/{year}",
        month=month,
        year=year,
        total_amount=total_amount,
        currency='USD',
        created_by='Administrator',
    )
    db.session.add(budget)
    db.session.commit()
    
    return jsonify({
        'id': budget.id,
        'message': 'Family budget created'
    }), 201


@main_bp.route('/expenses/budget/family/member/<member>', methods=['GET'])
def get_member_spending(member):
    """Get spending summary for a specific family member"""
    today = date.today()
    month_start = date(today.year, today.month, 1)
    month_end = today
    
    # Get member's expenses
    expenses = ExpenseEntry.query.filter(
        ExpenseEntry.payer == member,
        ExpenseEntry.date >= month_start,
        ExpenseEntry.date <= month_end
    ).all()
    
    # Get member's share of shared expenses
    shared_expenses = SharedExpense.query.filter(
        SharedExpense.date >= month_start,
        SharedExpense.date <= month_end
    ).all()
    
    member_shared_total = 0
    for shared in shared_expenses:
        participants = json.loads(shared.participants) if isinstance(shared.participants, str) else shared.participants
        for p in participants:
            if p.get('name') == member:
                member_shared_total += float(p.get('amount', 0))
    
    # Group by category
    category_spending = {}
    for exp in expenses:
        category = exp.category or 'Uncategorized'
        if category not in category_spending:
            category_spending[category] = 0
        category_spending[category] += float(exp.amount or 0)
    
    total_personal = sum(float(e.amount or 0) for e in expenses)
    
    return jsonify({
        'member': member,
        'period': f"{today.year}-{today.month:02d}",
        'personal_spending': {
            'total': total_personal,
            'count': len(expenses),
            'by_category': category_spending,
        },
        'shared_spending': {
            'total': member_shared_total,
        },
        'combined_total': total_personal + member_shared_total,
    })


@main_bp.route('/expenses/budget/recommendations', methods=['GET'])
def get_budget_recommendations():
    """Get budget recommendations based on spending patterns"""
    # Analyze last 3 months of data
    today = date.today()
    three_months_ago = today - timedelta(days=90)
    
    expenses = ExpenseEntry.query.filter(
        ExpenseEntry.date >= three_months_ago,
        ExpenseEntry.date <= today
    ).all()
    
    # Category analysis
    category_totals = {}
    category_counts = {}
    
    for exp in expenses:
        category = exp.category or 'Uncategorized'
        amount = float(exp.amount or 0)
        
        if category not in category_totals:
            category_totals[category] = 0
            category_counts[category] = 0
        
        category_totals[category] += amount
        category_counts[category] += 1
    
    # Calculate monthly averages
    months_analyzed = 3
    recommendations = []
    
    for category, total in category_totals.items():
        monthly_avg = total / months_analyzed
        recommended = monthly_avg * 1.1  # Add 10% buffer
        
        recommendations.append({
            'category': category,
            'monthly_average': round(monthly_avg, 2),
            'recommended_budget': round(recommended, 2),
            'transaction_count': category_counts[category],
            'confidence': 'high' if category_counts[category] >= 5 else 'medium',
        })
    
    # Sort by monthly average descending
    recommendations.sort(key=lambda x: x['monthly_average'], reverse=True)
    
    return jsonify({
        'period': '3 months',
        'total_spent': sum(category_totals.values()),
        'total_recommendations': sum(r['recommended_budget'] for r in recommendations),
        'recommendations': recommendations,
    })

