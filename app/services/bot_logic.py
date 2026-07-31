from app.integrations.whatsapp_client import (
    send_text_message,
    send_interactive_buttons,
    send_list_message,
)
from app.repositories.db_helpers import (
    get_user_by_phone,
    get_user_accounts,
    get_accounts_for_picker,
    set_session,
    reset_session,
    refresh_analytics_cache,
    record_transaction_with_account_update,
    database,
    # Single source of truth — no longer duplicated in this file
    PAYMENT_METHOD_LABELS,
)
from app.core.config import DASHBOARD_URL
from datetime import datetime, date, timedelta
import asyncio


# ── Ordinal helper ─────────────────────────────────────────────────────────────
def ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ── Due-date label helper ──────────────────────────────────────────────────────
def due_label(billing_day: int, today: date) -> str:
    import calendar
    try:
        due = today.replace(day=billing_day)
    except ValueError:
        last = calendar.monthrange(today.year, today.month)[1]
        due  = today.replace(day=last)

    delta = (due - today).days
    if delta == 0:
        return "⚠️ Due TODAY"
    elif delta == 1:
        return "⏰ Due Tomorrow"
    elif delta > 0:
        return f"📅 Due in {delta}d ({ordinal(billing_day)})"
    else:
        return f"🔴 Overdue {abs(delta)}d ({ordinal(billing_day)})"


def _with_stop_hint(text: str) -> str:
    if "Type *stop* to cancel." in text:
        return text
    return f"{text}\n\nType *stop* to cancel."


# ── Session helpers ────────────────────────────────────────────────────────────

def _get_parsed(session) -> dict:
    """
    Safely extracts parsed_result from a session dict.
    Returns {} if session is None, expired, or has no parsed_result.
    Eliminates the repeated `dict(session["parsed_result"]) if session else {}`
    pattern that caused crashes when sessions expired mid-flow.
    """
    if not session:
        return {}
    result = session.get("parsed_result", {})
    return dict(result) if result else {}


def _session_has(session, *fields) -> bool:
    """
    Returns True only if session exists AND all specified fields are
    present and non-falsy in parsed_result.
    Used to guard confirm handlers against expired sessions.
    """
    parsed = _get_parsed(session)
    return all(parsed.get(f) for f in fields)


def _goal_progress(goal) -> tuple[float, int]:
    current = float(goal["current_amount"] or 0)
    target = float(goal["target_amount"] or 0)
    pct = int((current / target) * 100) if target > 0 else 0
    return current, min(pct, 999)


def _goal_icon(goal) -> str:
    return goal.get("icon") or "🎯"


async def _expired_session_reply(to: str):
    """Standard reply when a confirm button is tapped with an expired session."""
    await send_text_message(
        to=to,
        text="⏱️ Your session expired. Please start again."
    )
    await send_main_menu(to)


async def send_reports_menu(to: str):
    await send_list_message(
        to=to,
        body_text="📊 *Reports & Insights*\n\nChoose what you want to see.",
        button_label="Open Reports",
        sections=[{
            "title": "Reports",
            "rows": [
                {"id": "sum_thismonth", "title": "📅 This Month", "description": "Income, expenses, savings"},
                {"id": "sum_balances", "title": "💳 Accounts", "description": "Balances and card dues"},
                {"id": "sum_recent", "title": "🕐 Recent 10", "description": "Latest transactions"},
                {"id": "sum_category", "title": "📂 Categories", "description": "Spend breakdown"},
            ]
        }]
    )


async def send_subscriptions_menu(to: str):
    await send_list_message(
        to=to,
        body_text="📺 *Subscriptions*\n\nManage your recurring bills.",
        button_label="Open Subs",
        sections=[{
            "title": "Subscriptions",
            "rows": [
                {"id": "sub_view", "title": "📋 View All", "description": "See active subscriptions"},
                {"id": "sub_mark_paid", "title": "✅ Mark Paid", "description": "Log this month’s payment"},
                {"id": "sub_add", "title": "➕ Add New", "description": "Track a new subscription"},
            ]
        }]
    )


async def send_goals_menu(to: str):
    await send_list_message(
        to=to,
        body_text="🎯 *Goals*\n\nTrack what you’re saving toward.",
        button_label="Open Goals",
        sections=[{
            "title": "Goals",
            "rows": [
                {"id": "goal_view", "title": "📋 View Goals", "description": "See progress and targets"},
                {"id": "goal_add", "title": "➕ Add Goal", "description": "Create a new savings goal"},
                {"id": "goal_add_funds", "title": "💰 Add Savings", "description": "Update an existing goal"},
            ]
        }]
    )


async def send_help_message(to: str):
    await send_text_message(
        to=to,
        text=(
            "🤝 *Quick Help*\n\n"
            "You can use the menu or type simple commands:\n"
            "• *expense* to log an expense\n"
            "• *income* to log income\n"
            "• *subscriptions* to manage recurring bills\n"
            "• *goals* to manage savings goals\n"
            "• *reports* to view metrics\n"
            "• *menu* or *home* to return anytime\n"
            "• *cancel* to stop the current flow"
        )
    )


async def get_due_subscriptions(user_id: int) -> list:
    today      = date.today()
    month_str  = today.strftime("%Y-%m")
    cutoff_day = today.day + 5

    subs = await database.fetch_all(
        """SELECT s.id, s.merchant AS service_name, s.amount, 
                  COALESCE(EXTRACT(DAY FROM s.next_expected::date)::integer, 1) AS billing_day,
                  COALESCE(a.nickname, 'No Account') AS account_name
           FROM recurring_payments s
           LEFT JOIN accounts a ON s.account_id = a.id
           WHERE s.user_id = :uid AND s.status IN ('active', 'confirmed')
           ORDER BY billing_day""",
        {"uid": user_id}
    )

    paid_rows = await database.fetch_all(
        """SELECT DISTINCT subscription_id FROM transactions
           WHERE user_id = :uid
             AND subscription_id IS NOT NULL
             AND type = 'expense'
             AND TO_CHAR(transaction_date, 'YYYY-MM') = :m""",
        {"uid": user_id, "m": month_str}
    )
    paid_ids = {r["subscription_id"] for r in paid_rows}

    result = []
    for s in subs:
        bd = s["billing_day"]
        if bd <= cutoff_day and s["id"] not in paid_ids:
            result.append(dict(s))

    return result


# ══════════════════════════════════════════════════════════════════════════════
# NOT REGISTERED
# ══════════════════════════════════════════════════════════════════════════════

