# Finance Tracker by Shyara — Database Helper Functions
# ─────────────────────────────────────────────────────────────────────────────

from database import database          # exported so bot_logic.py can import it
from datetime import datetime, timedelta
import json

# Make database importable directly from this module
# Usage in bot_logic.py:  from db_helpers import database
__all__ = ["database"]


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNT TYPE MAPS
# ══════════════════════════════════════════════════════════════════════════════

ACCOUNT_TYPE_LABELS = {
    # Bank
    "savings":      "Savings A/C",
    "current":      "Current A/C",
    "salary":       "Salary A/C",
    # Card
    "credit_card":  "Credit Card",
    "debit_card":   "Debit Card",
    "prepaid_card": "Prepaid Card",
    # Digital
    "wallet":       "Wallet",
    "upi":          "UPI",
    # Cash
    "cash":         "Cash in Hand",
}

CATEGORY_EMOJI = {
    "bank":    "🏦",
    "card":    "💳",
    "digital": "📱",
    "cash":    "💵",
}

# Payment methods — shown in the "How did you pay?" step
PAYMENT_METHOD_LABELS = {
    "upi":          "📱 UPI",
    "debit_card":   "💳 Debit Card",
    "credit_card":  "💳 Credit Card",
    "neft":         "🏛️ NEFT",
    "imps":         "🏛️ IMPS",
    "rtgs":         "🏛️ RTGS",
    "cheque":       "📄 Cheque",
    "wallet":       "👛 Wallet",
    "cash":         "💵 Cash",
    "auto_debit":   "🔄 Auto Debit",
    "bank_transfer":"🏦 Bank Transfer",
}

# ══════════════════════════════════════════════════════════════════════════════
# USERS
# ══════════════════════════════════════════════════════════════════════════════

async def is_user_registered(phone: str) -> bool:
    """
    Returns True only if phone exists AND is_registered = TRUE.
    This is the gate check in main.py before processing any message.
    """
    row = await database.fetch_one(
        """SELECT id FROM users
           WHERE phone_number = :phone AND is_registered = TRUE""",
        {"phone": phone}
    )
    return row is not None


async def get_user_by_phone(phone: str):
    """Returns full user row or None."""
    return await database.fetch_one(
        "SELECT * FROM users WHERE phone_number = :phone",
        {"phone": phone}
    )


async def get_user_by_id(user_id: int):
    """Returns full user row by ID or None."""
    return await database.fetch_one(
        "SELECT * FROM users WHERE id = :uid",
        {"uid": user_id}
    )


async def create_user(phone: str):
    """
    Creates a bare user row on first contact.
    is_registered stays FALSE until web dashboard completes signup.
    """
    return await database.fetch_one(
        """INSERT INTO users (phone_number)
           VALUES (:phone)
           ON CONFLICT (phone_number) DO NOTHING
           RETURNING *""",
        {"phone": phone}
    )


async def update_user_name(phone: str, name: str):
    """Update user name and mark as registered."""
    await database.execute(
        """UPDATE users
           SET name = :name, is_registered = TRUE
           WHERE phone_number = :phone""",
        {"name": name, "phone": phone}
    )


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNTS
# ══════════════════════════════════════════════════════════════════════════════

async def get_user_accounts(user_id: int):
    """
    Returns all active accounts for a user.
    Default account comes first, then sorted by category and nickname.
    """
    return await database.fetch_all(
        """SELECT * FROM accounts
           WHERE user_id = :uid AND is_active = TRUE
           ORDER BY is_default DESC, account_category, nickname""",
        {"uid": user_id}
    )


async def get_account_by_id(account_id: int):
    """Returns a single account row by ID."""
    return await database.fetch_one(
        "SELECT * FROM accounts WHERE id = :id",
        {"id": account_id}
    )


async def get_default_account(user_id: int):
    """Returns the user's default account or None."""
    return await database.fetch_one(
        "SELECT * FROM accounts WHERE user_id = :uid AND is_default = TRUE",
        {"uid": user_id}
    )


async def create_account(
    user_id: int,
    nickname: str,
    bank_name: str,
    account_category: str,  # 'bank' | 'card' | 'digital' | 'cash'
    account_type: str,      # 'savings' | 'credit_card' | 'wallet' etc.
    is_default: bool = False,
    credit_limit: float = None
):
    """Insert a new account row."""
    await database.execute(
        """INSERT INTO accounts
           (user_id, nickname, bank_name, account_category,
            account_type, is_default, credit_limit)
           VALUES (:uid, :nick, :bank, :cat, :atype, :is_def, :climit)""",
        {
            "uid":    user_id,
            "nick":   nickname,
            "bank":   bank_name,
            "cat":    account_category,
            "atype":  account_type,
            "is_def": is_default,
            "climit": credit_limit,
        }
    )


