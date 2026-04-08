from datetime import date
from difflib import get_close_matches

from app.repositories.db_helpers import (
    create_goal,
    database,
    get_goal_by_id,
    get_goals,
    get_monthly_expense_total,
    get_total_budget,
    get_user_by_phone,
    reset_session,
    set_session,
    update_goal_current_amount,
)
from app.services import bot_logic as legacy
from app.integrations.whatsapp_client import (
    send_interactive_buttons,
    send_list_message,
    send_text_message,
)


send_not_registered = legacy.send_not_registered


def _get_parsed(session) -> dict:
    if not session:
        return {}
    result = session.get("parsed_result", {})
    return dict(result) if result else {}


def _session_has(session, *fields) -> bool:
    parsed = _get_parsed(session)
    return all(parsed.get(field) is not None and parsed.get(field) != "" for field in fields)


def _goal_progress(goal) -> tuple[float, int]:
    current = float(goal["current_amount"] or 0)
    target = float(goal["target_amount"] or 0)
    pct = int((current / target) * 100) if target > 0 else 0
    return current, min(pct, 999)


def _goal_value(goal, field: str, default=None):
    try:
        value = goal[field]
    except (KeyError, TypeError):
        return default
    return default if value is None else value


def _goal_icon(goal) -> str:
    return _goal_value(goal, "icon", "🎯")


def _normalize_command(text: str) -> str:
    return " ".join(
        "".join(ch.lower() if (ch.isalnum() or ch.isspace()) else " " for ch in text).split()
    )


def _resolve_command(text: str, allowed: set[str] | None = None) -> str | None:
    command_map = {
        "hi": "start",
        "hello": "start",
        "hey": "start",
        "hii": "start",
        "start": "start",
        "menu": "menu",
        "main menu": "menu",
        "home": "menu",
        "help": "help",
        "hlp": "help",
        "stop": "stop",
        "cancel": "stop",
        "exit": "stop",
        "expense": "expense",
        "add expense": "expense",
        "income": "income",
        "add income": "income",
        "transactions": "transactions",
        "transaction": "transactions",
        "subscriptions": "subscriptions",
        "subscription": "subscriptions",
        "goals": "goals",
        "goal": "goals",
        "reports": "reports",
        "report": "reports",
        "analytics": "reports",
        "summary": "reports",
        "accounts": "accounts",
        "account": "accounts",
        "balances": "accounts",
        "balance": "accounts",
    }
    normalized = _normalize_command(text)
    if not normalized:
        return None

    if normalized in command_map:
        candidate = command_map[normalized]
        return candidate if allowed is None or candidate in allowed else None

    candidates = [key for key, value in command_map.items() if allowed is None or value in allowed]
    match = get_close_matches(normalized, candidates, n=1, cutoff=0.78)
    if not match:
        return None
    return command_map[match[0]]


async def _expired_session_reply(to: str):
    await send_text_message(to=to, text="Your session expired. Please start again.")
    await send_main_menu(to)


