# Finance Tracker by Shyara — Database Helper Functions
# ─────────────────────────────────────────────────────────────────────────────

import logging

from app.core.database import database
from datetime import datetime, timedelta, timezone
import json

__all__ = ["database"]

logger = logging.getLogger(__name__)
_table_exists_cache: dict[str, bool] = {}


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNT TYPE MAPS
# ══════════════════════════════════════════════════════════════════════════════

ACCOUNT_TYPE_LABELS = {
    "savings":      "Savings A/C",
    "current":      "Current A/C",
    "salary":       "Salary A/C",
    "credit_card":  "Credit Card",
    "debit_card":   "Debit Card",
    "prepaid_card": "Prepaid Card",
    "wallet":       "Wallet",
    "upi":          "UPI",
    "cash":         "Cash in Hand",
}

# Single source of truth for payment method labels.
# bot_logic.py imports this — do NOT redefine it there.
PAYMENT_METHOD_LABELS = {
    "upi":           "UPI",
    "debit_card":    "Debit Card",
    "neft":          "NEFT",
    "imps":          "IMPS",
    "rtgs":          "RTGS",
    "cheque":        "Cheque",
    "auto_debit":    "Auto Debit",
    "bank_transfer": "Bank Transfer",
    "credit_card":   "Credit Card",
    "wallet":        "Wallet",
    "cash":          "Cash",
    "prepaid_card":  "Prepaid Card",
}


# ══════════════════════════════════════════════════════════════════════════════
# USERS
# ══════════════════════════════════════════════════════════════════════════════

async def is_user_registered(phone: str) -> bool:
    row = await database.fetch_one(
        """SELECT id FROM users
           WHERE phone_number = :phone AND is_registered = TRUE""",
        {"phone": phone}
    )
    return row is not None


async def get_user_by_phone(phone: str):
    return await database.fetch_one(
        "SELECT * FROM users WHERE phone_number = :phone",
        {"phone": phone}
    )


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNTS
# ══════════════════════════════════════════════════════════════════════════════

async def get_user_accounts(user_id: int):
    return await database.fetch_all(
        """SELECT * FROM accounts
           WHERE user_id = :uid AND is_active = TRUE
           ORDER BY is_default DESC, account_category, nickname""",
        {"uid": user_id}
    )


async def get_account_by_id(account_id: int):
    return await database.fetch_one(
        "SELECT * FROM accounts WHERE id = :id",
        {"id": account_id}
    )


async def get_accounts_for_picker(user_id: int):
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
            "title":       f"{star}{acc['nickname']}"[:24],
            "description": desc[:72]
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