async def update_account_balance(account_id: int, new_balance: float):
    """Update balance for bank/digital/cash accounts."""
    await database.execute(
        "UPDATE accounts SET balance = :bal WHERE id = :id",
        {"bal": new_balance, "id": account_id}
    )


async def update_account_outstanding(account_id: int, new_outstanding: float):
    """Update outstanding amount for credit card accounts."""
    await database.execute(
        "UPDATE accounts SET outstanding = :out WHERE id = :id",
        {"out": new_outstanding, "id": account_id}
    )


async def get_accounts_for_picker(user_id: int):
    """
    Returns accounts grouped into WhatsApp list message sections.
    Used in any flow step that asks 'which account?'

    Output format:
    [
        {
            "title": "🏦 Bank Accounts",
            "rows": [
                {"id": "acc_1", "title": "⭐ HDFC Salary", "description": "Savings A/C · HDFC"}
            ]
        },
        ...
    ]
    """
    rows = await database.fetch_all(
        """SELECT * FROM accounts
           WHERE user_id = :uid AND is_active = TRUE
           ORDER BY is_default DESC, account_category, nickname""",
        {"uid": user_id}
    )

    if not rows:
        return []

    sections = {}
    for acc in rows:
        cat = acc["account_category"]
        if cat not in sections:
            sections[cat] = []

        star     = "⭐ " if acc["is_default"] else ""
        type_lbl = ACCOUNT_TYPE_LABELS.get(acc["account_type"], acc["account_type"])
        desc     = type_lbl
        if acc["bank_name"]:
            desc += f" · {acc['bank_name']}"
        if acc["account_type"] == "credit_card" and acc["outstanding"]:
            desc += f" · ₹{float(acc['outstanding']):,.0f} due"

        sections[cat].append({
            "id":          f"acc_{acc['id']}",
            "title":       f"{star}{acc['nickname']}"[:24],   # WhatsApp max 24 chars
            "description": desc[:72]                           # WhatsApp max 72 chars
        })

    category_order  = ["bank", "card", "digital", "cash"]
    category_titles = {
        "bank":    "🏦 Bank Accounts",
        "card":    "💳 Cards",
        "digital": "📱 Digital Payments",
        "cash":    "💵 Cash",
    }

    return [
        {"title": category_titles[cat], "rows": sections[cat]}
        for cat in category_order
        if cat in sections
    ]


# ══════════════════════════════════════════════════════════════════════════════
# TRANSACTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def insert_transaction(
    user_id: int,
    account_id: int,
    amount: float,
    type_: str,
    category: str = None,
    sub_category: str = None,
    merchant: str = None,
    is_essential: bool = True,
    to_account_id: int = None,
    subscription_id: int = None,
    payment_method: str = None       # ← new parameter
):
    return await database.fetch_one(
        """INSERT INTO transactions
           (user_id, account_id, amount, type, category,
            sub_category, merchant, is_essential,
            to_account_id, subscription_id, payment_method)
           VALUES
           (:uid, :aid, :amt, :type, :cat,
            :subcat, :merch, :ess,
            :to_aid, :sub_id, :pm)
           RETURNING id""",
        {
            "uid":    user_id,
            "aid":    account_id,
            "amt":    amount,
            "type":   type_,
            "cat":    category,
            "subcat": sub_category,
            "merch":  merchant,
            "ess":    is_essential,
            "to_aid": to_account_id,
            "sub_id": subscription_id,
            "pm":     payment_method,
        }
    )


async def get_recent_transactions(user_id: int, limit: int = 10):
    """Returns most recent transactions with account name joined."""
    return await database.fetch_all(
        """SELECT t.*, a.nickname AS account_name
           FROM transactions t
           LEFT JOIN accounts a ON t.account_id = a.id
           WHERE t.user_id = :uid
           ORDER BY t.transaction_date DESC
           LIMIT :lim""",
        {"uid": user_id, "lim": limit}
    )


async def get_last_transaction(user_id: int):
    """Returns the single most recent transaction — used for 'correct last entry'."""
    return await database.fetch_one(
        """SELECT t.*, a.nickname AS account_name
           FROM transactions t
           LEFT JOIN accounts a ON t.account_id = a.id
           WHERE t.user_id = :uid
           ORDER BY t.transaction_date DESC
           LIMIT 1""",
        {"uid": user_id}
    )