async def send_reports_menu(to: str):
    await send_list_message(
        to=to,
        body_text="Reports\n\nChoose what you want to see.",
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


async def send_transactions_menu(to: str):
    await send_interactive_buttons(
        to=to,
        body_text=(
            "Transactions\n\n"
            "Choose one option below.\n\n"
            "Type *menu* anytime to see the main options."
        ),
        buttons=[
            {"id": "menu_expense", "title": "Add Expense"},
            {"id": "menu_income", "title": "Add Income"},
            {"id": "sum_thismonth", "title": "This Month"},
        ]
    )


async def send_subscriptions_menu(to: str):
    await send_interactive_buttons(
        to=to,
        body_text=(
            "Subscriptions\n\n"
            "Choose one option below.\n\n"
            "Type *menu* anytime to see the main options."
        ),
        buttons=[
            {"id": "sub_view", "title": "View All"},
            {"id": "sub_mark_paid", "title": "Mark Paid"},
            {"id": "sub_add", "title": "Add New"},
        ]
    )


async def send_goals_menu(to: str):
    await send_interactive_buttons(
        to=to,
        body_text=(
            "Goals\n\n"
            "Choose one option below.\n\n"
            "Type *menu* anytime to see the main options."
        ),
        buttons=[
            {"id": "goal_view", "title": "View Goals"},
            {"id": "goal_add", "title": "Add Goal"},
            {"id": "goal_add_funds", "title": "Add Savings"},
        ]
    )


async def send_help_message(to: str):
    await send_text_message(
        to=to,
        text=(
            "*Help*\n\n"
            "Use the menu or type simple commands:\n"
            "- *expense* to log an expense\n"
            "- *income* to log income\n"
            "- *transactions* to open transaction options\n"
            "- *subscriptions* to manage recurring bills\n"
            "- *goals* to manage savings goals\n"
            "- *reports* to view metrics\n"
            "- *accounts* to view balances\n"
            "- *menu* or *home* to return anytime\n"
            "- *stop* or *cancel* to end the current flow\n\n"
            "Small spelling mistakes are okay for common commands."
        )
    )


async def send_goals_summary(to: str):
    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    goals = await get_goals(user["id"])
    if not goals:
        await send_interactive_buttons(
            to=to,
            body_text="🎯 No goals yet.\n\nCreate one to start tracking your savings targets.",
            buttons=[
                {"id": "goal_add", "title": "Add Goal"},
                {"id": "nav_home", "title": "Main Menu"},
            ]
        )
        return

    lines = [f"🎯 *Your Goals ({len(goals)})*\n"]
    for index, goal in enumerate(goals[:8], 1):
        current, pct = _goal_progress(goal)
        target = float(goal["target_amount"] or 0)
        remaining = max(target - current, 0)
        priority = (goal["priority"] or "medium").title()
        lines.append(
            f"{index}. {_goal_icon(goal)} {goal['name']}\n"
            f"   ₹{current:,.0f} / ₹{target:,.0f}  ·  {pct}%  ·  {priority}\n"
            f"   Remaining: ₹{remaining:,.0f}"
        )

    await send_interactive_buttons(
        to=to,
        body_text="\n".join(lines),
        buttons=[
            {"id": "goal_add_funds", "title": "Add Savings"},
            {"id": "goal_add", "title": "Add Goal"},
            {"id": "nav_home", "title": "Main Menu"},
        ]
    )


async def send_goal_picker(to: str):
    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    goals = await get_goals(user["id"])
    if not goals:
        await send_interactive_buttons(
            to=to,
            body_text="🎯 No goals found yet.\n\nCreate your first goal first.",
            buttons=[
                {"id": "goal_add", "title": "Add Goal"},
                {"id": "nav_home", "title": "Main Menu"},
            ]
        )
        return

    rows = []
    for goal in goals[:10]:
        current, pct = _goal_progress(goal)
        target = float(goal["target_amount"] or 0)
        rows.append({
            "id": f"goal_pick_{goal['id']}",
            "title": f"{_goal_icon(goal)} {goal['name']}"[:24],
            "description": f"₹{current:,.0f} / ₹{target:,.0f} · {pct}% saved"[:72],
        })

    await set_session(to, "goal_funding_picker", {})
    await send_list_message(
        to=to,
        body_text=(
            "Add Savings to Goal\n\n"
            "Which goal do you want to update?\n\n"
            "Type *stop* to cancel."
        ),
        button_label="Select Goal",
        sections=[{"title": "Goals", "rows": rows}]
    )


async def send_goal_priority_picker(to: str):
    await send_interactive_buttons(
        to=to,
        body_text="What priority should this goal have?\n\nType *stop* to cancel.",
        buttons=[
            {"id": "goal_priority_high", "title": "High"},
            {"id": "goal_priority_medium", "title": "Medium"},
            {"id": "goal_priority_low", "title": "Low"},
        ]
    )


async def show_goal_confirmation(to: str, parsed: dict):
    await set_session(to, "goal_confirm", parsed)
    await send_interactive_buttons(
        to=to,
        body_text=(
            "🎯 *Confirm Goal?*\n\n"
            f"Name: {parsed.get('name')}\n"
            f"Target: ₹{parsed.get('target_amount', 0):,.0f}\n"
            f"Starting amount: ₹{parsed.get('current_amount', 0):,.0f}\n"
            f"Priority: {(parsed.get('priority') or 'medium').title()}"
        ),
        buttons=[
            {"id": "goal_confirm", "title": "Confirm"},
            {"id": "goal_cancel", "title": "Cancel"},
            {"id": "nav_home", "title": "Main Menu"},
        ]
    )


async def show_goal_funding_confirmation(to: str, parsed: dict):
    await set_session(to, "goal_fund_confirm", parsed)
    new_total = float(parsed.get("current_amount", 0)) + float(parsed.get("add_amount", 0))
    target = float(parsed.get("target_amount", 0))
    pct = int((new_total / target) * 100) if target > 0 else 0
    await send_interactive_buttons(
        to=to,
        body_text=(
            "💰 *Add Savings to Goal?*\n\n"
            f"Goal: {parsed.get('goal_name')}\n"
            f"Add now: ₹{parsed.get('add_amount', 0):,.0f}\n"
            f"New total: ₹{new_total:,.0f} / ₹{target:,.0f}\n"
            f"Progress: {pct}%"
        ),
        buttons=[
            {"id": "goal_fund_confirm_btn", "title": "Confirm"},
            {"id": "goal_fund_cancel", "title": "Cancel"},
            {"id": "nav_home", "title": "Main Menu"},
        ]
    )


async def confirm_goal(to: str, parsed: dict):
    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    priority = parsed.get("priority") or "medium"
    theme_map = {"high": "ruby", "medium": "emerald", "low": "sky"}
    icon_map = {"high": "🚀", "medium": "🎯", "low": "🌱"}

    await create_goal(
        user_id=user["id"],
        name=parsed.get("name"),
        target_amount=parsed.get("target_amount", 0),
        current_amount=parsed.get("current_amount", 0),
        icon=icon_map.get(priority, "🎯"),
        theme=theme_map.get(priority, "emerald"),
        priority=priority,
    )

    await reset_session(to)
    await send_interactive_buttons(
        to=to,
        body_text=(
            f"✅ *{parsed.get('name')} created!*\n\n"
            f"Target: ₹{parsed.get('target_amount', 0):,.0f}\n"
            f"Saved so far: ₹{parsed.get('current_amount', 0):,.0f}"
        ),
        buttons=[
            {"id": "goal_view", "title": "View Goals"},
            {"id": "goal_add_funds", "title": "Add Savings"},
            {"id": "nav_home", "title": "Main Menu"},
        ]
    )


async def confirm_goal_funding(to: str, parsed: dict):
    goal = await get_goal_by_id(parsed.get("goal_id"))
    if not goal:
        await reset_session(to)
        await send_text_message(to=to, text="⚠️ Goal not found. Please try again.")
        await send_goals_menu(to)
        return

    new_total = float(goal["current_amount"] or 0) + float(parsed.get("add_amount", 0))
    await update_goal_current_amount(goal["id"], new_total)

    target = float(goal["target_amount"] or 0)
    pct = int((new_total / target) * 100) if target > 0 else 0

    await reset_session(to)
    await send_interactive_buttons(
        to=to,
        body_text=(
            f"✅ Added ₹{parsed.get('add_amount', 0):,.0f} to *{goal['name']}*.\n\n"
            f"Now saved: ₹{new_total:,.0f} / ₹{target:,.0f}\n"
            f"Progress: {pct}%"
        ),
        buttons=[
            {"id": "goal_view", "title": "View Goals"},
            {"id": "goal_add_funds", "title": "Add More"},
            {"id": "nav_home", "title": "Main Menu"},
        ]
    )


async def send_main_menu(to: str, greeting: bool = False):
    user = await get_user_by_phone(to)
    if not user:
        await send_not_registered(to)
        return

    user_id = user["id"]
    name = user["name"] or "there"
    month_year = date.today().strftime("%Y-%m")
    available_balance = float(await database.fetch_val(
        """SELECT COALESCE(SUM(balance), 0)
           FROM accounts
           WHERE user_id = :uid
             AND is_active = TRUE
             AND account_type != 'credit_card'""",
        {"uid": user_id}
    ) or 0)
    credit_due = float(await database.fetch_val(
        """SELECT COALESCE(SUM(outstanding), 0)
           FROM accounts
           WHERE user_id = :uid
             AND is_active = TRUE
             AND account_type = 'credit_card'""",
        {"uid": user_id}
    ) or 0)
    net_balance = available_balance - credit_due
    spent = await get_monthly_expense_total(user_id, month_year)
    budget_total = await get_total_budget(user_id, month_year)
    budget_used = int((spent / budget_total) * 100) if budget_total > 0 else 0
    goal_count = int(await database.fetch_val(
        "SELECT COUNT(*) FROM goals WHERE user_id = :uid",
        {"uid": user_id}
    ) or 0)

    snapshot_lines = [
        f"Available balance: ₹{available_balance:,.0f}",
        f"Credit due: ₹{credit_due:,.0f}",
        f"Net balance: ₹{net_balance:,.0f}",
    ]
    if budget_total > 0:
        snapshot_lines.append(f"Budget used: {budget_used}%")
    else:
        snapshot_lines.append("Budget used: no budget set")

    snapshot_lines.append(f"Goals: {goal_count} active" if goal_count else "Goals: none yet")

    today = date.today()
    due_subs = await legacy.get_due_subscriptions(user_id)
    if due_subs:
        next_sub = due_subs[0]
        label = legacy.due_label(next_sub["billing_day"], today)
        snapshot_lines.append(
            f"Next subscription: {next_sub['service_name']} ₹{float(next_sub['amount']):,.0f} ({label})"
        )
    else:
        snapshot_lines.append("Next subscription: none due soon")

    header = f"Hello {name}." if greeting else "Main Menu"

    await send_interactive_buttons(
        to=to,
        body_text=(
            f"{header}\n\n"
            f"{chr(10).join(snapshot_lines)}\n\n"
            "Choose what you want to manage.\n\n"
            "Quick commands: *expense*, *income*, *goals*\n"
            "Type *help* for all commands.\n"
            "Type *stop* to cancel any flow."
        ),
        buttons=[
            {"id": "menu_transactions", "title": "Transactions"},
            {"id": "menu_subscriptions", "title": "Subscriptions"},
            {"id": "menu_goals", "title": "Goals"},
        ]
    )
    await reset_session(to)


async def handle_button_click(to: str, button_id: str, session):
    if button_id == "menu_transactions":
        await send_transactions_menu(to)
        return

    if button_id == "menu_subscriptions":
        await send_subscriptions_menu(to)
        return

    if button_id == "menu_reports":
        await send_reports_menu(to)
        return

    if button_id == "menu_goals":
        await send_goals_menu(to)
        return

    if button_id == "menu_help":
        await reset_session(to)
        await send_help_message(to)
        await send_main_menu(to)
        return

    if button_id == "goal_add":
        await set_session(to, "goal_add_name", {})
        await send_text_message(
            to=to,
            text=(
                "Add Goal\n\n"
                "What should we call this goal?\n"
                "Examples: _Emergency Fund, Europe Trip, New Laptop_\n\n"
                "Type *stop* to cancel."
            )
        )
        return

    if button_id == "goal_view":
        await reset_session(to)
        await send_goals_summary(to)
        return

    if button_id == "goal_add_funds":
        await send_goal_picker(to)
        return

    if button_id == "goal_confirm":
        if not _session_has(session, "name", "target_amount"):
            await _expired_session_reply(to)
            return
        await confirm_goal(to, _get_parsed(session))
        return

    if button_id == "goal_cancel":
        await reset_session(to)
        await send_text_message(to=to, text="❌ Goal creation cancelled.")
        await send_goals_menu(to)
        return

    if button_id == "goal_fund_confirm_btn":
        if not _session_has(session, "goal_id", "add_amount"):
            await _expired_session_reply(to)
            return
        await confirm_goal_funding(to, _get_parsed(session))
        return

    if button_id == "goal_fund_cancel":
        await reset_session(to)
        await send_text_message(to=to, text="❌ Goal update cancelled.")
        await send_goals_menu(to)
        return

    if button_id.startswith("goal_priority_") and session and session.get("session_status") == "goal_add_priority":
        parsed = _get_parsed(session)
        parsed["priority"] = button_id.replace("goal_priority_", "")
        await show_goal_confirmation(to, parsed)
        return

    await legacy.handle_button_click(to, button_id, session)


async def handle_list_selection(to: str, row_id: str, session):
    status = session["session_status"] if session else "idle"

    if row_id in {"menu_transactions", "menu_expense", "menu_income", "menu_subscriptions", "menu_reports", "menu_goals", "menu_help"}:
        await handle_button_click(to, row_id, session)
        return

    if row_id in {"sum_thismonth", "sum_balances", "sum_recent", "sum_category"}:
        await legacy.handle_button_click(to, row_id, session)
        return

    if row_id in {"sub_view", "sub_mark_paid", "sub_add"}:
        await legacy.handle_button_click(to, row_id, session)
        return

    if row_id == "goal_view":
        await reset_session(to)
        await send_goals_summary(to)
        return

    if row_id == "goal_add":
        await handle_button_click(to, "goal_add", session)
        return

    if row_id == "goal_add_funds":
        await handle_button_click(to, "goal_add_funds", session)
        return

    if row_id.startswith("goal_priority_") and status == "goal_add_priority":
        parsed = _get_parsed(session)
        parsed["priority"] = row_id.replace("goal_priority_", "")
        await show_goal_confirmation(to, parsed)
        return

    if row_id.startswith("goal_pick_") and status == "goal_funding_picker":
        goal_id = int(row_id.replace("goal_pick_", ""))
        goal = await get_goal_by_id(goal_id)
        if not goal:
            await send_goals_menu(to)
            return
        parsed = {
            "goal_id": goal_id,
            "goal_name": goal["name"],
            "current_amount": float(goal["current_amount"] or 0),
            "target_amount": float(goal["target_amount"] or 0),
        }
        await set_session(to, "goal_add_amount", parsed)
        await send_text_message(
            to=to,
            text=(
                f"{goal['name']}\n\n"
                f"How much do you want to add now?\n"
                f"Saved so far: ₹{float(goal['current_amount'] or 0):,.0f}\n\n"
                "Type *stop* to cancel."
            )
        )
        return

    await legacy.handle_list_selection(to, row_id, session)


async def handle_text_message(to: str, text: str, session):
    status = session["session_status"] if session else "idle"
    global_command = _resolve_command(text, {"start", "menu", "help", "stop"})
    idle_command = _resolve_command(
        text,
        {"expense", "income", "transactions", "subscriptions", "goals", "reports", "accounts"},
    )

    if global_command == "start":
        await send_main_menu(to, greeting=True)
        return

    if global_command == "menu":
        await send_main_menu(to)
        return

    if global_command == "stop":
        await reset_session(to)
        await send_text_message(to=to, text="✅ Current flow cancelled.")
        await send_main_menu(to)
        return

    if global_command == "help":
        await reset_session(to)
        await send_help_message(to)
        await send_main_menu(to)
        return

    if status == "idle":
        if idle_command == "expense":
            await legacy.handle_button_click(to, "menu_expense", session)
            return
        if idle_command == "income":
            await legacy.handle_button_click(to, "menu_income", session)
            return
        if idle_command == "transactions":
            await send_transactions_menu(to)
            return
        if idle_command == "subscriptions":
            await send_subscriptions_menu(to)
            return
        if idle_command == "goals":
            await send_goals_menu(to)
            return
        if idle_command == "reports":
            await send_reports_menu(to)
            return
        if idle_command == "accounts":
            await legacy.send_balance_summary(to)
            return

    if status == "goal_add_name":
        name = text.strip()
        if len(name) < 2:
            await send_text_message(to=to, text="⚠️ Please enter a slightly longer goal name.")
            return
        await set_session(to, "goal_add_target", {"name": name})
        await send_text_message(
            to=to,
            text=(
                f"{name}\n\n"
                "What is the target amount?\n"
                "Example: *50000*\n\n"
                "Type *stop* to cancel."
            )
        )
        return

    if status == "goal_add_target":
        try:
            amount = float(text.replace(",", "").replace("₹", "").strip())
            if amount <= 0:
                raise ValueError
            parsed = _get_parsed(session)
            parsed["target_amount"] = amount
            await set_session(to, "goal_add_current", parsed)
            await send_text_message(
                to=to,
                text=(
                    "How much have you already saved for this goal?\n"
                    "Reply with *0* if you're just starting.\n\n"
                    "Type *stop* to cancel."
                )
            )
        except ValueError:
            await send_text_message(to=to, text="⚠️ Please enter a valid target amount.\nExample: *50000*")
        return

    if status == "goal_add_current":
        try:
            amount = float(text.replace(",", "").replace("₹", "").strip())
            if amount < 0:
                raise ValueError
            parsed = _get_parsed(session)
            parsed["current_amount"] = amount
            await set_session(to, "goal_add_priority", parsed)
            await send_goal_priority_picker(to)
        except ValueError:
            await send_text_message(to=to, text="⚠️ Please enter a valid amount.\nExample: *0* or *12000*")
        return

    if status == "goal_add_amount":
        try:
            amount = float(text.replace(",", "").replace("₹", "").strip())
            if amount <= 0:
                raise ValueError
            parsed = _get_parsed(session)
            parsed["add_amount"] = amount
            await show_goal_funding_confirmation(to, parsed)
        except ValueError:
            await send_text_message(to=to, text="⚠️ Please enter a valid amount to add.\nExample: *2500*")
        return

    if status == "idle":
        await send_text_message(
            to=to,
            text=(
                "I didn't catch that.\n\n"
                "Use the menu below or type one of these commands:\n"
                "*expense*, *income*, *goals*, *subscriptions*, *menu*, *help*\n\n"
                "Type *stop* to cancel any flow."
            )
        )
        await send_main_menu(to)
        return

    await legacy.handle_text_message(to, text, session)


legacy.send_main_menu = send_main_menu
