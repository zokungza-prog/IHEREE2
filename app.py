from __future__ import annotations

import csv
import io
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "finance.db"))

INCOME_CATEGORIES = ["Salary", "Side Job", "Sales", "Refund", "Other"]
EXPENSE_CATEGORIES = ["Food", "Travel", "Bills", "Shopping", "Health", "Family", "Other"]
ALL_CATEGORIES = INCOME_CATEGORIES + [item for item in EXPENSE_CATEGORIES if item not in INCOME_CATEGORIES]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: object | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = sqlite3.connect(DATABASE_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            amount REAL NOT NULL,
            transaction_type TEXT NOT NULL CHECK(transaction_type IN ('income', 'expense')),
            category TEXT NOT NULL,
            note TEXT,
            transaction_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            budget_month TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(budget_month, category)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS recurring_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            amount REAL NOT NULL,
            transaction_type TEXT NOT NULL CHECK(transaction_type IN ('income', 'expense')),
            category TEXT NOT NULL,
            note TEXT,
            day_of_month INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_applied_month TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    db.commit()
    db.close()


init_db()


def normalize_month(month_value: str | None) -> str:
    if not month_value:
        return datetime.today().strftime("%Y-%m")
    try:
        datetime.strptime(month_value, "%Y-%m")
        return month_value
    except ValueError:
        return datetime.today().strftime("%Y-%m")


def month_bounds(month_value: str) -> tuple[str, str]:
    start = datetime.strptime(month_value + "-01", "%Y-%m-%d")
    if start.month == 12:
        end = datetime(start.year + 1, 1, 1)
    else:
        end = datetime(start.year, start.month + 1, 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def next_month_key(month_value: str) -> str:
    current = datetime.strptime(month_value + "-01", "%Y-%m-%d")
    if current.month == 12:
        next_value = datetime(current.year + 1, 1, 1)
    else:
        next_value = datetime(current.year, current.month + 1, 1)
    return next_value.strftime("%Y-%m")


def days_in_month(month_value: str) -> int:
    start = datetime.strptime(month_value + "-01", "%Y-%m-%d")
    if start.month == 12:
        end = datetime(start.year + 1, 1, 1)
    else:
        end = datetime(start.year, start.month + 1, 1)
    return (end - start).days


def parse_filters(args: Any) -> dict[str, str]:
    return {
        "query": args.get("query", "").strip(),
        "type": args.get("type", "").strip(),
        "category": args.get("category", "").strip(),
    }


def build_where_clause(selected_month: str, filters: dict[str, str]) -> tuple[str, list[Any]]:
    start_date, end_date = month_bounds(selected_month)
    clauses = ["transaction_date >= ?", "transaction_date < ?"]
    params: list[Any] = [start_date, end_date]

    if filters["query"]:
        clauses.append("(title LIKE ? OR note LIKE ?)")
        like_value = f"%{filters['query']}%"
        params.extend([like_value, like_value])

    if filters["type"] in {"income", "expense"}:
        clauses.append("transaction_type = ?")
        params.append(filters["type"])

    if filters["category"] and filters["category"] in ALL_CATEGORIES:
        clauses.append("category = ?")
        params.append(filters["category"])

    return " WHERE " + " AND ".join(clauses), params


def format_currency(value: float) -> str:
    return f"{value:,.2f} THB"


def get_balance_base(db: sqlite3.Connection) -> float:
    row = db.execute(
        """
        SELECT value
        FROM app_meta
        WHERE key = 'balance_base'
        """
    ).fetchone()
    if row is None:
        return 0.0
    try:
        return float(row["value"])
    except ValueError:
        return 0.0


def set_balance_base(db: sqlite3.Connection, value: float) -> None:
    db.execute(
        """
        INSERT INTO app_meta (key, value)
        VALUES ('balance_base', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(value),),
    )


def build_ai_insights(
    *,
    selected_month: str,
    total_income: float,
    total_expense: float,
    balance: float,
    transaction_count: int,
    category_summary: list[sqlite3.Row],
    top_expense: sqlite3.Row | None,
    budget_progress: list[dict[str, Any]],
) -> list[dict[str, str]]:
    insights: list[dict[str, str]] = []

    if transaction_count == 0:
        return [
            {
                "title": "Not enough data yet",
                "body": f"No records for {selected_month}. Add a few entries so AI can learn your pattern.",
                "tone": "neutral",
            }
        ]

    saving_rate = 0.0 if total_income <= 0 else (balance / total_income) * 100
    if balance < 0:
        insights.append(
            {
                "title": "Spending is too high",
                "body": "Expenses are higher than income this month. Cut the top category first.",
                "tone": "warning",
            }
        )
    elif saving_rate >= 20:
        insights.append(
            {
                "title": "Healthy money flow",
                "body": f"You kept about {saving_rate:.1f}% of income this month. Good job.",
                "tone": "positive",
            }
        )
    else:
        insights.append(
            {
                "title": "Savings can improve",
                "body": f"You kept about {max(saving_rate, 0):.1f}% of income. Small cuts can help.",
                "tone": "neutral",
            }
        )

    over_budget = next((item for item in budget_progress if item["status"] == "over"), None)
    if over_budget is not None:
        insights.append(
            {
                "title": f"Budget alert: {over_budget['category']}",
                "body": f"You are over the {over_budget['category']} budget by {format_currency(over_budget['spent'] - over_budget['budget'])}.",
                "tone": "warning",
            }
        )

    if category_summary:
        top_category = category_summary[0]
        share = 0.0 if total_expense <= 0 else (float(top_category["total"]) / total_expense) * 100
        insights.append(
            {
                "title": f"Top category: {top_category['category']}",
                "body": f"It makes up about {share:.1f}% of total spending.",
                "tone": "neutral",
            }
        )

    if top_expense is not None and len(insights) < 4:
        insights.append(
            {
                "title": "Largest expense",
                "body": f"{top_expense['title']} cost {top_expense['amount']:,.2f} THB on {top_expense['transaction_date']}.",
                "tone": "neutral",
            }
        )

    return insights[:4]


def fetch_budget_progress(db: sqlite3.Connection, selected_month: str) -> list[dict[str, Any]]:
    budgets = db.execute(
        """
        SELECT category, amount
        FROM budgets
        WHERE budget_month = ?
        ORDER BY category ASC
        """,
        (selected_month,),
    ).fetchall()

    progress: list[dict[str, Any]] = []
    for budget in budgets:
        spent_row = db.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS spent
            FROM transactions
            WHERE transaction_type = 'expense'
              AND category = ?
              AND transaction_date >= ?
              AND transaction_date < ?
            """,
            (budget["category"], *month_bounds(selected_month)),
        ).fetchone()
        spent = float(spent_row["spent"])
        budget_amount = float(budget["amount"])
        usage = 0.0 if budget_amount <= 0 else (spent / budget_amount) * 100
        if spent > budget_amount:
            status = "over"
        elif usage >= 80:
            status = "near"
        else:
            status = "safe"
        progress.append(
            {
                "category": budget["category"],
                "budget": budget_amount,
                "spent": spent,
                "left": budget_amount - spent,
                "usage": usage,
                "status": status,
            }
        )
    return progress


def fetch_dashboard_data(selected_month: str, filters: dict[str, str]) -> dict[str, object]:
    start_date, end_date = month_bounds(selected_month)
    db = get_db()
    where_clause, params = build_where_clause(selected_month, filters)

    transactions = db.execute(
        f"""
        SELECT id, title, amount, transaction_type, category, note, transaction_date
        FROM transactions
        {where_clause}
        ORDER BY transaction_date DESC, id DESC
        """,
        params,
    ).fetchall()

    monthly_summary = db.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN transaction_type = 'income' THEN amount END), 0) AS total_income,
            COALESCE(SUM(CASE WHEN transaction_type = 'expense' THEN amount END), 0) AS total_expense
        FROM transactions
        WHERE transaction_date >= ? AND transaction_date < ?
        """,
        (start_date, end_date),
    ).fetchone()

    category_summary = db.execute(
        """
        SELECT category, COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE transaction_type = 'expense'
          AND transaction_date >= ?
          AND transaction_date < ?
        GROUP BY category
        ORDER BY total DESC, category ASC
        """,
        (start_date, end_date),
    ).fetchall()

    daily_summary = db.execute(
        """
        SELECT
            transaction_date,
            COALESCE(SUM(CASE WHEN transaction_type = 'income' THEN amount END), 0) AS income_total,
            COALESCE(SUM(CASE WHEN transaction_type = 'expense' THEN amount END), 0) AS expense_total
        FROM transactions
        WHERE transaction_date >= ? AND transaction_date < ?
        GROUP BY transaction_date
        ORDER BY transaction_date ASC
        """,
        (start_date, end_date),
    ).fetchall()

    transaction_count_row = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM transactions
        WHERE transaction_date >= ? AND transaction_date < ?
        """,
        (start_date, end_date),
    ).fetchone()

    top_expense = db.execute(
        """
        SELECT title, amount, category, transaction_date
        FROM transactions
        WHERE transaction_type = 'expense'
          AND transaction_date >= ?
          AND transaction_date < ?
        ORDER BY amount DESC, transaction_date DESC, id DESC
        LIMIT 1
        """,
        (start_date, end_date),
    ).fetchone()

    overall_summary = db.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN transaction_type = 'income' THEN amount END), 0) AS total_income,
            COALESCE(SUM(CASE WHEN transaction_type = 'expense' THEN amount END), 0) AS total_expense
        FROM transactions
        """
    ).fetchone()

    overall_count = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()

    recurring_items = db.execute(
        """
        SELECT id, title, amount, transaction_type, category, note, day_of_month, is_active, last_applied_month
        FROM recurring_items
        ORDER BY is_active DESC, day_of_month ASC, title ASC
        """
    ).fetchall()

    budget_progress = fetch_budget_progress(db, selected_month)

    total_income = float(monthly_summary["total_income"])
    total_expense = float(monthly_summary["total_expense"])
    overall_income = float(overall_summary["total_income"])
    overall_expense = float(overall_summary["total_expense"])
    balance_base = get_balance_base(db)

    chart_data = {
        "labels": [row["transaction_date"][8:10] for row in daily_summary],
        "income": [float(row["income_total"]) for row in daily_summary],
        "expense": [float(row["expense_total"]) for row in daily_summary],
        "categories": [
            {"label": row["category"], "value": float(row["total"])}
            for row in category_summary[:6]
        ],
    }

    return {
        "transactions": transactions,
        "total_income": total_income,
        "total_expense": total_expense,
        "balance": total_income - total_expense,
        "category_summary": category_summary,
        "ai_insights": build_ai_insights(
            selected_month=selected_month,
            total_income=total_income,
            total_expense=total_expense,
            balance=total_income - total_expense,
            transaction_count=int(transaction_count_row["count"]),
            category_summary=category_summary,
            top_expense=top_expense,
            budget_progress=budget_progress,
        ),
        "top_expense": top_expense,
        "transaction_count": int(transaction_count_row["count"]),
        "chart_data": chart_data,
        "overall_balance": overall_income - overall_expense,
        "display_overall_balance": balance_base + (overall_income - overall_expense),
        "carry_over_balance": balance_base,
        "balance_base": balance_base,
        "overall_entries": int(overall_count["count"]),
        "budget_progress": budget_progress,
        "recurring_items": recurring_items,
        "filters": filters,
    }


def validate_transaction_form(form: Any) -> tuple[bool, dict[str, Any]]:
    title = form.get("title", "").strip()
    amount_raw = form.get("amount", "").strip()
    transaction_type = form.get("transaction_type", "").strip()
    category = form.get("category", "").strip()
    note = form.get("note", "").strip()
    transaction_date = form.get("transaction_date", "").strip()

    try:
        amount = float(amount_raw)
    except ValueError:
        amount = -1.0

    try:
        datetime.strptime(transaction_date, "%Y-%m-%d")
    except ValueError:
        transaction_date = ""

    valid = (
        bool(title)
        and amount > 0
        and transaction_type in {"income", "expense"}
        and category in ALL_CATEGORIES
        and bool(transaction_date)
    )

    return valid, {
        "title": title,
        "amount": amount,
        "transaction_type": transaction_type,
        "category": category,
        "note": note,
        "transaction_date": transaction_date,
    }


def redirect_with_month(month_value: str) -> Any:
    return redirect(url_for("index", month=normalize_month(month_value)))


def render_page(active_page: str):
    selected_month = normalize_month(request.args.get("month"))
    filters = parse_filters(request.args)
    dashboard = fetch_dashboard_data(selected_month, filters)
    return render_template(
        "index.html",
        active_page=active_page,
        selected_month=selected_month,
        next_month=next_month_key(selected_month),
        default_date=datetime.today().strftime("%Y-%m-%d"),
        income_categories=INCOME_CATEGORIES,
        expense_categories=EXPENSE_CATEGORIES,
        all_categories=ALL_CATEGORIES,
        **dashboard,
    )


@app.route("/", methods=["GET"])
def index():
    return render_page("home")


@app.route("/add-entry", methods=["GET"])
def add_entry_page():
    return render_page("add")


@app.route("/assistant-page", methods=["GET"])
def assistant_page():
    return render_page("assistant")


@app.route("/history", methods=["GET"])
def history_page():
    return render_page("history")


@app.route("/planner", methods=["GET"])
def planner_page():
    return render_page("planner")


@app.route("/add", methods=["POST"])
def add_transaction():
    valid, payload = validate_transaction_form(request.form)
    if not valid:
        flash("Please complete the form and enter a valid amount.", "error")
        return redirect_with_month(request.form.get("redirect_month"))

    db = get_db()
    db.execute(
        """
        INSERT INTO transactions (
            title, amount, transaction_type, category, note, transaction_date, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["title"],
            payload["amount"],
            payload["transaction_type"],
            payload["category"],
            payload["note"],
            payload["transaction_date"],
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()

    flash("Entry saved.", "success")
    return redirect_with_month(payload["transaction_date"][:7])


@app.route("/edit/<int:transaction_id>", methods=["POST"])
def edit_transaction(transaction_id: int):
    valid, payload = validate_transaction_form(request.form)
    if not valid:
        flash("Could not update entry. Check the fields and try again.", "error")
        return redirect_with_month(request.form.get("redirect_month"))

    db = get_db()
    db.execute(
        """
        UPDATE transactions
        SET title = ?, amount = ?, transaction_type = ?, category = ?, note = ?, transaction_date = ?
        WHERE id = ?
        """,
        (
            payload["title"],
            payload["amount"],
            payload["transaction_type"],
            payload["category"],
            payload["note"],
            payload["transaction_date"],
            transaction_id,
        ),
    )
    db.commit()

    flash("Entry updated.", "success")
    return redirect_with_month(payload["transaction_date"][:7])


@app.route("/delete/<int:transaction_id>", methods=["POST"])
def delete_transaction(transaction_id: int):
    db = get_db()
    db.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
    db.commit()
    flash("Entry deleted.", "success")
    return redirect_with_month(request.form.get("redirect_month"))


@app.route("/budgets", methods=["POST"])
def save_budget():
    selected_month = normalize_month(request.form.get("budget_month"))
    category = request.form.get("category", "").strip()
    amount_raw = request.form.get("amount", "").strip()

    try:
        amount = float(amount_raw)
    except ValueError:
        amount = -1.0

    if category not in EXPENSE_CATEGORIES or amount <= 0:
        flash("Enter a valid budget category and amount.", "error")
        return redirect_with_month(selected_month)

    db = get_db()
    db.execute(
        """
        INSERT INTO budgets (budget_month, category, amount, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(budget_month, category)
        DO UPDATE SET amount = excluded.amount
        """,
        (selected_month, category, amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    db.commit()
    flash("Budget saved.", "success")
    return redirect_with_month(selected_month)


@app.route("/recurring", methods=["POST"])
def save_recurring_item():
    title = request.form.get("title", "").strip()
    category = request.form.get("category", "").strip()
    transaction_type = request.form.get("transaction_type", "").strip()
    note = request.form.get("note", "").strip()
    amount_raw = request.form.get("amount", "").strip()
    day_raw = request.form.get("day_of_month", "").strip()

    try:
        amount = float(amount_raw)
        day_of_month = int(day_raw)
    except ValueError:
        amount = -1.0
        day_of_month = 0

    if (
        not title
        or amount <= 0
        or category not in ALL_CATEGORIES
        or transaction_type not in {"income", "expense"}
        or not 1 <= day_of_month <= 28
    ):
        flash("Recurring item needs a title, valid amount, type, category, and day 1-28.", "error")
        return redirect_with_month(request.form.get("redirect_month"))

    db = get_db()
    db.execute(
        """
        INSERT INTO recurring_items (
            title, amount, transaction_type, category, note, day_of_month, is_active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            title,
            amount,
            transaction_type,
            category,
            note,
            day_of_month,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    db.commit()

    flash("Recurring item added.", "success")
    return redirect_with_month(request.form.get("redirect_month"))


@app.route("/recurring/<int:item_id>/toggle", methods=["POST"])
def toggle_recurring_item(item_id: int):
    db = get_db()
    db.execute(
        """
        UPDATE recurring_items
        SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
        WHERE id = ?
        """,
        (item_id,),
    )
    db.commit()
    flash("Recurring item updated.", "success")
    return redirect_with_month(request.form.get("redirect_month"))


@app.route("/recurring/apply", methods=["POST"])
def apply_recurring_items():
    selected_month = normalize_month(request.form.get("redirect_month"))
    db = get_db()
    items = db.execute(
        """
        SELECT id, title, amount, transaction_type, category, note, day_of_month
        FROM recurring_items
        WHERE is_active = 1
          AND (last_applied_month IS NULL OR last_applied_month != ?)
        ORDER BY day_of_month ASC, id ASC
        """,
        (selected_month,),
    ).fetchall()

    inserted = 0
    days_total = days_in_month(selected_month)
    for item in items:
        day_value = min(int(item["day_of_month"]), days_total)
        transaction_date = f"{selected_month}-{day_value:02d}"
        exists = db.execute(
            """
            SELECT id
            FROM transactions
            WHERE title = ?
              AND amount = ?
              AND transaction_type = ?
              AND category = ?
              AND transaction_date = ?
            LIMIT 1
            """,
            (
                item["title"],
                item["amount"],
                item["transaction_type"],
                item["category"],
                transaction_date,
            ),
        ).fetchone()
        if exists is not None:
            db.execute(
                "UPDATE recurring_items SET last_applied_month = ? WHERE id = ?",
                (selected_month, item["id"]),
            )
            continue

        db.execute(
            """
            INSERT INTO transactions (
                title, amount, transaction_type, category, note, transaction_date, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["title"],
                item["amount"],
                item["transaction_type"],
                item["category"],
                item["note"],
                transaction_date,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        db.execute(
            "UPDATE recurring_items SET last_applied_month = ? WHERE id = ?",
            (selected_month, item["id"]),
        )
        inserted += 1

    db.commit()
    flash(f"Applied {inserted} recurring item(s).", "success")
    return redirect_with_month(selected_month)


@app.route("/export.csv", methods=["GET"])
def export_csv():
    selected_month = normalize_month(request.args.get("month"))
    filters = parse_filters(request.args)
    where_clause, params = build_where_clause(selected_month, filters)
    db = get_db()
    rows = db.execute(
        f"""
        SELECT transaction_date, title, transaction_type, category, amount, note
        FROM transactions
        {where_clause}
        ORDER BY transaction_date DESC, id DESC
        """,
        params,
    ).fetchall()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Date", "Title", "Type", "Category", "Amount", "Note"])
    for row in rows:
        writer.writerow(
            [
                row["transaction_date"],
                row["title"],
                row["transaction_type"],
                row["category"],
                f"{float(row['amount']):.2f}",
                row["note"] or "",
            ]
        )

    response = make_response(buffer.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment; filename=money-flow-{selected_month}.csv"
    return response


def build_ai_answer(question: str, selected_month: str) -> str:
    db = get_db()
    lower = question.lower()
    start_date, end_date = month_bounds(selected_month)

    if not question.strip():
        return "Ask about spending, savings, top categories, budgets, or trends."

    if "most" in lower and ("spend" in lower or "category" in lower):
        row = db.execute(
            """
            SELECT category, COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE transaction_type = 'expense'
              AND transaction_date >= ?
              AND transaction_date < ?
            GROUP BY category
            ORDER BY total DESC
            LIMIT 1
            """,
            (start_date, end_date),
        ).fetchone()
        if row is None:
            return "No expense data yet for this month."
        return f"You spent the most on {row['category']} in this cycle at {format_currency(float(row['total']))}."

    if "save" in lower or "balance" in lower:
        row = db.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN transaction_type = 'income' THEN amount END), 0) AS total_income,
                COALESCE(SUM(CASE WHEN transaction_type = 'expense' THEN amount END), 0) AS total_expense
            FROM transactions
            WHERE transaction_date >= ? AND transaction_date < ?
            """,
            (start_date, end_date),
        ).fetchone()
        balance = float(row["total_income"]) - float(row["total_expense"])
        return f"Your net result for {selected_month} is {format_currency(balance)}."

    if "budget" in lower:
        budgets = fetch_budget_progress(db, selected_month)
        if not budgets:
            return "You have no budgets set for this month yet."
        over = [item for item in budgets if item["status"] == "over"]
        near = [item for item in budgets if item["status"] == "near"]
        if over:
            item = over[0]
            return f"{item['category']} is over budget by {format_currency(item['spent'] - item['budget'])}."
        if near:
            item = near[0]
            return f"{item['category']} is at {item['usage']:.0f}% of budget."
        return "All current budgets are in a safe range."

    if "recurring" in lower:
        row = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM recurring_items
            WHERE is_active = 1
            """
        ).fetchone()
        return f"You have {int(row['count'])} active recurring item(s)."

    sample_questions = [
        "What did I spend most on this month?",
        "How much did I save this month?",
        "Am I over budget anywhere?",
        "How many recurring items are active?",
    ]
    return "Try one of these: " + " | ".join(sample_questions)


@app.route("/assistant", methods=["POST"])
def assistant():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    selected_month = normalize_month(payload.get("month"))
    answer = build_ai_answer(question, selected_month)
    return jsonify({"answer": answer})


@app.route("/reset-history", methods=["POST"])
def reset_history():
    selected_month = normalize_month(request.form.get("redirect_month"))
    db = get_db()

    totals = db.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN transaction_type = 'income' THEN amount END), 0) AS total_income,
            COALESCE(SUM(CASE WHEN transaction_type = 'expense' THEN amount END), 0) AS total_expense
        FROM transactions
        """
    ).fetchone()

    current_net = float(totals["total_income"]) - float(totals["total_expense"])
    next_base = get_balance_base(db) + current_net
    set_balance_base(db, next_base)
    next_month = next_month_key(selected_month)
    current_budgets = db.execute(
        """
        SELECT category, amount
        FROM budgets
        WHERE budget_month = ?
        """,
        (selected_month,),
    ).fetchall()
    db.execute("DELETE FROM transactions")
    db.execute("DELETE FROM budgets")
    db.execute("UPDATE recurring_items SET last_applied_month = NULL")
    for budget in current_budgets:
        db.execute(
            """
            INSERT INTO budgets (budget_month, category, amount, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(budget_month, category)
            DO UPDATE SET amount = excluded.amount
            """,
            (
                next_month,
                budget["category"],
                budget["amount"],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    db.commit()

    flash(f"Month closed. Overall balance kept and planner moved to {next_month}.", "success")
    return redirect(url_for("planner_page", month=next_month))


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=debug_mode)