async def delete_transaction(transaction_id: int):
    """Hard delete a transaction by ID."""
    await database.execute(
        "DELETE FROM transactions WHERE id = :id",
        {"id": transaction_id}
    )


async def get_monthly_expense_total(user_id: int, month_year: str) -> float:
    """Returns total expense amount for a given month (format: '2026-03')."""
    val = await database.fetch_val(
        """SELECT COALESCE(SUM(amount), 0) FROM transactions
           WHERE user_id = :uid
             AND type = 'expense'
             AND TO_CHAR(transaction_date, 'YYYY-MM') = :my""",
        {"uid": user_id, "my": month_year}
    )
    return float(val or 0)


async def get_monthly_income_total(user_id: int, month_year: str) -> float:
    """Returns total income amount for a given month (format: '2026-03')."""
    val = await database.fetch_val(
        """SELECT COALESCE(SUM(amount), 0) FROM transactions
           WHERE user_id = :uid
             AND type = 'income'
             AND TO_CHAR(transaction_date, 'YYYY-MM') = :my""",
        {"uid": user_id, "my": month_year}
    )
    return float(val or 0)


async def get_category_totals(user_id: int, month_year: str):
    """Returns expense totals grouped by category for a given month."""
    return await database.fetch_all(
        """SELECT category,
                  COUNT(*) AS txn_count,
                  SUM(amount) AS total
           FROM transactions
           WHERE user_id = :uid
             AND type = 'expense'
             AND TO_CHAR(transaction_date, 'YYYY-MM') = :my
             AND category IS NOT NULL
           GROUP BY category
           ORDER BY total DESC""",
        {"uid": user_id, "my": month_year}
    )


# ══════════════════════════════════════════════════════════════════════════════
# SESSIONS
# ══════════════════════════════════════════════════════════════════════════════

async def get_session(phone: str):
    """
    Returns active session dict or None.

    Returns None when:
    - No session row exists for this phone
    - Session exists but is expired (updated_at > 15 minutes ago)

    The parsed_result JSONB field is always returned as a Python dict,
    never as a raw string.
    """
    row = await database.fetch_one(
        "SELECT * FROM whatsapp_bot_sessions WHERE phone_number = :phone",
        {"phone": phone}
    )
    if not row:
        return None

    # 15-minute expiry — only applies to non-idle sessions
    if row["session_status"] != "idle":
        age = datetime.utcnow() - row["updated_at"].replace(tzinfo=None)
        if age > timedelta(minutes=15):
            await reset_session(phone)
            return None

    # Convert immutable DB row → mutable dict
    # Parse parsed_result from JSON string → Python dict if needed
    session = dict(row)
    if isinstance(session["parsed_result"], str):
        try:
            session["parsed_result"] = json.loads(session["parsed_result"])
        except (json.JSONDecodeError, TypeError):
            session["parsed_result"] = {}

    return session


async def set_session(phone: str, status: str, parsed_result: dict = None):
    """
    Create or update a session row (upsert).

    Note: We pass parsed_result as a plain JSON string — NOT with ::jsonb cast.
    The databases library doesn't support PostgreSQL type casts in query strings.
    PostgreSQL automatically handles the string → jsonb conversion.
    """
    data = json.dumps(parsed_result or {})
    await database.execute(
        """INSERT INTO whatsapp_bot_sessions
               (phone_number, session_status, parsed_result, updated_at)
           VALUES (:phone, :status, :data, NOW())
           ON CONFLICT (phone_number) DO UPDATE
               SET session_status = :status,
                   parsed_result  = :data,
                   updated_at     = NOW()""",
        {"phone": phone, "status": status, "data": data}
    )


async def reset_session(phone: str):
    """Reset session to idle and clear all collected data."""
    await set_session(phone, "idle", {})


# ══════════════════════════════════════════════════════════════════════════════
# SUBSCRIPTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def get_subscriptions(user_id: int):
    """Returns all active subscriptions with account name joined."""
    return await database.fetch_all(
        """SELECT s.*, a.nickname AS account_name
           FROM subscriptions s
           LEFT JOIN accounts a ON s.account_id = a.id
           WHERE s.user_id = :uid AND s.status = 'active'
           ORDER BY s.billing_day""",
        {"uid": user_id}
    )


async def find_subscription_by_merchant(user_id: int, merchant: str):
    """
    Case-insensitive lookup of subscriptions by merchant name.
    Used during expense flow to auto-detect subscription charges
    and avoid double-counting.
    """
    return await database.fetch_one(
        """SELECT * FROM subscriptions
           WHERE user_id = :uid
             AND status = 'active'
             AND LOWER(service_name) = LOWER(:merchant)""",
        {"uid": user_id, "merchant": merchant}
    )