async def send_not_registered(to: str):
    await send_text_message(
        to=to,
        text=(
            "👋 Welcome to Finance Tracker by Shyara!\n\n"
            "It looks like you haven't registered yet.\n"
            "Please sign up on our website to get started:\n\n"
            f"🌐 {DASHBOARD_URL}\n\n"
            "Once registered, come back and say *Hi* to get started!"
        )
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN MENU
# ══════════════════════════════════════════════════════════════════════════════

async def send_main_menu(to: str):
    from app.services.chatbot_flow import send_main_menu as flow_send_main_menu

    await flow_send_main_menu(to)
    return

    user_id = user["id"]
    cache = await get_analytics_cache(user_id)
    name  = user["name"] or "there"
    goal_count = int(await database.fetch_val(
        "SELECT COUNT(*) FROM goals WHERE user_id = :uid",
        {"uid": user_id}
    ) or 0)

    if cache:
        balance  = float(cache["default_balance"] or 0)
        snapshot = (
            f"💰 All Accounts Balance: ₹{balance:,.0f}\n"
            f"📊 Budget: {cache['budget_pct_used'] or 0}% used"
        )
    else:
        snapshot = "📊 No data yet — log your first transaction!"

    today    = date.today()
    due_subs = await get_due_subscriptions(user_id)

    if due_subs:
        alert_lines = ["\n📺 *Subscriptions due / coming up:*"]
        for s in due_subs[:5]:
            label = due_label(s["billing_day"], today)
            alert_lines.append(
                f"  • {s['service_name']}  ₹{float(s['amount']):,.0f}  {label}"
            )
        alert_text = "\n".join(alert_lines)
    else:
        alert_text = ""

    await send_interactive_buttons(
        to=to,
        body_text=(
            f"👋 Welcome back, {name}!\n\n"
            f"{snapshot}"
            f"{alert_text}\n\n"
            f"What would you like to do?"
        ),
        buttons=[
            {"id": "menu_transactions",  "title": "💸 Transactions"},
            {"id": "menu_subscriptions", "title": "📺 Subscriptions"},
            {"id": "menu_reports",       "title": "📊 Reports"},
        ]
    )
    await reset_session(to)


# ══════════════════════════════════════════════════════════════════════════════
# BUTTON CLICK HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def handle_button_click(to: str, button_id: str, session):

    # ── TOP LEVEL ─────────────────────────────────────────────────────────────

    if button_id == "menu_transactions":
        await send_interactive_buttons(
            to=to,
            body_text="💸 *Transactions*\n\nWhat would you like to do?",
            buttons=[
                {"id": "menu_expense", "title": "💸 Log Expense"},
                {"id": "menu_income",  "title": "💵 Log Income"},
                {"id": "nav_home",     "title": "🏠 Main Menu"},
            ]
        )

    elif button_id == "menu_subscriptions":
        await send_interactive_buttons(
            to=to,
            body_text="📺 *Subscriptions*\n\nWhat would you like to do?",
            buttons=[
                {"id": "sub_view",      "title": "📋 View All"},
                {"id": "sub_mark_paid", "title": "✅ Mark as Paid"},
                {"id": "sub_add",       "title": "➕ Add New"},
            ]
        )

    elif button_id == "menu_reports":
        await send_interactive_buttons(
            to=to,
            body_text="📊 *Reports*\n\nChoose a report:",
            buttons=[
                {"id": "sum_thismonth", "title": "📅 This Month"},
                {"id": "sum_balances",  "title": "💳 Balances"},
                {"id": "rep_more",      "title": "📋 More Reports"},
            ]
        )

    elif button_id == "rep_more":
        await send_interactive_buttons(
            to=to,
            body_text="📋 *More Reports*",
            buttons=[
                {"id": "sum_recent",   "title": "🕐 Recent 10"},
                {"id": "sum_category", "title": "📂 By Category"},
                {"id": "nav_home",     "title": "🏠 Main Menu"},
            ]
        )

    # ── Transactions ──────────────────────────────────────────────────────────

    elif button_id == "menu_expense":
        await set_session(to, "expense_amount", {})
        await send_text_message(
            to=to,
            text=_with_stop_hint(
                "💸 *Log Expense*\n\n"
                "Enter the expense amount:\n\n"
                "E.g. *350* or *1200*"
            )
        )

    elif button_id == "menu_income":
        await set_session(to, "income_type", {})
        await send_list_message(
            to=to,
            body_text="💵 What type of income?",
            button_label="Select Type",
            sections=[{
                "title": "Income Type",
                "rows": [
                    {"id": "inc_salary",     "title": "💼 Salary"},
                    {"id": "inc_freelance",  "title": "💻 Freelance"},
                    {"id": "inc_business",   "title": "🏢 Business"},
                    {"id": "inc_rental",     "title": "🏠 Rental"},
                    {"id": "inc_investment", "title": "📈 Investment"},
                    {"id": "inc_other",      "title": "🎁 Other"},
                ]
            }]
        )

    # ── Reports ───────────────────────────────────────────────────────────────

    elif button_id == "sum_balances":
        await reset_session(to)
        await send_balance_summary(to)

    elif button_id == "sum_thismonth":
        await reset_session(to)
        await send_monthly_summary(to)

    elif button_id == "sum_recent":
        await reset_session(to)
        await send_recent_transactions(to)

    elif button_id == "sum_category":
        await reset_session(to)
        await send_category_summary(to)

    # ── Subscriptions ─────────────────────────────────────────────────────────

    elif button_id == "sub_view":
        await reset_session(to)
        await send_subscriptions_list(to)

    elif button_id == "sub_mark_paid":
        await send_subscription_picker(to)

    elif button_id == "sub_pay_confirm":
        # Guard: session must have sub_id, amount, account_id
        if not _session_has(session, "sub_id", "amount", "account_id"):
            await _expired_session_reply(to)
            return
        parsed = _get_parsed(session)
        await confirm_subscription_payment(to, parsed)

    elif button_id == "sub_pay_cancel":
        await reset_session(to)
        await send_text_message(to=to, text="❌ Payment not recorded.")
        await send_subscriptions_list(to)

    elif button_id == "sub_add":
        await set_session(to, "sub_add_name", {})
        await send_text_message(
            to=to,
            text=(
                "➕ *Add Subscription*\n\n"
                "Type the service name:\n"
                "e.g. _Netflix, Spotify, Swiggy One_"
            )
        )

    elif button_id == "sub_confirm":
        # Guard: session must have service_name, amount, billing_day, category
        if not _session_has(session, "service_name", "amount", "billing_day", "category"):
            await _expired_session_reply(to)
            return
        parsed = _get_parsed(session)
        await confirm_subscription(to, parsed)

    elif button_id == "sub_cancel":
        await reset_session(to)
        await send_text_message(to=to, text="❌ Subscription not saved.")
        await send_main_menu(to)

    # ── Expense flow ──────────────────────────────────────────────────────────

    elif button_id == "exp_skip_merchant":
        if not _session_has(session, "amount"):
            await _expired_session_reply(to)
            return
        parsed = _get_parsed(session)
        parsed["merchant"] = "Not specified"
        await show_expense_confirmation(to, parsed)

    elif button_id == "exp_skip_other_detail":
        if not _session_has(session, "amount", "category"):
            await _expired_session_reply(to)
            return
        parsed = _get_parsed(session)
        parsed["sub_category"] = "Miscellaneous"
        await set_session(to, "expense_account", parsed)
        await send_account_picker(to, "💳 Paid from which account?")

    elif button_id == "exp_confirm":
        # Guard: session must have amount and category at minimum
        if not _session_has(session, "amount", "category"):
            await _expired_session_reply(to)
            return
        parsed = _get_parsed(session)
        await confirm_expense(to, parsed)

    elif button_id == "exp_cancel":
        await reset_session(to)
        await send_text_message(to=to, text="❌ Expense cancelled.")
        await send_main_menu(to)

    # ── Income flow ───────────────────────────────────────────────────────────

    elif button_id == "inc_confirm":
        # Guard: session must have amount and income_type
        if not _session_has(session, "amount", "income_type"):
            await _expired_session_reply(to)
            return
        parsed = _get_parsed(session)
        await confirm_income(to, parsed)

    elif button_id == "inc_cancel":
        await reset_session(to)
        await send_text_message(to=to, text="❌ Income entry cancelled.")
        await send_main_menu(to)

    # ── Navigation ────────────────────────────────────────────────────────────

    elif button_id == "nav_home":
        await send_main_menu(to)

    else:
        await send_main_menu(to)


# ══════════════════════════════════════════════════════════════════════════════
# LIST SELECTION HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def handle_list_selection(to: str, row_id: str, session):
    status = session["session_status"] if session else "idle"

    if row_id.startswith("inc_") and status == "income_type":
        type_map = {
            "inc_salary":     "Salary",
            "inc_freelance":  "Freelance",
            "inc_business":   "Business",
            "inc_rental":     "Rental",
            "inc_investment": "Investment",
            "inc_other":      "Other",
        }
        income_type = type_map.get(row_id, "Other")
        await set_session(to, "income_amount", {"income_type": income_type})
        await send_text_message(
            to=to,
            text=_with_stop_hint(
                f"💵 *{income_type}*\n\n"
                f"Enter the amount credited to your account:\n"
                f"_(net / take-home amount)_"
            )
        )

    elif row_id.startswith("cat_") and status == "expense_category":
        if not session:
            await _expired_session_reply(to)
            return
        parsed = _get_parsed(session)
        cat_map = {
            "cat_food_dining":   "Food & Dining",
            "cat_transport":     "Transport",
            "cat_shopping":      "Shopping",
            "cat_healthcare":    "Healthcare",
            "cat_housing":       "Housing",
            "cat_entertainment": "Entertainment",
            "cat_education":     "Education",
            "cat_other":         "Other",
        }
        parsed["category"] = cat_map.get(
            row_id,
            row_id.replace("cat_", "").replace("_", " ").title()
        )
        if parsed["category"] == "Other":
            await set_session(to, "expense_other_detail", parsed)
            await send_interactive_buttons(
                to=to,
                body_text=_with_stop_hint(
                    "Other expense selected.\n\n"
                    "Please type what this expense was for.\n"
                    "Example: *Pet care*, *Donation*, *Home supplies*\n\n"
                    "Or tap Skip to use *Miscellaneous*."
                ),
                buttons=[
                    {"id": "exp_skip_other_detail", "title": "Skip"},
                    {"id": "nav_home", "title": "Main Menu"},
                ]
            )
        else:
            await set_session(to, "expense_subcategory", parsed)
            await send_subcategory_menu(to, parsed["category"])

    elif row_id.startswith("sub_cat_") and status == "expense_subcategory":
        if not session:
            await _expired_session_reply(to)
            return
        parsed = _get_parsed(session)
        parsed["sub_category"] = (
            row_id.replace("sub_cat_", "").replace("_", " ").title()
        )
        await set_session(to, "expense_account", parsed)
        await send_account_picker(to, "💳 Paid from which account?")

    elif row_id.startswith("acc_") and status == "expense_account":
        if not session:
            await _expired_session_reply(to)
            return
        parsed = _get_parsed(session)
        parsed["account_id"] = int(row_id.replace("acc_", ""))
        await set_session(to, "expense_account", parsed)
        await send_payment_method_or_skip(to, parsed)

    elif row_id.startswith("pm_") and status == "expense_payment_method":
        if not session:
            await _expired_session_reply(to)
            return
        parsed = _get_parsed(session)
        parsed["payment_method"] = row_id.replace("pm_", "")
        await set_session(to, "expense_merchant", parsed)
        await send_merchant_step(to)

    elif row_id.startswith("acc_") and status == "income_account":
        if not session:
            await _expired_session_reply(to)
            return
        parsed = _get_parsed(session)
        parsed["account_id"] = int(row_id.replace("acc_", ""))
        await set_session(to, "income_confirm", parsed)
        await show_income_confirmation(to, parsed)

    elif row_id.startswith("sub_pay_") and status == "sub_paying":
        sub_id = int(row_id.replace("sub_pay_", ""))
        sub = await database.fetch_one(
            """SELECT s.id, s.merchant AS service_name, s.category, s.amount, 
                      COALESCE(EXTRACT(DAY FROM s.next_expected::date)::integer, 1) AS billing_day
               FROM recurring_payments s
               WHERE s.id = :id""",
            {"id": sub_id}
        )
        if not sub:
            await send_main_menu(to)
            return
        parsed = {
            "sub_id":       sub_id,
            "service_name": sub["service_name"],
            "category":     sub["category"] or "Other",
            "amount":       float(sub["amount"]),
            "billing_day":  sub["billing_day"],
        }
        await set_session(to, "sub_pay_account", parsed)
        await send_account_picker(to, f"🏦 Which account paid for {parsed['service_name']}?")

    elif row_id.startswith("acc_") and status == "sub_pay_account":
        if not session:
            await _expired_session_reply(to)
            return
        parsed = _get_parsed(session)
        parsed["account_id"] = int(row_id.replace("acc_", ""))
        acc_row = await database.fetch_one(
            "SELECT nickname FROM accounts WHERE id = :id",
            {"id": parsed["account_id"]}
        )
        parsed["account_name"] = acc_row["nickname"] if acc_row else "Selected account"
        await set_session(to, "sub_pay_confirm", parsed)
        await send_interactive_buttons(
            to=to,
            body_text=(
                f"Mark {parsed['service_name']} as paid?\n\n"
                f"Amount: ₹{parsed['amount']:,.0f}\n"
                f"From: {parsed['account_name']}\n\n"
                "This will log it as an expense and update the selected account."
            ),
            buttons=[
                {"id": "sub_pay_confirm", "title": "Confirm"},
                {"id": "sub_pay_cancel",  "title": "Cancel"},
                {"id": "nav_home",        "title": "Main Menu"},
            ]
        )

    elif row_id.startswith("sub_category_") and status == "sub_add_category":
        if not session:
            await _expired_session_reply(to)
            return
        parsed = _get_parsed(session)
        parsed["category"] = row_id.replace("sub_category_", "").replace("_", " ").title()
        await set_session(to, "sub_add_confirm", parsed)
        await show_subscription_confirmation(to, parsed)

    else:
        await send_main_menu(to)


# ══════════════════════════════════════════════════════════════════════════════
# TEXT MESSAGE HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def handle_text_message(to: str, text: str, session):
    status  = session["session_status"] if session else "idle"
    text_lo = text.lower().strip()

    if text_lo in {"hi", "hello", "hey", "start", "menu", "home"}:
        await send_main_menu(to)
        return

    if status == "expense_amount":
        try:
            amount = float(text.replace(",", "").replace("₹", "").strip())
            if amount <= 0:
                raise ValueError
            await set_session(to, "expense_category", {"amount": amount})
            await send_category_menu(to, amount)
        except ValueError:
            await send_text_message(to=to, text="⚠️ Please enter a valid amount.\nE.g. *350* or *1,200*")

    elif status == "income_amount":
        try:
            amount = float(text.replace(",", "").replace("₹", "").strip())
            if amount <= 0:
                raise ValueError
            parsed = _get_parsed(session)
            parsed["amount"] = amount
            await set_session(to, "income_account", parsed)
            await send_account_picker(to, "🏦 Which account received this income?")
        except ValueError:
            await send_text_message(to=to, text="⚠️ Please enter a valid amount.\nE.g. *92000*")

    elif status == "expense_merchant":
        parsed = _get_parsed(session)
        parsed["merchant"] = text.strip()
        await show_expense_confirmation(to, parsed)

    elif status == "expense_other_detail":
        detail = text.strip()
        if len(detail) < 2:
            await send_text_message(
                to=to,
                text="⚠️ Please type a little more detail for this expense.\nExample: *Pet care*"
            )
            return
        parsed = _get_parsed(session)
        parsed["sub_category"] = detail
        await set_session(to, "expense_account", parsed)
        await send_account_picker(to, "💳 Paid from which account?")

    elif status == "sub_add_name":
        parsed = {"service_name": text.strip()}
        await set_session(to, "sub_add_amount", parsed)
        await send_text_message(
            to=to,
            text=_with_stop_hint(
                f"💰 *{text.strip()}*\n\nEnter the monthly amount (₹):"
            )
        )

    elif status == "sub_add_amount":
        try:
            amount = float(text.replace(",", "").replace("₹", "").strip())
            if amount <= 0:
                raise ValueError
            parsed = _get_parsed(session)
            parsed["amount"] = amount
            await set_session(to, "sub_add_day", parsed)
            await send_text_message(
                to=to,
                text=_with_stop_hint(
                    "📅 *Billing day?*\n\n"
                    "Which day of the month is it charged?\n"
                    "e.g. _1, 5, 15, 20_"
                )
            )
        except ValueError:
            await send_text_message(to=to, text="⚠️ Please enter a valid amount.\nE.g. *649*")

    elif status == "sub_add_day":
        try:
            day = int(text.strip())
            if not 1 <= day <= 31:
                raise ValueError
            parsed = _get_parsed(session)
            parsed["billing_day"] = day
            await set_session(to, "sub_add_category", parsed)
            await send_subscription_category_picker(to)
        except ValueError:
            await send_text_message(to=to, text="⚠️ Please enter a day between 1 and 31.")

    else:
        await send_text_message(
            to=to,
            text=(
                "I didn't quite understand that.\n\n"
                "Type *menu* to see the main menu or *help* for commands.\n"
                "Type *stop* to cancel the current flow."
            )
        )


# ══════════════════════════════════════════════════════════════════════════════
# EXPENSE FLOW HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def send_category_menu(to: str, amount: float):
    await send_list_message(
        to=to,
        body_text=_with_stop_hint(f"📂 Select a category for ₹{amount:,.0f}:"),
        button_label="Select Category",
        sections=[
            {
                "title": "Common",
                "rows": [
                    {"id": "cat_food_dining",   "title": "🍔 Food & Dining"},
                    {"id": "cat_transport",     "title": "🚗 Transport"},
                    {"id": "cat_shopping",      "title": "🛍️ Shopping"},
                    {"id": "cat_healthcare",    "title": "💊 Healthcare"},
                ]
            },
            {
                "title": "Others",
                "rows": [
                    {"id": "cat_housing",       "title": "🏠 Housing"},
                    {"id": "cat_entertainment", "title": "🎬 Entertainment"},
                    {"id": "cat_education",     "title": "📚 Education"},
                    {"id": "cat_other",         "title": "📦 Other"},
                ]
            }
        ]
    )


async def send_subcategory_menu(to: str, category: str):
    subcats = {
        "Food & Dining": [
            {"id": "sub_cat_groceries",     "title": "🛒 Groceries"},
            {"id": "sub_cat_restaurants",   "title": "🍽️ Restaurants"},
            {"id": "sub_cat_cafes",         "title": "☕ Cafes"},
            {"id": "sub_cat_food_delivery", "title": "🚴 Food Delivery"},
            {"id": "sub_cat_snacks",        "title": "🍿 Snacks & Beverages"},
        ],
        "Transport": [
            {"id": "sub_cat_fuel",          "title": "⛽ Fuel"},
            {"id": "sub_cat_cab",           "title": "🚕 Cab / Auto"},
            {"id": "sub_cat_public",        "title": "🚌 Public Transport"},
            {"id": "sub_cat_parking",       "title": "🅿️ Parking / Toll"},
            {"id": "sub_cat_vehicle",       "title": "🔧 Vehicle Maintenance"},
        ],
        "Shopping": [
            {"id": "sub_cat_clothing",      "title": "👕 Clothing & Fashion"},
            {"id": "sub_cat_electronics",   "title": "📱 Electronics"},
            {"id": "sub_cat_home",          "title": "🏠 Home & Kitchen"},
            {"id": "sub_cat_personal_care", "title": "🧴 Personal Care"},
            {"id": "sub_cat_gifts",         "title": "🎁 Gifts"},
        ],
        "Healthcare": [
            {"id": "sub_cat_medicine",      "title": "💊 Medicine"},
            {"id": "sub_cat_doctor",        "title": "🏥 Doctor / Hospital"},
            {"id": "sub_cat_lab",           "title": "🧪 Lab Tests"},
            {"id": "sub_cat_fitness",       "title": "🏋️ Gym / Fitness"},
        ],
        "Housing": [
            {"id": "sub_cat_rent",          "title": "🏠 Rent / EMI"},
            {"id": "sub_cat_utilities",     "title": "💡 Electricity / Water"},
            {"id": "sub_cat_internet",      "title": "🌐 Internet / Cable"},
            {"id": "sub_cat_maintenance",   "title": "🔧 Maintenance / Repair"},
        ],
        "Entertainment": [
            {"id": "sub_cat_movies",        "title": "🎬 Movies / Events"},
            {"id": "sub_cat_games",         "title": "🎮 Gaming"},
            {"id": "sub_cat_travel",        "title": "✈️ Travel / Vacation"},
            {"id": "sub_cat_hobbies",       "title": "🎨 Hobbies"},
        ],
        "Education": [
            {"id": "sub_cat_fees",          "title": "📚 Tuition / Fees"},
            {"id": "sub_cat_books",         "title": "📖 Books / Stationery"},
            {"id": "sub_cat_courses",       "title": "💻 Online Courses"},
        ],
    }
    rows = subcats.get(category, [{"id": "sub_cat_general", "title": "📦 General"}])
    await send_list_message(
        to=to,
        body_text=_with_stop_hint(f"📂 {category}\n\nWhich type of expense?"),
        button_label="Select",
        sections=[{"title": category, "rows": rows}]
    )


async def send_payment_method_or_skip(to: str, parsed: dict):
    account_id = parsed.get("account_id")

    if not account_id:
        await set_session(to, "expense_merchant", parsed)
        await send_merchant_step(to)
        return

    acc = await database.fetch_one(
        "SELECT account_type, account_category, nickname FROM accounts WHERE id = :id",
        {"id": account_id}
    )

    if not acc:
        await set_session(to, "expense_merchant", parsed)
        await send_merchant_step(to)
        return

    acc_type = acc["account_type"]

    if acc_type in ("credit_card", "prepaid_card", "wallet", "cash"):
        parsed["payment_method"] = acc_type
        await set_session(to, "expense_merchant", parsed)
        await send_merchant_step(to)
    else:
        await set_session(to, "expense_payment_method", parsed)
        await send_list_message(
            to=to,
            body_text=_with_stop_hint(f"📱 *How did you pay?*\n\nAccount: {acc['nickname']}"),
            button_label="Select Method",
            sections=[
                {
                    "title": "Digital Payments",
                    "rows": [
                        {"id": "pm_upi",        "title": "📱 UPI",        "description": "GPay, PhonePe, Paytm UPI, BHIM"},
                        {"id": "pm_debit_card", "title": "💳 Debit Card", "description": "Swipe or tap at POS / online"},
                        {"id": "pm_auto_debit", "title": "🔄 Auto Debit", "description": "ECS, NACH, standing instruction"},
                    ]
                },
                {
                    "title": "Bank Transfers",
                    "rows": [
                        {"id": "pm_neft",          "title": "🏛️ NEFT",          "description": "National Electronic Funds Transfer"},
                        {"id": "pm_imps",          "title": "🏛️ IMPS",          "description": "Immediate Payment Service"},
                        {"id": "pm_rtgs",          "title": "🏛️ RTGS",          "description": "Real Time Gross Settlement"},
                        {"id": "pm_cheque",        "title": "📄 Cheque",         "description": "Physical cheque payment"},
                        {"id": "pm_bank_transfer", "title": "🏦 Other Transfer", "description": "Any other bank transfer"},
                    ]
                }
            ]
        )


async def send_merchant_step(to: str):
    await send_interactive_buttons(
        to=to,
        body_text=_with_stop_hint(
            "🏪 *Merchant name?* (optional)\n\n"
            "Type the name e.g. _Swiggy, DMart, Uber_\n"
            "or tap Skip to save it as *Not specified*."
        ),
        buttons=[
            {"id": "exp_skip_merchant", "title": "⏭️ Skip"},
        ]
    )


async def show_expense_confirmation(to: str, parsed: dict):
    await set_session(to, "expense_confirm", parsed)

    amount     = parsed.get("amount", 0)
    account_id = parsed.get("account_id")
    acc_name   = f"ID {account_id or '-'}"
    warning_line = ""

    if account_id:
        acc_row = await database.fetch_one(
            "SELECT nickname, account_type, balance, credit_limit, outstanding FROM accounts WHERE id = :id",
            {"id": account_id}
        )
        if acc_row:
            acc_name = acc_row["nickname"]
            if acc_row["account_type"] == "credit_card":
                limit       = float(acc_row["credit_limit"] or 0)
                outstanding = float(acc_row["outstanding"] or 0)
                available   = limit - outstanding
                if limit > 0 and amount > available:
                    warning_line = f"\n⚠️ *Over limit!* Available: ₹{available:,.0f}\n"
            else:
                balance = float(acc_row["balance"] or 0)
                if amount > balance:
                    warning_line = f"\n⚠️ *Low balance!* {acc_name} has ₹{balance:,.0f}\n"

    pm       = parsed.get("payment_method", "")
    pm_label = PAYMENT_METHOD_LABELS.get(pm, pm.replace("_", " ").title() if pm else "")
    pm_line  = f"📱 Via:       {pm_label}\n" if pm_label else ""
    merch_line = f"🏪 Merchant:  {parsed.get('merchant')}\n" if parsed.get("merchant") else ""

    await send_interactive_buttons(
        to=to,
        body_text=(
            f"✅ *Confirm this expense?*\n\n"
            f"💸 Amount:    ₹{amount:,.0f}\n"
            f"📂 Category:  {parsed.get('category', '-')} › {parsed.get('sub_category', '-')}\n"
            f"💳 Account:   {acc_name}\n"
            f"{pm_line}"
            f"{merch_line}"
            f"📅 Date:      Today"
            f"{warning_line}"
        ),
        buttons=[
            {"id": "exp_confirm", "title": "✅ Confirm"},
            {"id": "exp_cancel",  "title": "❌ Cancel"},
            {"id": "nav_home",    "title": "🏠 Main Menu"},
        ]
    )


async def confirm_expense(to: str, parsed: dict):
    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    try:
        await record_transaction_with_account_update(
            user_id        = user["id"],
            account_id     = parsed.get("account_id"),
            amount         = parsed.get("amount"),
            type_          = "expense",
            category       = parsed.get("category"),
            sub_category   = parsed.get("sub_category"),
            merchant       = parsed.get("merchant"),
            is_essential   = True,
            payment_method = parsed.get("payment_method"),
        )
    except ValueError as exc:
        await reset_session(to)
        await send_text_message(to=to, text=f"⚠️ {exc}\n\nPlease start again from the menu.")
        await send_main_menu(to)
        return

    # Run cache refresh in background — no need to block the reply
    asyncio.create_task(refresh_analytics_cache(user["id"]))
    await reset_session(to)

    acc_name = f"Account ID {parsed.get('account_id', '')}"
    if parsed.get("account_id"):
        acc_row = await database.fetch_one(
            "SELECT nickname FROM accounts WHERE id = :id",
            {"id": parsed["account_id"]}
        )
        if acc_row:
            acc_name = acc_row["nickname"]

    pm_label   = PAYMENT_METHOD_LABELS.get(parsed.get("payment_method", ""), "")
    via_part   = f" via {pm_label}" if pm_label else ""
    merch_part = f"{parsed.get('merchant')} · " if parsed.get("merchant") else ""
    sub_part   = f"{parsed.get('sub_category')} · " if parsed.get("sub_category") else ""

    await send_interactive_buttons(
        to=to,
        body_text=(
            f"✅ *₹{parsed.get('amount', 0):,.0f} logged!*\n\n"
            f"{merch_part}{sub_part}{acc_name}{via_part}"
        ),
        buttons=[
            {"id": "menu_expense",      "title": "🔄 Log Another"},
            {"id": "menu_transactions", "title": "💸 Transactions"},
            {"id": "nav_home",          "title": "🏠 Main Menu"},
        ]
    )


# ══════════════════════════════════════════════════════════════════════════════
# INCOME FLOW HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def show_income_confirmation(to: str, parsed: dict):
    acc_name = f"ID {parsed.get('account_id', '-')}"
    if parsed.get("account_id"):
        acc_row = await database.fetch_one(
            "SELECT nickname FROM accounts WHERE id = :id",
            {"id": parsed["account_id"]}
        )
        if acc_row:
            acc_name = acc_row["nickname"]

    await send_interactive_buttons(
        to=to,
        body_text=(
            f"✅ *Confirm this income?*\n\n"
            f"💵 Amount:  ₹{parsed.get('amount', 0):,.0f}\n"
            f"📂 Type:    {parsed.get('income_type', '-')}\n"
            f"🏦 Account: {acc_name}\n"
            f"📅 Date:    Today"
        ),
        buttons=[
            {"id": "inc_confirm", "title": "✅ Confirm"},
            {"id": "inc_cancel",  "title": "❌ Cancel"},
            {"id": "nav_home",    "title": "🏠 Main Menu"},
        ]
    )


async def confirm_income(to: str, parsed: dict):
    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    try:
        await record_transaction_with_account_update(
            user_id      = user["id"],
            account_id   = parsed.get("account_id"),
            amount       = parsed.get("amount"),
            type_        = "income",
            category     = parsed.get("income_type"),
            sub_category = None,
            merchant     = None,
            is_essential = True,
        )
    except ValueError as exc:
        await reset_session(to)
        await send_text_message(to=to, text=f"⚠️ {exc}\n\nPlease start again from the menu.")
        await send_main_menu(to)
        return

    asyncio.create_task(refresh_analytics_cache(user["id"]))
    await reset_session(to)

    acc_name = f"Account ID {parsed.get('account_id', '')}"
    if parsed.get("account_id"):
        acc_row = await database.fetch_one(
            "SELECT nickname FROM accounts WHERE id = :id",
            {"id": parsed["account_id"]}
        )
        if acc_row:
            acc_name = acc_row["nickname"]

    await send_interactive_buttons(
        to=to,
        body_text=(
            f"✅ *₹{parsed.get('amount', 0):,.0f} income logged!*\n\n"
            f"{parsed.get('income_type', 'Income')} · {acc_name}"
        ),
        buttons=[
            {"id": "menu_income",       "title": "🔄 Log Another"},
            {"id": "menu_transactions", "title": "💸 Transactions"},
            {"id": "nav_home",          "title": "🏠 Main Menu"},
        ]
    )


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNT PICKER HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def send_account_picker(to: str, prompt: str):
    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    sections = await get_accounts_for_picker(user["id"])

    if not sections:
        await send_text_message(
            to=to,
            text="⚠️ No accounts found.\n\nPlease add your accounts from the web dashboard first."
        )
        return

    await send_list_message(
        to=to,
        body_text=_with_stop_hint(prompt),
        button_label="Select Account",
        sections=sections,
    )


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS — ACCOUNT BALANCES
# ══════════════════════════════════════════════════════════════════════════════

async def send_balance_summary(to: str):
    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    accounts = await get_user_accounts(user["id"])

    if not accounts:
        await send_interactive_buttons(
            to=to,
            body_text="💳 No accounts found.\n\nAdd accounts from the web dashboard.",
            buttons=[{"id": "nav_home", "title": "🏠 Main Menu"}]
        )
        return

    groups = {"bank": [], "card": [], "digital": [], "cash": []}
    for acc in accounts:
        cat = acc["account_category"]
        if cat in groups:
            groups[cat].append(acc)

    category_labels = {
        "bank":    "🏦 Bank Accounts",
        "card":    "💳 Cards",
        "digital": "📱 Wallets",
        "cash":    "💵 Cash",
    }

    lines      = ["💳 *Your Account Balances*\n"]
    total_bank = 0

    for cat, label in category_labels.items():
        accs = groups[cat]
        if not accs:
            continue
        lines.append(label)
        for acc in accs:
            star = "⭐ " if acc["is_default"] else "    "
            name = acc["nickname"]
            if acc["account_type"] == "credit_card":
                outstanding = float(acc["outstanding"] or 0)
                limit       = float(acc["credit_limit"] or 0)
                available   = limit - outstanding if limit else 0
                lines.append(f"{star}{name}")
                lines.append(f"     ₹{outstanding:,.0f} due  ·  ₹{available:,.0f} available")
            elif acc["account_type"] == "prepaid_card":
                balance = float(acc["balance"] or 0)
                lines.append(f"{star}{name} (Prepaid)   ₹{balance:,.0f}")
            else:
                balance = float(acc["balance"] or 0)
                lines.append(f"{star}{name}   ₹{balance:,.0f}")
                if cat == "bank":
                    total_bank += balance
        lines.append("")

    if total_bank > 0:
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(f"Total Bank Balance: ₹{total_bank:,.0f}")

    await send_interactive_buttons(
        to=to,
        body_text="\n".join(lines),
        buttons=[
            {"id": "sum_thismonth", "title": "📅 This Month"},
            {"id": "nav_home",      "title": "🏠 Main Menu"},
        ]
    )


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS — THIS MONTH
# ══════════════════════════════════════════════════════════════════════════════

async def send_monthly_summary(to: str):
    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    uid         = user["id"]
    month       = datetime.now().strftime("%Y-%m")
    month_label = datetime.now().strftime("%B %Y")

    income = float(await database.fetch_val(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=:uid AND type='income' AND TO_CHAR(transaction_date,'YYYY-MM')=:m",
        {"uid": uid, "m": month}
    ) or 0)
    expense = float(await database.fetch_val(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=:uid AND type='expense' AND TO_CHAR(transaction_date,'YYYY-MM')=:m",
        {"uid": uid, "m": month}
    ) or 0)
    subs = float(await database.fetch_val(
        "SELECT COALESCE(SUM(amount),0) FROM recurring_payments WHERE user_id=:uid AND status IN ('active', 'confirmed')",
        {"uid": uid}
    ) or 0)

    saved = income - expense
    pct   = int((expense / income * 100)) if income else 0

    top_cat = await database.fetch_one(
        """SELECT category, SUM(amount) as total FROM transactions
           WHERE user_id=:uid AND type='expense'
             AND TO_CHAR(transaction_date,'YYYY-MM')=:m AND category IS NOT NULL
           GROUP BY category ORDER BY total DESC LIMIT 1""",
        {"uid": uid, "m": month}
    )

    lines = [
        f"📊 *{month_label} Summary*\n",
        f"💵 Income:          ₹{income:,.0f}",
        f"💸 Expenses:        ₹{expense:,.0f}",
        f"📺 Subscriptions:   ₹{subs:,.0f}/mo",
        f"💰 Saved:           ₹{saved:,.0f}",
        f"📈 Spent:           {pct}% of income",
    ]
    if top_cat:
        lines.append(f"\n🔥 Top: {top_cat['category']}  ₹{float(top_cat['total']):,.0f}")
    if income == 0 and expense == 0:
        lines.append("\n📭 No transactions this month yet.")
    elif saved < 0:
        lines.append(f"\n⚠️ Overspent by ₹{abs(saved):,.0f}!")
    elif pct < 50:
        lines.append("\n✅ Great — saved more than half your income.")

    await send_interactive_buttons(
        to=to,
        body_text="\n".join(lines),
        buttons=[
            {"id": "sum_category", "title": "📂 By Category"},
            {"id": "sum_recent",   "title": "🕐 Recent 10"},
            {"id": "nav_home",     "title": "🏠 Main Menu"},
        ]
    )


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS — RECENT 10 TRANSACTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def send_recent_transactions(to: str):
    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    rows = await database.fetch_all(
        """SELECT t.amount, t.type, t.category, t.sub_category,
                  t.merchant, t.transaction_date, t.payment_method,
                  a.nickname as acc_name
           FROM transactions t
           LEFT JOIN accounts a ON t.account_id = a.id
           WHERE t.user_id = :uid
           ORDER BY t.transaction_date DESC LIMIT 10""",
        {"uid": user["id"]}
    )

    if not rows:
        await send_interactive_buttons(
            to=to,
            body_text="📋 No transactions yet.\n\nStart by logging your first expense!",
            buttons=[
                {"id": "menu_expense", "title": "💸 Log Expense"},
                {"id": "nav_home",     "title": "🏠 Main Menu"},
            ]
        )
        return

    type_icon = {"expense": "💸", "income": "💵", "transfer": "🔄"}
    lines     = ["🕐 *Recent 10 Transactions*\n"]

    for i, t in enumerate(rows, 1):
        icon   = type_icon.get(t["type"], "•")
        date_s = t["transaction_date"].strftime("%d %b") if t["transaction_date"] else "—"
        cat    = t["sub_category"] or t["category"] or t["type"].title()
        merch  = f" · {t['merchant']}" if t["merchant"] else ""
        acc    = f" · {t['acc_name']}" if t["acc_name"] else ""
        pm_lbl = PAYMENT_METHOD_LABELS.get(t["payment_method"] or "", "")
        via    = f" · {pm_lbl}" if pm_lbl else ""
        lines.append(f"{i}. {icon} *₹{float(t['amount']):,.0f}*  {cat}{merch}")
        lines.append(f"    {date_s}{acc}{via}")

    await send_interactive_buttons(
        to=to,
        body_text="\n".join(lines),
        buttons=[
            {"id": "sum_category",  "title": "📂 By Category"},
            {"id": "sum_thismonth", "title": "📅 This Month"},
            {"id": "nav_home",      "title": "🏠 Main Menu"},
        ]
    )


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS — CATEGORY BREAKDOWN
# ══════════════════════════════════════════════════════════════════════════════

async def send_category_summary(to: str):
    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    month       = datetime.now().strftime("%Y-%m")
    month_label = datetime.now().strftime("%B %Y")

    rows = await database.fetch_all(
        """SELECT category, COUNT(*) as txn_count, SUM(amount) as total
           FROM transactions
           WHERE user_id=:uid AND type='expense'
             AND TO_CHAR(transaction_date,'YYYY-MM')=:m AND category IS NOT NULL
           GROUP BY category ORDER BY total DESC""",
        {"uid": user["id"], "m": month}
    )

    if not rows:
        await send_interactive_buttons(
            to=to,
            body_text=f"📂 No expenses recorded in {month_label} yet.",
            buttons=[
                {"id": "menu_expense", "title": "💸 Log Expense"},
                {"id": "nav_home",     "title": "🏠 Main Menu"},
            ]
        )
        return

    grand_total = sum(float(r["total"]) for r in rows)
    cat_icons   = {
        "Food & Dining": "🍔", "Transport": "🚗", "Shopping": "🛍️",
        "Healthcare": "💊", "Housing": "🏠", "Entertainment": "🎬",
        "Education": "📚", "Other": "📦", "Subscriptions": "📺",
    }

    lines = [f"📂 *{month_label} — By Category*\n"]
    for r in rows:
        icon   = cat_icons.get(r["category"], "•")
        amt    = float(r["total"])
        pct    = int(amt / grand_total * 100) if grand_total else 0
        filled = pct // 10
        bar    = "█" * filled + "░" * (10 - filled)
        lines.append(f"{icon} {r['category']}")
        lines.append(f"   {bar} {pct}%   ₹{amt:,.0f}  ({r['txn_count']} txns)")

    lines.append(f"\n💸 *Total: ₹{grand_total:,.0f}*")

    await send_interactive_buttons(
        to=to,
        body_text="\n".join(lines),
        buttons=[
            {"id": "sum_recent",    "title": "🕐 Recent 10"},
            {"id": "sum_thismonth", "title": "📅 This Month"},
            {"id": "nav_home",      "title": "🏠 Main Menu"},
        ]
    )


# ══════════════════════════════════════════════════════════════════════════════
# SUBSCRIPTIONS LIST
# ══════════════════════════════════════════════════════════════════════════════

async def send_subscriptions_list(to: str):
    from app.repositories.db_helpers import get_subscriptions

    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    subs  = await get_subscriptions(user["id"])
    today = date.today()

    if not subs:
        await send_interactive_buttons(
            to=to,
            body_text="📺 No active subscriptions found.\n\nAdd one using the button below.",
            buttons=[
                {"id": "sub_add",  "title": "➕ Add New"},
                {"id": "nav_home", "title": "🏠 Main Menu"},
            ]
        )
        return

    month_str = today.strftime("%Y-%m")
    paid_rows = await database.fetch_all(
        """SELECT DISTINCT subscription_id FROM transactions
           WHERE user_id=:uid AND subscription_id IS NOT NULL
             AND type='expense' AND TO_CHAR(transaction_date,'YYYY-MM')=:m""",
        {"uid": user["id"], "m": month_str}
    )
    paid_ids = {r["subscription_id"] for r in paid_rows}

    total = sum(float(s["amount"]) for s in subs)
    lines = [f"📺 *Active Subscriptions ({len(subs)})*\n"]

    for i, s in enumerate(subs, 1):
        paid_tag = " ✅" if s["id"] in paid_ids else ""
        lines.append(
            f"{i}. {s['service_name']}{paid_tag}"
            f"   ₹{float(s['amount']):,.0f}/mo"
            f"  · {ordinal(s['billing_day'])}"
            f"  · {s['category'] or 'Other'}"
        )

    lines.append(f"\n━━━━━━━━━━━━━━━━━━")
    lines.append(f"Monthly total: ₹{total:,.0f}")
    lines.append(f"Annual total:  ₹{total * 12:,.0f}")

    await send_interactive_buttons(
        to=to,
        body_text="\n".join(lines),
        buttons=[
            {"id": "sub_mark_paid", "title": "✅ Mark as Paid"},
            {"id": "sub_add",       "title": "➕ Add New"},
            {"id": "nav_home",      "title": "🏠 Main Menu"},
        ]
    )


# ══════════════════════════════════════════════════════════════════════════════
# SUBSCRIPTION MARK-AS-PAID FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def send_subscription_picker(to: str):
    from app.repositories.db_helpers import get_subscriptions

    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    subs      = await get_subscriptions(user["id"])
    today     = date.today()
    month_str = today.strftime("%Y-%m")

    paid_rows = await database.fetch_all(
        """SELECT DISTINCT subscription_id FROM transactions
           WHERE user_id=:uid AND subscription_id IS NOT NULL
             AND type='expense' AND TO_CHAR(transaction_date,'YYYY-MM')=:m""",
        {"uid": user["id"], "m": month_str}
    )
    paid_ids = {r["subscription_id"] for r in paid_rows}
    unpaid   = [s for s in subs if s["id"] not in paid_ids]

    if not unpaid:
        await send_interactive_buttons(
            to=to,
            body_text="✅ All subscriptions are already marked as paid this month!",
            buttons=[
                {"id": "menu_subscriptions", "title": "📺 Subscriptions"},
                {"id": "nav_home",           "title": "🏠 Main Menu"},
            ]
        )
        return

    rows = [
        {
            "id":          f"sub_pay_{s['id']}",
            "title":       s["service_name"][:24],
            "description": f"₹{float(s['amount']):,.0f}/mo · {s['category'] or 'Other'}"[:72],
        }
        for s in unpaid
    ]

    await set_session(to, "sub_paying", {})
    await send_list_message(
        to=to,
        body_text="📺 *Mark Subscription as Paid*\n\nWhich one did you pay?",
        button_label="Select Service",
        sections=[{"title": "Unpaid This Month", "rows": rows}]
    )


async def send_subscription_category_picker(to: str):
    await send_list_message(
        to=to,
        body_text=_with_stop_hint("📂 Select a category for this subscription."),
        button_label="Select Category",
        sections=[{
            "title": "Categories",
            "rows": [
                {"id": "sub_category_entertainment", "title": "Entertainment", "description": "Streaming, music, media"},
                {"id": "sub_category_productivity", "title": "Productivity", "description": "Work, storage, tools"},
                {"id": "sub_category_utilities", "title": "Utilities", "description": "Internet, phone, services"},
                {"id": "sub_category_software", "title": "Software", "description": "Apps, SaaS, developer tools"},
                {"id": "sub_category_health", "title": "Health", "description": "Fitness and wellness"},
                {"id": "sub_category_other", "title": "Other", "description": "Anything else"},
            ]
        }]
    )


async def confirm_subscription_payment(to: str, parsed: dict):
    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    account_id = parsed.get("account_id")
    amount     = parsed.get("amount", 0)
    sub_id     = parsed.get("sub_id")

    try:
        await record_transaction_with_account_update(
            user_id         = user["id"],
            account_id      = account_id,
            amount          = amount,
            type_           = "expense",
            category        = parsed.get("category") or "Other",
            sub_category    = parsed.get("service_name"),
            merchant        = parsed.get("service_name"),
            is_essential    = True,
            subscription_id = sub_id,
            payment_method  = None,
        )
    except ValueError as exc:
        await reset_session(to)
        await send_text_message(to=to, text=f"⚠️ {exc}\n\nPlease start again from the menu.")
        await send_main_menu(to)
        return

    asyncio.create_task(refresh_analytics_cache(user["id"]))
    await reset_session(to)

    await send_interactive_buttons(
        to=to,
        body_text=(
            f"✅ *{parsed.get('service_name')} marked as paid!*\n\n"
            f"💰 ₹{amount:,.0f} logged as expense\n"
            f"🏦 Deducted from: {parsed.get('account_name', 'your account')}"
        ),
        buttons=[
            {"id": "sub_mark_paid",      "title": "✅ Mark Another"},
            {"id": "menu_subscriptions", "title": "📺 Subscriptions"},
            {"id": "nav_home",           "title": "🏠 Main Menu"},
        ]
    )


# ══════════════════════════════════════════════════════════════════════════════
# SUBSCRIPTION ADD FLOW HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def show_subscription_confirmation(to: str, parsed: dict):
    await send_interactive_buttons(
        to=to,
        body_text=(
            f"Confirm subscription?\n\n"
            f"Service: {parsed.get('service_name')}\n"
            f"Amount: ₹{parsed.get('amount', 0):,.0f}/mo\n"
            f"Category: {parsed.get('category', 'Other')}\n"
            f"Billed: {ordinal(parsed.get('billing_day', 1))} of every month\n\n"
            "You will choose the payment account when you mark it as paid."
        ),
        buttons=[
            {"id": "sub_confirm", "title": "Confirm"},
            {"id": "sub_cancel",  "title": "Cancel"},
            {"id": "nav_home",    "title": "Main Menu"},
        ]
    )


async def confirm_subscription(to: str, parsed: dict):
    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    if not parsed.get("service_name") or not parsed.get("amount") or not parsed.get("billing_day") or not parsed.get("category"):
        await reset_session(to)
        await send_text_message(
            to=to,
            text="⚠️ Session data was lost. Please add the subscription again."
        )
        await send_main_menu(to)
        return

    billing_day = parsed.get("billing_day", 1)
    today = date.today()
    try:
        due = today.replace(day=billing_day)
    except ValueError:
        import calendar
        last_day = calendar.monthrange(today.year, today.month)[1]
        due = today.replace(day=last_day)
    
    if due < today:
        next_month_date = today + timedelta(days=32)
        try:
            due = next_month_date.replace(day=billing_day)
        except ValueError:
            import calendar
            last_day = calendar.monthrange(next_month_date.year, next_month_date.month)[1]
            due = next_month_date.replace(day=last_day)

    await database.execute(
        """INSERT INTO recurring_payments
           (user_id, account_id, merchant, category, amount, frequency, next_expected, status)
           VALUES (:uid, :aid, :name, :category, :amt, 'monthly', :next_expected, 'confirmed')""",
        {
            "uid":           user["id"],
            "aid":           parsed.get("account_id"),
            "name":          parsed.get("service_name"),
            "category":      parsed.get("category"),
            "amt":           parsed.get("amount"),
            "next_expected": due,
        }
    )

    await reset_session(to)

    await send_interactive_buttons(
        to=to,
        body_text=(
            f"✅ *{parsed.get('service_name')} added!*\n\n"
            f"{parsed.get('category')} · ₹{parsed.get('amount', 0):,.0f}/mo · "
            f"{ordinal(parsed.get('billing_day', 1))} of every month"
        ),
        buttons=[
            {"id": "sub_view",           "title": "📋 View All"},
            {"id": "menu_subscriptions", "title": "📺 Subscriptions"},
            {"id": "nav_home",           "title": "🏠 Main Menu"},
        ]
    )