async def record_transaction_with_account_update(
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
    payment_method: str = None,
):
    async with database.connection() as connection:
        async with connection.transaction():
            account = None
            if account_id:
                account = await connection.fetch_one(
                    """SELECT id, user_id, account_type, balance, outstanding
                       FROM accounts
                       WHERE id = :id
                       FOR UPDATE""",
                    {"id": account_id}
                )
                if not account:
                    raise ValueError("Selected account was not found.")
                if account["user_id"] != user_id:
                    raise ValueError("Selected account does not belong to this user.")

            transaction_row = await connection.fetch_one(
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

            if account:
                if type_ == "income":
                    await connection.execute(
                        """UPDATE accounts
                           SET balance = COALESCE(balance, 0) + :amt
                           WHERE id = :id""",
                        {"amt": amount, "id": account_id}
                    )
                elif type_ == "expense":
                    if account["account_type"] == "credit_card":
                        await connection.execute(
                            """UPDATE accounts
                               SET outstanding = COALESCE(outstanding, 0) + :amt
                               WHERE id = :id""",
                            {"amt": amount, "id": account_id}
                        )
                    else:
                        await connection.execute(
                            """UPDATE accounts
                               SET balance = COALESCE(balance, 0) - :amt
                               WHERE id = :id""",
                            {"amt": amount, "id": account_id}
                        )

            return transaction_row


async def get_monthly_expense_total(user_id: int, month_year: str) -> float:
    val = await database.fetch_val(
        """SELECT COALESCE(SUM(amount), 0) FROM transactions
           WHERE user_id = :uid
             AND type = 'expense'
             AND TO_CHAR(transaction_date, 'YYYY-MM') = :my""",
        {"uid": user_id, "my": month_year}
    )
    return float(val or 0)


# ══════════════════════════════════════════════════════════════════════════════
# SESSIONS
# ══════════════════════════════════════════════════════════════════════════════

async def get_session(phone: str):
    """
    Returns active session dict or None.

    Returns None when:
    - No session row exists for this phone
    - Session is expired (updated_at > 15 minutes ago)

    Fix applied: uses datetime.now(timezone.utc) instead of
    datetime.utcnow() to ensure correct timezone-aware comparison
    against the TIMESTAMPTZ column returned by Postgres.
    """
    row = await database.fetch_one(
        "SELECT * FROM whatsapp_bot_sessions WHERE phone_number = :phone",
        {"phone": phone}
    )
    if not row:
        return None

    if row["session_status"] != "idle":
        # Both sides are now timezone-aware — correct comparison
        age = datetime.now(timezone.utc) - row["updated_at"]
        if age > timedelta(minutes=15):
            await reset_session(phone)
            return None

    session = dict(row)
    if isinstance(session["parsed_result"], str):
        try:
            session["parsed_result"] = json.loads(session["parsed_result"])
        except (json.JSONDecodeError, TypeError):
            session["parsed_result"] = {}

    return session


async def set_session(phone: str, status: str, parsed_result: dict = None):
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
    await set_session(phone, "idle", {})


# ══════════════════════════════════════════════════════════════════════════════
# SUBSCRIPTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def get_subscriptions(user_id: int):
    return await database.fetch_all(
<<<<<<< HEAD
        """SELECT s.id, s.merchant AS service_name, s.amount, s.billing_day, s.category, s.status, s.account_id, a.nickname AS account_name
=======
        """SELECT s.*, a.nickname AS account_name
>>>>>>> 71b0709e61386b0bb172347d5968c3caa9157af9
           FROM recurring_payments s
           LEFT JOIN accounts a ON s.account_id = a.id
           WHERE s.user_id = :uid AND s.status = 'active'
           ORDER BY s.billing_day""",
        {"uid": user_id}
    )


async def get_goals(user_id: int):
    return await database.fetch_all(
        """SELECT *
           FROM goals
           WHERE user_id = :uid
           ORDER BY
             CASE priority
               WHEN 'high' THEN 1
               WHEN 'medium' THEN 2
               WHEN 'low' THEN 3
               ELSE 4
             END,
             created_at ASC""",
        {"uid": user_id}
    )


async def get_goal_by_id(goal_id: int):
    return await database.fetch_one(
        "SELECT * FROM goals WHERE id = :id",
        {"id": goal_id}
    )


async def create_goal(
    user_id: int,
    name: str,
    target_amount: float,
    current_amount: float = 0,
    icon: str = "🎯",
    theme: str = "emerald",
    priority: str = "medium"
):
    return await database.fetch_one(
        """INSERT INTO goals
           (user_id, name, icon, target_amount, current_amount, theme, priority)
           VALUES (:uid, :name, :icon, :target, :current, :theme, :priority)
           RETURNING id""",
        {
            "uid": user_id,
            "name": name,
            "icon": icon,
            "target": target_amount,
            "current": current_amount,
            "theme": theme,
            "priority": priority,
        }
    )


async def update_goal_current_amount(goal_id: int, new_amount: float):
    await database.execute(
        "UPDATE goals SET current_amount = :amount WHERE id = :id",
        {"amount": new_amount, "id": goal_id}
    )


# ══════════════════════════════════════════════════════════════════════════════
# BUDGETS
# ══════════════════════════════════════════════════════════════════════════════

async def get_total_budget(user_id: int, month_year: str) -> float:
    if await _table_exists("budgets"):
        val = await database.fetch_val(
            """SELECT COALESCE(SUM(monthly_limit), 0)
               FROM budgets
               WHERE user_id = :uid AND month_year = :my""",
            {"uid": user_id, "my": month_year}
        )
        return float(val or 0)

    if await _table_exists("budget_configurations"):
        val = await database.fetch_val(
            """SELECT COALESCE(SUM(monthly_limit), 0)
               FROM budget_configurations
               WHERE user_id = :uid""",
            {"uid": user_id}
        )
        return float(val or 0)

    return 0.0


async def _table_exists(table_name: str) -> bool:
    cached = _table_exists_cache.get(table_name)
    if cached is not None:
        return cached

    exists = await database.fetch_val(
        """SELECT EXISTS (
               SELECT 1
               FROM information_schema.tables
               WHERE table_schema = 'public'
                 AND table_name = :table_name
           )""",
        {"table_name": table_name}
    )
    _table_exists_cache[table_name] = bool(exists)
    return bool(exists)


# ══════════════════════════════════════════════════════════════════════════════
# ANALYTICS CACHE
# ══════════════════════════════════════════════════════════════════════════════

async def refresh_analytics_cache(user_id: int):
    month_year = datetime.now(timezone.utc).strftime("%Y-%m")

    balance_row = await database.fetch_one(
        """SELECT COALESCE(SUM(balance), 0) AS total
           FROM accounts
           WHERE user_id = :uid
             AND is_active = TRUE
             AND account_type != 'credit_card'""",
        {"uid": user_id}
    )
    balance = float(balance_row["total"] or 0) if balance_row else 0.0

    spent = await get_monthly_expense_total(user_id, month_year)
    try:
        budget_total = await get_total_budget(user_id, month_year)
    except Exception as e:
        logger.warning(f"Budget total refresh failed for user {user_id}: {e}")
        budget_total = 0
    pct = int((spent / budget_total * 100)) if budget_total > 0 else 0

    next_sub = await database.fetch_one(
        """SELECT s.merchant AS service_name, s.amount, s.billing_day,
                  COALESCE(a.nickname, 'No Account') AS acc_name
           FROM recurring_payments s
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