async def get_subscription_monthly_total(user_id: int) -> float:
    """Returns total monthly subscription cost for active subscriptions."""
    val = await database.fetch_val(
        """SELECT COALESCE(SUM(amount), 0) FROM subscriptions
           WHERE user_id = :uid AND status = 'active'""",
        {"uid": user_id}
    )
    return float(val or 0)


# ══════════════════════════════════════════════════════════════════════════════
# BUDGETS
# ══════════════════════════════════════════════════════════════════════════════

async def get_budget_for_category(user_id: int, category: str, month_year: str):
    """Returns budget row for a specific category and month or None."""
    return await database.fetch_one(
        """SELECT * FROM budgets
           WHERE user_id = :uid
             AND category = :cat
             AND month_year = :my""",
        {"uid": user_id, "cat": category, "my": month_year}
    )


async def get_all_budgets(user_id: int, month_year: str):
    """Returns all budget rows for a given month."""
    return await database.fetch_all(
        """SELECT * FROM budgets
           WHERE user_id = :uid AND month_year = :my
           ORDER BY monthly_limit DESC""",
        {"uid": user_id, "my": month_year}
    )


async def get_total_budget(user_id: int, month_year: str) -> float:
    """Returns sum of all category budgets for the month."""
    val = await database.fetch_val(
        """SELECT COALESCE(SUM(monthly_limit), 0)
           FROM budgets
           WHERE user_id = :uid AND month_year = :my""",
        {"uid": user_id, "my": month_year}
    )
    return float(val or 0)


# ══════════════════════════════════════════════════════════════════════════════
# ANALYTICS CACHE
# ══════════════════════════════════════════════════════════════════════════════

async def get_analytics_cache(user_id: int):
    """
    Returns pre-computed greeting snapshot from cache.
    Call refresh_analytics_cache() after every transaction to keep this fresh.
    """
    return await database.fetch_one(
        "SELECT * FROM analytics_cache WHERE user_id = :uid",
        {"uid": user_id}
    )


async def refresh_analytics_cache(user_id: int):
    """
    Recomputes and upserts the analytics cache for a user.
    Called after every transaction, income, or subscription change
    so the greeting snapshot is always up to date.

    Computes:
    - default_balance:  balance of the user's default account
    - budget_pct_used:  expenses this month as % of total budget
    - next_bill_text:   nearest upcoming subscription in plain text
    """
    month_year = datetime.now().strftime("%Y-%m")

    # ── Total balance across all non-credit-card accounts ────────────────────
    balance_row = await database.fetch_one(
        """SELECT COALESCE(SUM(balance), 0) AS total
           FROM accounts
           WHERE user_id = :uid
             AND is_active = TRUE
             AND account_type != 'credit_card'""",
        {"uid": user_id}
    )
    balance = float(balance_row["total"] or 0) if balance_row else 0.0

    # ── Budget % used this month ──────────────────────────────────────────────
    spent        = await get_monthly_expense_total(user_id, month_year)
    budget_total = await get_total_budget(user_id, month_year)
    pct          = int((spent / budget_total * 100)) if budget_total > 0 else 0

    # ── Next upcoming subscription bill ──────────────────────────────────────
    next_sub = await database.fetch_one(
        """SELECT s.service_name, s.amount, s.billing_day,
                  COALESCE(a.nickname, 'No Account') AS acc_name
           FROM subscriptions s
           LEFT JOIN accounts a ON s.account_id = a.id
           WHERE s.user_id = :uid AND s.status = 'active'
           ORDER BY s.billing_day ASC
           LIMIT 1""",
        {"uid": user_id}
    )
    next_bill_text = ""
    if next_sub:
        next_bill_text = (
            f"{next_sub['service_name']} "
            f"₹{float(next_sub['amount']):,.0f} "
            f"on {next_sub['billing_day']} "
            f"({next_sub['acc_name']})"
        )

    # ── Upsert cache row ──────────────────────────────────────────────────────
    await database.execute(
        """INSERT INTO analytics_cache
               (user_id, default_balance, budget_pct_used, next_bill_text, refreshed_at)
           VALUES (:uid, :bal, :pct, :bill, NOW())
           ON CONFLICT (user_id) DO UPDATE
               SET default_balance  = :bal,
                   budget_pct_used  = :pct,
                   next_bill_text   = :bill,
                   refreshed_at     = NOW()""",
        {
            "uid":  user_id,
            "bal":  balance,
            "pct":  pct,
            "bill": next_bill_text,
        }
    )