import json
import sqlite3
import hashlib
from datetime import datetime, date
import calendar
from pathlib import Path

import pandas as pd
import streamlit as st
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "budgetos.db"
CONFIG_DIR = APP_DIR / "config"

PREFS_PATH = CONFIG_DIR / "preferences.json"
EXPENSE_CATS_PATH = CONFIG_DIR / "expense_categories.json"
INCOME_CATS_PATH = CONFIG_DIR / "income_categories.json"
ACCOUNTS_PATH = CONFIG_DIR / "accounts.json"

MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"]
MONTH_COLS = [f"{m:02d}" for m in range(1,13)]

# ---------------------------
# Storage
# ---------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT NOT NULL,
                txn_date TEXT NOT NULL,
                posted_date TEXT,
                description TEXT NOT NULL,
                amount REAL NOT NULL,             -- signed: negative=expense, positive=income
                kind TEXT NOT NULL,               -- 'expense' or 'income'
                account TEXT,
                category TEXT,
                excluded INTEGER DEFAULT 0,        -- exclude from totals (payments/transfers)
                notes TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_external_id ON ledger(external_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_date ON ledger(txn_date)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS goals (
                year INTEGER NOT NULL,
                category TEXT NOT NULL,
                monthly_goal REAL NOT NULL,
                PRIMARY KEY (year, category)
            )
            """
        )

        # Lock months to prevent accidental changes (statement close).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS locked_months (
                year INTEGER NOT NULL,
                month TEXT NOT NULL,   -- '01'..'12'
                PRIMARY KEY (year, month)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

def get_state(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return default if row is None else row["value"]

def set_state(key, value):
    with db() as conn:
        conn.execute(
            "INSERT INTO app_state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

# ---------------------------
# Config load/save
# ---------------------------
def load_json_file(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        return default
    return default

def save_json_file(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")

def load_prefs():
    raw = get_state("prefs_json", None)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return load_json_file(PREFS_PATH, {})

def save_prefs(prefs: dict):
    set_state("prefs_json", json.dumps(prefs))
    try:
        save_json_file(PREFS_PATH, prefs)
    except Exception:
        pass

def load_expense_categories():
    raw = get_state("expense_categories_json", None)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return load_json_file(EXPENSE_CATS_PATH, [])

def save_expense_categories(cats: list):
    set_state("expense_categories_json", json.dumps(cats))
    try:
        save_json_file(EXPENSE_CATS_PATH, cats)
    except Exception:
        pass

def load_income_categories():
    raw = get_state("income_categories_json", None)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return load_json_file(INCOME_CATS_PATH, [])

def save_income_categories(cats: list):
    set_state("income_categories_json", json.dumps(cats))
    try:
        save_json_file(INCOME_CATS_PATH, cats)
    except Exception:
        pass

def load_accounts():
    return load_json_file(ACCOUNTS_PATH, ["Chase","Venmo","BOA","Cash","Other"])

# ---------------------------
# Helpers
# ---------------------------
def parse_date(val):
    if pd.isna(val):
        return None
    s = str(val).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return pd.to_datetime(s).date().isoformat()
    except Exception:
        return None

def parse_amount(val):
    if pd.isna(val):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace("$","").replace(",","").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None

def base_hash(signature: str) -> str:
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]

def make_external_ids(df: pd.DataFrame) -> pd.Series:
    # Stable IDs while preserving duplicates.
    sig = df.apply(
        lambda r: f"{r['txn_date']}|{r.get('posted_date','')}|{r['amount']:.2f}|{r['description']}|{r.get('account','')}|{r['kind']}",
        axis=1,
    )
    h = sig.apply(base_hash)
    df2 = df.copy()
    df2["_h"] = h
    df2["_i"] = df2.groupby("_h").cumcount() + 1
    return df2.apply(lambda r: f"{r['_h']}-{int(r['_i'])}", axis=1)

def signed_to_spend(amount: float) -> float:
    # signed: negative expense -> positive spend; positive refund -> negative spend
    return -float(amount)

def currency(x: float) -> str:
    return f"${x:,.2f}"

def month_of(txn_date_iso: str) -> str:
    return str(txn_date_iso)[5:7]

def month_name(month: str) -> str:
    return MONTHS[int(month)-1]

def normalize_description(desc: str) -> str:
    # Very light normalization to group common variants while keeping your exact-string mapping model.
    # - Uppercase
    # - Remove multiple spaces
    # - Remove trailing card noise like "*XXXX" or "#1234" patterns (best-effort)
    if not desc:
        return ""
    s = " ".join(str(desc).upper().split())
    # Remove common trailing noise patterns
    s = s.replace("  ", " ")
    # Strip long digit runs (but keep short ones)
    s = "".join(ch for ch in s if not (ch.isdigit() and ch != ""))
    s = " ".join(s.split())
    return s

# ---------------------------
# Month locks (statement close)
# ---------------------------
def get_locked_months(year: int) -> set[str]:
    with db() as conn:
        rows = conn.execute("SELECT month FROM locked_months WHERE year=?", (int(year),)).fetchall()
    return set(r["month"] for r in rows)

def save_locked_months(year: int, months: set[str]):
    months = set(months)
    with db() as conn:
        conn.execute("DELETE FROM locked_months WHERE year=?", (int(year),))
        for m in sorted(months):
            conn.execute("INSERT INTO locked_months(year, month) VALUES(?,?)", (int(year), str(m)))

# ---------------------------
# Data fetch / write
# ---------------------------
def fetch_year(year: int) -> pd.DataFrame:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM ledger WHERE substr(txn_date,1,4)=? ORDER BY txn_date ASC, id ASC",
            (str(year),),
        ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["excluded"] = df["excluded"].fillna(0).astype(int)
    df["amount"] = df["amount"].astype(float)
    return df

def insert_rows(df: pd.DataFrame) -> tuple[int,int]:
    inserted = 0
    skipped = 0
    now = datetime.utcnow().isoformat(timespec="seconds")
    with db() as conn:
        for _, r in df.iterrows():
            try:
                conn.execute(
                    """
                    INSERT INTO ledger(external_id, txn_date, posted_date, description, amount, kind, account, category, excluded, notes, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        r["external_id"],
                        r["txn_date"],
                        r.get("posted_date"),
                        r["description"],
                        float(r["amount"]),
                        r["kind"],
                        r.get("account"),
                        r.get("category"),
                        int(r.get("excluded", 0)),
                        r.get("notes"),
                        now,
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
    return inserted, skipped

def update_category_for_description(description: str, category: str, year: int, locked_months: set[str]):
    # Apply only to unlocked months to respect statement close.
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, txn_date FROM ledger
            WHERE description=? AND kind='expense' AND (category IS NULL OR category='')
              AND substr(txn_date,1,4)=?
            """,
            (description, str(year)),
        ).fetchall()
        for r in rows:
            m = month_of(r["txn_date"])
            if m in locked_months:
                continue
            conn.execute("UPDATE ledger SET category=? WHERE id=?", (category, int(r["id"])))

def update_txn_by_id(txn_id: int, updates: dict):
    allowed = {"txn_date","description","amount","account","category","excluded","notes","posted_date","kind"}
    set_cols = []
    params = []
    for k, v in updates.items():
        if k not in allowed:
            continue
        set_cols.append(f"{k}=?")
        params.append(v)
    if not set_cols:
        return
    params.append(int(txn_id))
    with db() as conn:
        conn.execute(f"UPDATE ledger SET {', '.join(set_cols)} WHERE id=?", params)

# ---------------------------
# Import Chase CSV
# ---------------------------
def import_chase_csv(file, account_name="Chase", route_statement_credits_to_income: bool = True) -> pd.DataFrame:
    df = pd.read_csv(file)

    def pick_col(name: str):
        for c in df.columns:
            if c.strip().lower() == name.strip().lower():
                return c
        return None

    c_txn = pick_col("Transaction Date")
    c_post = pick_col("Post Date")
    c_desc = pick_col("Description")
    c_amt = pick_col("Amount")
    c_type = pick_col("Type")

    if not (c_txn and c_desc and c_amt):
        raise ValueError(f"CSV missing required columns. Found: {list(df.columns)}")

    out = []
    for _, r in df.iterrows():
        txn_date = parse_date(r[c_txn])
        posted = parse_date(r[c_post]) if c_post else None
        desc = str(r[c_desc]).strip()
        amt = parse_amount(r[c_amt])
        if not txn_date or amt is None or not desc:
            continue

        # Default: everything from the statement is treated as an expense transaction (signed).
        # Exclusions / special cases:
        # - Payments should be excluded (they are just moving money).
        # - Optional: route statement credits (Type=Adjustment, positive amount) to Income.
        kind = "expense"
        excluded = 0
        category = None

        t = str(r[c_type]).strip().lower() if c_type else ""
        if "payment" in t:
            excluded = 1
        elif route_statement_credits_to_income and ("adjustment" in t) and float(amt) > 0:
            kind = "income"
            category = "Credit Card Redemptions/Interest"

        out.append(
            {
                "txn_date": txn_date,
                "posted_date": posted,
                "description": desc,
                "amount": float(amt),
                "kind": kind,
                "account": account_name,
                "category": category,
                "excluded": excluded,
                "notes": None,
            }
        )

    df_out = pd.DataFrame(out)
    if df_out.empty:
        return df_out
    df_out["external_id"] = make_external_ids(df_out)
    return df_out

# ---------------------------
# Summaries (sheet math)
# ---------------------------
def compute_summary(df_year: pd.DataFrame, expense_cats: list, income_cats: list):
    # Expenses (kind=expense, excluded=0) -> spend = -amount
    exp = df_year[(df_year["kind"]=="expense") & (df_year["excluded"]==0)].copy()
    exp["spend"] = exp["amount"].apply(signed_to_spend)
    exp["month"] = exp["txn_date"].str.slice(5,7)

    exp_pivot = exp.pivot_table(index="category", columns="month", values="spend", aggfunc="sum", fill_value=0.0)
    for c in MONTH_COLS:
        if c not in exp_pivot.columns:
            exp_pivot[c] = 0.0
    exp_pivot = exp_pivot[MONTH_COLS]
    exp_pivot = exp_pivot.reindex(expense_cats).fillna(0.0)

    exp_pivot["Average Per Month"] = exp_pivot.sum(axis=1) / 12.0
    exp_pivot["YTD Totals"] = exp_pivot[MONTH_COLS].sum(axis=1)
    exp_pivot["Average Per Week"] = exp_pivot["YTD Totals"] / 52.0
    exp_pivot["Average Per Day"] = exp_pivot["YTD Totals"] / 364.0

    total_spent = float(exp_pivot["YTD Totals"].sum())
    total_row = pd.DataFrame(
        [
            {
                **{c: float(exp_pivot[c].sum()) for c in MONTH_COLS},
                "Average Per Month": total_spent / 12.0,
                "YTD Totals": total_spent,
                "Average Per Week": total_spent / 52.0,
                "Average Per Day": total_spent / 364.0,
            }
        ],
        index=["Total Spent"],
    )
    exp_table = pd.concat([exp_pivot, total_row], axis=0)

    # Income (kind=income, excluded=0)
    inc = df_year[(df_year["kind"]=="income") & (df_year["excluded"]==0)].copy()
    inc["month"] = inc["txn_date"].str.slice(5,7)

    inc_pivot = inc.pivot_table(index="category", columns="month", values="amount", aggfunc="sum", fill_value=0.0)
    for c in MONTH_COLS:
        if c not in inc_pivot.columns:
            inc_pivot[c] = 0.0
    inc_pivot = inc_pivot[MONTH_COLS]
    inc_pivot = inc_pivot.reindex(income_cats).fillna(0.0)

    inc_pivot["Average Per Month"] = inc_pivot.sum(axis=1) / 12.0
    inc_pivot["YTD Totals"] = inc_pivot[MONTH_COLS].sum(axis=1)
    inc_pivot["Average Per Week"] = inc_pivot["YTD Totals"] / 52.0
    inc_pivot["Average Per Day"] = inc_pivot["YTD Totals"] / 364.0

    total_income = float(inc_pivot["YTD Totals"].sum())
    total_income_row = pd.DataFrame(
        [
            {
                **{c: float(inc_pivot[c].sum()) for c in MONTH_COLS},
                "Average Per Month": total_income / 12.0,
                "YTD Totals": total_income,
                "Average Per Week": total_income / 52.0,
                "Average Per Day": total_income / 364.0,
            }
        ],
        index=["Total Net Income"],
    )
    inc_table = pd.concat([inc_pivot, total_income_row], axis=0)

    # Savings (monthly + YTD)
    spend_by_month = exp_pivot[MONTH_COLS].sum(axis=0)
    income_by_month = inc_pivot[MONTH_COLS].sum(axis=0)
    saved_by_month = income_by_month - spend_by_month

    # Monthly savings rate
    savings_rate_month = []
    for m in MONTH_COLS:
        inc_m = float(income_by_month.get(m, 0.0))
        sav_m = float(saved_by_month.get(m, 0.0))
        savings_rate_month.append((sav_m / inc_m) if inc_m else 0.0)

    total_saved = float(total_income - total_spent)
    savings_rate_ytd = (total_saved / total_income) if total_income else 0.0

    saved_row = pd.DataFrame(
        [
            {
                **{m: float(saved_by_month.get(m, 0.0)) for m in MONTH_COLS},
                "Average Per Month": total_saved / 12.0,
                "YTD Totals": total_saved,
                "Average Per Week": total_saved / 52.0,
                "Average Per Day": total_saved / 364.0,
            }
        ],
        index=["Total Saved"],
    )

    rate_row = pd.DataFrame(
        [
            {
                **{MONTH_COLS[i]: float(savings_rate_month[i]) for i in range(12)},
                "Average Per Month": None,
                "YTD Totals": float(savings_rate_ytd),
                "Average Per Week": None,
                "Average Per Day": None,
            }
        ],
        index=["Savings Rate"],
    )

    savings_table = pd.concat([saved_row, rate_row], axis=0)
    return exp_table, inc_table, savings_table, float(savings_rate_ytd)

# ---------------------------
# Excel export
# ---------------------------
def export_sheet_view_to_excel(year: int, expense_table: pd.DataFrame, income_table: pd.DataFrame, savings_table: pd.DataFrame) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = f"{year} Summary"

    ws.append([f"BudgetOS Sheet View - {year}"])
    ws.append([])

    ws.append(["EXPENSES"])
    df_e = expense_table.reset_index().rename(columns={"index":"Category"})
    for r in dataframe_to_rows(df_e, index=False, header=True):
        ws.append(r)
    ws.append([])

    ws.append(["INCOME"])
    df_i = income_table.reset_index().rename(columns={"index":"Category"})
    for r in dataframe_to_rows(df_i, index=False, header=True):
        ws.append(r)
    ws.append([])

    ws.append(["SAVINGS"])
    df_s = savings_table.reset_index().rename(columns={"index":"Metric"})
    for r in dataframe_to_rows(df_s, index=False, header=True):
        ws.append(r)

    from io import BytesIO
    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()

# ---------------------------
# UI helpers
# ---------------------------
def format_sheet(df: pd.DataFrame) -> pd.DataFrame:
    # For display: currency for numeric except Savings Rate row/columns.
    out = df.copy()

    def fmt_cell(val, row_name):
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return ""
        if row_name == "Savings Rate":
            try:
                return f"{float(val)*100:.2f}%"
            except Exception:
                return ""
        if isinstance(val, (int, float)):
            return currency(float(val))
        return str(val)

    # Apply formatting by rows
    formatted = pd.DataFrame(index=out.index, columns=out.columns)
    for idx in out.index:
        for col in out.columns:
            formatted.loc[idx, col] = fmt_cell(out.loc[idx, col], idx)
    return formatted

def month_range_selector(key: str, default=(MONTHS[0], MONTHS[-1])):
    return st.select_slider(
        "Month range",
        options=MONTHS,
        value=default,
        key=key,
    )

def months_between(start_month_name: str, end_month_name: str) -> list[str]:
    a = MONTHS.index(start_month_name) + 1
    b = MONTHS.index(end_month_name) + 1
    if a <= b:
        return [f"{m:02d}" for m in range(a, b+1)]
    # If user selects reversed, swap
    return [f"{m:02d}" for m in range(b, a+1)]

def clamp_date_to_month(d: date, year: int, month: int) -> date:
    # Ensure date is inside the given year/month.
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    if d < first:
        return first
    if d > last:
        return last
    return d

# Quick-add dialog (one global dialog)
if hasattr(st, "dialog"):
    @st.dialog("➕ Add expense", width="small", dismissible=False)
    def add_expense_dialog():
        ctx = st.session_state.get("quick_add_ctx", None)
        if not ctx:
            st.write("No context.")
            return
        year = int(ctx["year"])
        month = str(ctx["month"])
        category = str(ctx["category"])

        locked = get_locked_months(year)
        if month in locked:
            st.warning("This month is locked (statement closed). Unlock it to add/edit.")
            if st.button("Close"):
                st.session_state.pop("quick_add_ctx", None)
                st.rerun()
            return

        if st.button("Cancel"):
            st.session_state.pop("quick_add_ctx", None)
            st.rerun()
            return

        m_int = int(month)
        default_date = date(year, m_int, 1)
        default_date = clamp_date_to_month(date.today(), year, m_int)

        accounts = load_accounts()

        with st.form("quick_add_form"):
            st.markdown(f"**{month_name(month)} {year}**  \\nCategory: **{category}**")
            d = st.date_input("Date", value=default_date, min_value=date(year, m_int, 1), max_value=date(year, m_int, calendar.monthrange(year, m_int)[1]))
            amt = st.number_input("Amount (positive)", min_value=0.0, step=1.0, value=10.0)
            desc = st.text_input("Description", value="")
            acct = st.selectbox("Account", options=accounts, index=accounts.index("Venmo") if "Venmo" in accounts else 0)
            notes = st.text_input("Notes", value="")
            ok = st.form_submit_button("Add expense")
            if ok:
                if not desc.strip():
                    desc = f"Manual - {category}"
                df_one = pd.DataFrame([{
                    "txn_date": d.isoformat(),
                    "posted_date": None,
                    "description": desc.strip(),
                    "amount": -abs(float(amt)),
                    "kind": "expense",
                    "account": acct,
                    "category": category,
                    "excluded": 0,
                    "notes": notes.strip() or None,
                }])
                df_one["external_id"] = make_external_ids(df_one)
                insert_rows(df_one)
                st.session_state.pop("quick_add_ctx", None)
                st.success("Added.")
                st.rerun()

# ---------------------------
# App scaffold
# ---------------------------
st.set_page_config(page_title="BudgetOS", layout="wide")
init_db()

st.markdown(
    """
    <style>
      .block-container { padding-top: 0.9rem; padding-bottom: 2.5rem; }
      div[data-testid="stMetric"] { background: #ffffff; border: 1px solid #eef2f7; padding: 12px 14px; border-radius: 14px; }
      .small-note { color:#64748b; font-size: 0.9rem; }
      code { background: #0b1220; color: #e2e8f0; padding: 0.1rem 0.35rem; border-radius: 0.4rem; }
      /* Make secondary buttons look more like flat cells (used in sheet grid) */
      button[kind="secondary"] { border-radius: 10px; }
      /* Slightly soften dataframe borders */
      .stDataFrame { border: 1px solid #eef2f7; border-radius: 14px; overflow: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)

prefs = load_prefs()
expense_cats = load_expense_categories()
income_cats = load_income_categories()
accounts = load_accounts()

with db() as conn:
    years = [int(r["y"]) for r in conn.execute("SELECT DISTINCT substr(txn_date,1,4) as y FROM ledger ORDER BY y DESC").fetchall() if r["y"]]

default_year = years[0] if years else datetime.now().year

# Year selector
year = st.selectbox("Year", options=sorted(set(years + [default_year]), reverse=True), index=0)
df_year = fetch_year(year)

# Nav (keep admin pages at bottom)
page_options = ["Import", "Overview", "Sheet View", "Goals", "Export", "Admin: Ledger", "Admin: Categories"]
default_page = "Import" if df_year.empty else "Overview"
default_index = page_options.index(default_page)

with st.sidebar:
    st.markdown("## BudgetOS")
    page = st.radio("Navigate", page_options, index=default_index)
    st.divider()
    st.markdown("<span class='small-note'>Flow: Import JSON (first time) → Import Chase CSV → Categorize new charges → Add manual expenses + income → Sheet View</span>", unsafe_allow_html=True)

st.write("")

# ---------------------------
# Pages
# ---------------------------
if page == "Overview":
    st.subheader("Overview")

    if df_year.empty:
        st.info("No data for this year yet. Go to **Import**.")
        st.stop()

    exp_table, inc_table, savings_table, savings_rate_ytd = compute_summary(df_year, expense_cats, income_cats)
    total_spent = float(exp_table.loc["Total Spent","YTD Totals"])
    total_income = float(inc_table.loc["Total Net Income","YTD Totals"])
    total_saved = float(savings_table.loc["Total Saved","YTD Totals"])

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Spending (YTD)", currency(total_spent))
    m2.metric("Net Income (YTD)", currency(total_income))
    m3.metric("Total Saved (YTD)", currency(total_saved))
    m4.metric("Savings Rate (YTD)", f"{savings_rate_ytd*100:.2f}%")

    st.divider()

    # Month range filter
    start_m_name, end_m_name = month_range_selector("ov_month_range", default=(MONTHS[0], MONTHS[-1]))
    range_months = months_between(start_m_name, end_m_name)

    d = df_year.copy()
    d["month"] = d["txn_date"].str.slice(0,7)
    d["m"] = d["txn_date"].str.slice(5,7)
    d = d[d["m"].isin(range_months)]

    d["spend"] = d.apply(lambda r: signed_to_spend(r["amount"]) if (r["kind"]=="expense" and r["excluded"]==0) else 0.0, axis=1)
    d["income"] = d.apply(lambda r: float(r["amount"]) if (r["kind"]=="income" and r["excluded"]==0) else 0.0, axis=1)
    monthly = d.groupby("month")[["spend","income"]].sum().reset_index()
    if not monthly.empty:
        monthly["month"] = pd.to_datetime(monthly["month"] + "-01")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("### Monthly Spend")
        if monthly.empty:
            st.info("No data in this month range.")
        else:
            st.line_chart(monthly.set_index("month")["spend"])
    with c2:
        st.markdown("### Monthly Income")
        if monthly.empty:
            st.info("No data in this month range.")
        else:
            st.line_chart(monthly.set_index("month")["income"])

    st.divider()
    st.markdown("### Needs category")
    needs = df_year[(df_year["kind"]=="expense") & (df_year["excluded"]==0) & (df_year["category"].isna() | (df_year["category"]==""))]
    if needs.empty:
        st.success("Nothing to categorize 🎉")
    else:
        st.dataframe(needs[["txn_date","description","amount","account"]], use_container_width=True, height=320)

elif page == "Sheet View":
    st.subheader("Sheet View")

    if df_year.empty:
        st.info("No data yet for this year.")
        st.stop()

    locked = get_locked_months(year)

    # Controls
    cA, cB, cC = st.columns([1.3, 1.2, 1.5])
    with cA:
        sort_alpha = st.checkbox("Sort categories A→Z", value=False)
    with cB:
        show_uncat = st.checkbox("Show Uncategorized row", value=True)
    with cC:
        st.caption("Tip: Use Quick Add to add a manual transaction without leaving Sheet View.")

    # Month locks
    with st.expander("Statement Close (Lock months so totals don't change accidentally)", expanded=False):
        st.markdown("<span class='small-note'>Locked months block: importing into that month, categorizing charges in that month, and editing totals.</span>", unsafe_allow_html=True)
        with st.form("lock_months_form"):
            cols = st.columns(6)
            new_locked = set()
            for i, m in enumerate(MONTH_COLS):
                label = MONTHS[int(m)-1][:3]
                if cols[i % 6].checkbox(label, value=(m in locked), key=f"lock_{year}_{m}"):
                    new_locked.add(m)
            saved = st.form_submit_button("Save month locks")
            if saved:
                save_locked_months(year, new_locked)
                st.success("Saved.")
                st.rerun()

    # Quick add expense (from Sheet View)
    with st.expander("➕ Quick Add Expense", expanded=False):
        q1, q2, q3 = st.columns([2.2, 1.2, 1.2])
        with q1:
            qa_cat = st.selectbox("Category", options=expense_cats, key="sv_quick_cat")
        with q2:
            qa_month_name = st.selectbox("Month", options=MONTHS, index=date.today().month-1, key="sv_quick_month")
        with q3:
            st.write("")  # spacing
            if st.button("Add", type="primary", use_container_width=True):
                qa_month = f"{MONTHS.index(qa_month_name)+1:02d}"
                st.session_state["quick_add_ctx"] = {"year": year, "month": qa_month, "category": qa_cat}
                st.rerun()

    # Compute tables
    cats_for_table = expense_cats.copy()
    if sort_alpha:
        cats_for_table = sorted(cats_for_table)

    exp_table, inc_table, savings_table, savings_rate_ytd = compute_summary(df_year, cats_for_table, income_cats)

    # Optionally add Uncategorized as a row (computed)
    if show_uncat:
        unc = df_year[(df_year["kind"]=="expense") & (df_year["excluded"]==0) & (df_year["category"].isna() | (df_year["category"]==""))].copy()
        if not unc.empty:
            unc["spend"] = unc["amount"].apply(signed_to_spend)
            unc["month"] = unc["txn_date"].str.slice(5,7)
            unc_month = unc.groupby("month")["spend"].sum()
            row = {m: float(unc_month.get(m, 0.0)) for m in MONTH_COLS}
            row.update({
                "Average Per Month": sum(row[m] for m in MONTH_COLS)/12.0,
                "YTD Totals": sum(row[m] for m in MONTH_COLS),
                "Average Per Week": sum(row[m] for m in MONTH_COLS)/52.0,
                "Average Per Day": sum(row[m] for m in MONTH_COLS)/364.0,
            })
            unc_df = pd.DataFrame([row], index=["Uncategorized"])
            # Insert Uncategorized just before Total Spent
            base_rows = [c for c in exp_table.index if c != "Total Spent"]
            exp_table = pd.concat([exp_table.loc[base_rows], unc_df, exp_table.loc[["Total Spent"]]], axis=0)

    # Summary header cards
    total_spent = float(exp_table.loc["Total Spent","YTD Totals"])
    total_income = float(inc_table.loc["Total Net Income","YTD Totals"])
    total_saved = float(savings_table.loc["Total Saved","YTD Totals"])

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total Spent", currency(total_spent))
    k2.metric("Total Net Income", currency(total_income))
    k3.metric("Total Saved", currency(total_saved))
    k4.metric("Savings Rate", f"{savings_rate_ytd*100:.2f}%")

    st.divider()

    # Classic sheet view tables (scrollable + readable)
    def rename_month_cols_for_display(df: pd.DataFrame) -> pd.DataFrame:
        mapping = {}
        for i in range(1, 13):
            m = f"{i:02d}"
            lock_icon = " 🔒" if m in locked else ""
            mapping[m] = f"{MONTHS[i-1]}{lock_icon}"
        return df.rename(columns=mapping)

    exp_disp = rename_month_cols_for_display(exp_table)
    inc_disp = rename_month_cols_for_display(inc_table)
    sav_disp = rename_month_cols_for_display(savings_table)

    st.markdown("### Expenses")
    st.dataframe(exp_disp.applymap(lambda x: currency(x) if isinstance(x,(int,float)) else x), use_container_width=True, height=560)

    st.markdown("### Income")
    st.dataframe(inc_disp.applymap(lambda x: currency(x) if isinstance(x,(int,float)) else x), use_container_width=True, height=320)

    st.markdown("### Savings")
    sav_fmt = format_sheet(sav_disp)
    c1, c2 = st.columns([3,1])
    with c1:
        st.dataframe(sav_fmt, use_container_width=True, height=140)
    with c2:
        st.metric("Savings Rate (YTD)", f"{savings_rate_ytd*100:.2f}%")

    # If dialog exists, calling it when ctx is set will show it.
    if hasattr(st, "dialog") and st.session_state.get("quick_add_ctx"):
        add_expense_dialog()


elif page == "Import":
    st.subheader("Import")

    locked = get_locked_months(year)

    st.markdown(f"<span class='small-note'>Flow: Import JSON (first time) → Import Chase CSV → Categorize new charges → Add manual expenses + income → Sheet View</span>", unsafe_allow_html=True)
    st.markdown(f"<span class='small-note'>Current mapping loaded: <b>{len(prefs)}</b> descriptions in preferences.json.</span>", unsafe_allow_html=True)

    st.markdown("### 1) Import JSON (preferences.json)")
    st.markdown("<span class='small-note'>You only need to do this the first time (or when you want to replace your mapping). After that, the app keeps it locally.</span>", unsafe_allow_html=True)

    colP1, colP2 = st.columns([2,1])
    with colP1:
        up_prefs = st.file_uploader("Upload preferences.json", type=["json"], key="prefs_upload")
        if up_prefs is not None:
            try:
                loaded = json.loads(up_prefs.read().decode("utf-8"))
                if not isinstance(loaded, dict):
                    st.error("preferences.json must be a JSON object (dict).")
                else:
                    save_prefs(loaded)
                    st.success(f"Loaded mapping with {len(loaded)} entries.")
                    st.rerun()
            except Exception as e:
                st.error(f"Could not parse JSON: {e}")
    with colP2:
        st.download_button(
            "Download current preferences.json",
            data=json.dumps(load_prefs(), indent=2),
            file_name="preferences.json",
            mime="application/json",
        )

    st.divider()
    st.markdown("### 2) Import Chase CSV")
    up = st.file_uploader("Drop a Chase statement CSV here", type=["csv"], key="chase_csv")
    account_name = st.selectbox("Account", options=accounts, index=accounts.index("Chase") if "Chase" in accounts else 0)
    route_credits = st.checkbox("Route statement credits (Adjustment) to Income", value=True)
    allow_locked = st.checkbox("Allow importing into locked months (not recommended)", value=False)

    if up is not None and st.button("Import CSV", type="primary"):
        try:
            df_new = import_chase_csv(up, account_name=account_name, route_statement_credits_to_income=route_credits)
            if df_new.empty:
                st.warning("No rows imported (CSV parsed but yielded no valid transactions).")
            else:
                # Block locked months unless override
                if not allow_locked:
                    df_new["m"] = df_new["txn_date"].str.slice(5,7)
                    before = len(df_new)
                    df_new = df_new[~df_new["m"].isin(locked)].copy()
                    skipped_locked = before - len(df_new)
                    if skipped_locked:
                        st.warning(f"Skipped {skipped_locked} rows because those months are locked.")

                # Apply mapping for expenses
                prefs = load_prefs()
                mask = df_new["kind"] == "expense"
                df_new.loc[mask, "category"] = df_new.loc[mask, "description"].map(prefs)

                ins, skip = insert_rows(df_new)
                st.success(f"Imported {len(df_new)} rows. Inserted {ins}, skipped {skip}.")
                st.info("Next: categorize any new charges below.")
                st.rerun()
        except Exception as e:
            st.error(str(e))

    st.divider()
    st.markdown("### 3) Categorize New Charges")

    df_year_now = fetch_year(year)
    needs = df_year_now[(df_year_now["kind"]=="expense") & (df_year_now["excluded"]==0) & (df_year_now["category"].isna() | (df_year_now["category"]==""))].copy()

    if needs.empty:
        st.success("Nothing to categorize 🎉")
    else:
        # Bulk mapping table
        prefs = load_prefs()

        unique_desc = needs.groupby("description").agg(
            count=("id","count"),
            total_amount=("amount","sum"),
            first_date=("txn_date","min"),
            last_date=("txn_date","max"),
        ).reset_index()
        unique_desc["total_spend"] = unique_desc["total_amount"].apply(signed_to_spend)
        unique_desc = unique_desc.sort_values(["count","total_spend"], ascending=[False, False])

        st.markdown("<span class='small-note'>Tip: Use the wizard for a quick one-by-one flow, or bulk map below. Saving updates preferences.json and applies to existing transactions (unlocked months only).</span>", unsafe_allow_html=True)

        # Wizard-style (modal)
        if hasattr(st, "dialog") and st.button("Open wizard (modal)", type="secondary"):
            st.session_state["cat_wizard_open"] = True

        if hasattr(st, "dialog"):
            @st.dialog("🏷️ Categorize new charge", width="small", dismissible=False)
            def categorize_dialog():
                prefs_local = load_prefs()
                needs_local = fetch_year(year)
                needs_local = needs_local[(needs_local["kind"]=="expense") & (needs_local["excluded"]==0) & (needs_local["category"].isna() | (needs_local["category"]==""))]
                if needs_local.empty:
                    st.success("Nothing left to categorize 🎉")
                    if st.button("Close"):
                        st.session_state.pop("cat_wizard_open", None)
                        st.rerun()
                    return

                row = needs_local.sort_values(["txn_date","id"]).iloc[0]
                desc = row["description"]
                txn_month = month_of(row["txn_date"])

                st.markdown(f"**Date:** {row['txn_date']}  \\n**Description:** `{desc}`  \\n**Amount:** {currency(signed_to_spend(row['amount']))}")
                if txn_month in locked:
                    st.warning("This transaction is in a locked month. Unlock the month to categorize it.")
                    if st.button("Close"):
                        st.session_state.pop("cat_wizard_open", None)
                        st.rerun()
                    return

                w_cat = st.selectbox("Assign category", options=[""] + expense_cats, key="wiz_cat")
                apply_to_prefs = st.checkbox("Save to preferences.json (apply to future exact matches)", value=True)
                apply_similar = st.checkbox("Also apply to similar variants (best-effort)", value=False)

                if st.button("Save & Next", type="primary"):
                    if not w_cat:
                        st.error("Pick a category.")
                        return

                    locked_months = get_locked_months(year)

                    # Update this txn
                    update_txn_by_id(int(row["id"]), {"category": w_cat})

                    # Save exact mapping + apply to all matching descriptions (unlocked)
                    if apply_to_prefs:
                        prefs_local[desc] = w_cat

                    update_category_for_description(desc, w_cat, year=year, locked_months=locked_months)

                    # Similar variants: group by normalized description among remaining needs
                    if apply_similar:
                        base = normalize_description(desc)
                        others = needs_local["description"].unique().tolist()
                        similar = [d for d in others if d != desc and normalize_description(d) == base]
                        for s in similar:
                            if apply_to_prefs:
                                prefs_local[s] = w_cat
                            update_category_for_description(s, w_cat, year=year, locked_months=locked_months)

                    if apply_to_prefs:
                        save_prefs(prefs_local)

                    st.session_state.pop("wiz_cat", None)
                    st.success("Saved.")
                    st.rerun()

                if st.button("Cancel"):
                    st.session_state.pop("cat_wizard_open", None)
                    st.rerun()

        if hasattr(st, "dialog") and st.session_state.get("cat_wizard_open"):
            categorize_dialog()

        # Bulk mapping table UI
        st.markdown("#### Bulk map (fast)")
        bulk = unique_desc[["description","count","total_spend","first_date","last_date"]].copy()
        bulk["category"] = bulk["description"].map(prefs).fillna("")
        edited = st.data_editor(
            bulk,
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            column_config={
                "description": st.column_config.TextColumn("Description", width="large"),
                "count": st.column_config.NumberColumn("Count", format="%d"),
                "total_spend": st.column_config.NumberColumn("Total Spend", format="$%.2f"),
                "category": st.column_config.SelectboxColumn("Category", options=[""] + expense_cats),
            },
            key="bulk_editor",
        )

        cA, cB = st.columns([1,1])
        with cA:
            if st.button("Save mappings", type="primary"):
                prefs_local = load_prefs()
                locked_months = get_locked_months(year)
                updates = 0
                for _, r in edited.iterrows():
                    desc = str(r["description"])
                    cat = str(r.get("category","")).strip()
                    if not cat:
                        continue
                    if prefs_local.get(desc) != cat:
                        prefs_local[desc] = cat
                        updates += 1
                    update_category_for_description(desc, cat, year=year, locked_months=locked_months)
                save_prefs(prefs_local)
                st.success(f"Saved {updates} mappings.")
                st.rerun()

        with cB:
            st.download_button(
                "Download updated preferences.json",
                data=json.dumps(load_prefs(), indent=2),
                file_name="preferences.json",
                mime="application/json",
            )

    st.divider()
    st.markdown("### 4) Add manual expenses + income")

    with st.expander("Add manual expense (Venmo/BOA/etc.)", expanded=False):
        with st.form("manual_exp"):
            c1, c2, c3, c4 = st.columns([1.2, 2.2, 1.1, 1.5])
            with c1:
                m_date = st.date_input("Date", value=date.today(), key="m_exp_date")
            with c2:
                m_desc = st.text_input("Description", key="m_exp_desc")
            with c3:
                m_amt = st.number_input("Amount (positive number)", min_value=0.0, step=1.0, value=10.0, key="m_exp_amt")
            with c4:
                m_acct = st.selectbox("Account", options=accounts, index=accounts.index("Venmo") if "Venmo" in accounts else 0, key="m_exp_acct")
            m_cat = st.selectbox("Category", options=[""] + expense_cats, key="m_exp_cat")
            m_excl = st.checkbox("Excluded (transfer/payment)", value=False, key="m_exp_excl")
            m_notes = st.text_input("Notes", "", key="m_exp_notes")
            ok = st.form_submit_button("Add Expense", type="primary")
            if ok:
                if not m_desc:
                    st.error("Description required.")
                else:
                    m_month = f"{m_date.month:02d}"
                    if (m_month in locked):
                        st.error("That month is locked. Unlock it to add expenses.")
                    else:
                        df_one = pd.DataFrame([{
                            "txn_date": m_date.isoformat(),
                            "posted_date": None,
                            "description": m_desc.strip(),
                            "amount": -abs(float(m_amt)),
                            "kind": "expense",
                            "account": m_acct,
                            "category": m_cat or None,
                            "excluded": 1 if m_excl else 0,
                            "notes": m_notes or None,
                        }])
                        df_one["external_id"] = make_external_ids(df_one)
                        insert_rows(df_one)
                        st.success("Added expense.")
                        st.rerun()

    with st.expander("Add manual income", expanded=False):
        with st.form("manual_inc"):
            c1, c2, c3, c4 = st.columns([1.2, 2.2, 1.1, 1.5])
            with c1:
                i_date = st.date_input("Date", value=date.today(), key="m_inc_date")
            with c2:
                i_desc = st.text_input("Description / source", key="m_inc_desc")
            with c3:
                i_amt = st.number_input("Amount (positive number)", min_value=0.0, step=1.0, value=100.0, key="m_inc_amt")
            with c4:
                i_acct = st.selectbox("Account", options=accounts, index=accounts.index("Other") if "Other" in accounts else 0, key="m_inc_acct")
            i_cat = st.selectbox("Income Category", options=[""] + income_cats, key="m_inc_cat")
            i_notes = st.text_input("Notes", "", key="m_inc_notes")
            ok2 = st.form_submit_button("Add Income", type="primary")
            if ok2:
                if not i_desc:
                    st.error("Description required.")
                else:
                    i_month = f"{i_date.month:02d}"
                    if (i_month in locked):
                        st.error("That month is locked. Unlock it to add income.")
                    else:
                        df_one = pd.DataFrame([{
                            "txn_date": i_date.isoformat(),
                            "posted_date": None,
                            "description": i_desc.strip(),
                            "amount": abs(float(i_amt)),
                            "kind": "income",
                            "account": i_acct,
                            "category": i_cat or None,
                            "excluded": 0,
                            "notes": i_notes or None,
                        }])
                        df_one["external_id"] = make_external_ids(df_one)
                        insert_rows(df_one)
                        st.success("Added income.")
                        st.rerun()


elif page == "Goals":
    st.subheader("Goals")

    if not expense_cats:
        st.info("No expense categories configured.")
        st.stop()

    # Month range context for live progress
    start_m_name, end_m_name = month_range_selector("goals_month_range", default=(MONTHS[0], MONTHS[-1]))
    range_months = months_between(start_m_name, end_m_name)
    n_months = len(range_months)

    # Load existing goals
    with db() as conn:
        rows = conn.execute("SELECT category, monthly_goal FROM goals WHERE year=?", (int(year),)).fetchall()
    existing = {r["category"]: float(r["monthly_goal"]) for r in rows}

    # Actual spend by category in range
    d = df_year.copy()
    if d.empty:
        st.info("No transactions yet this year.")
        st.stop()

    d = d[(d["kind"]=="expense") & (d["excluded"]==0)].copy()
    d["m"] = d["txn_date"].str.slice(5,7)
    d = d[d["m"].isin(range_months)]
    d["spend"] = d["amount"].apply(signed_to_spend)
    actual = d.groupby("category")["spend"].sum()

    goal_rows = []
    for c in expense_cats:
        mg = float(existing.get(c, 0.0))
        goal_total = mg * n_months
        act = float(actual.get(c, 0.0))
        delta = goal_total - act
        status = "On track" if act <= goal_total else "Over"
        goal_rows.append({
            "category": c,
            "monthly_goal": mg,
            "goal_for_range": goal_total,
            "actual_for_range": act,
            "remaining": delta,
            "status": status,
        })

    gdf = pd.DataFrame(goal_rows)

    # Totals
    tot_goal = float(gdf["goal_for_range"].sum())
    tot_act = float(gdf["actual_for_range"].sum())
    tot_rem = tot_goal - tot_act

    k1, k2, k3 = st.columns(3)
    k1.metric("Goal (range)", currency(tot_goal))
    k2.metric("Actual (range)", currency(tot_act))
    k3.metric("Remaining", currency(tot_rem))

    st.divider()

    # Display table w/ color
    def style_goals(row):
        if row["monthly_goal"] == 0:
            return ["color: #64748b"] * len(row)
        if row["status"] == "Over":
            return ["background-color: rgba(239, 68, 68, 0.10)"] * len(row)
        return ["background-color: rgba(34, 197, 94, 0.10)"] * len(row)

    disp = gdf.copy()
    for col in ["monthly_goal","goal_for_range","actual_for_range","remaining"]:
        disp[col] = disp[col].apply(lambda x: currency(float(x)))
    styled = disp.style.apply(style_goals, axis=1)
    st.dataframe(styled, use_container_width=True, height=520)

    st.divider()
    st.markdown("### Edit goals (year-wide monthly targets)")
    edit_df = pd.DataFrame({
        "category": expense_cats,
        "monthly_goal": [existing.get(c, 0.0) for c in expense_cats],
    })
    edit_df["daily_goal (approx)"] = edit_df["monthly_goal"] / 30.33

    edited = st.data_editor(
        edit_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "monthly_goal": st.column_config.NumberColumn("monthly_goal", format="$%.2f"),
            "daily_goal (approx)": st.column_config.NumberColumn("daily_goal (approx)", format="$%.2f", disabled=True),
        },
        disabled=["daily_goal (approx)"],
        height=420,
        key="goals_editor",
    )

    if st.button("Save goals", type="primary"):
        with db() as conn:
            for _, r in edited.iterrows():
                conn.execute(
                    "INSERT INTO goals(year, category, monthly_goal) VALUES(?,?,?) "
                    "ON CONFLICT(year,category) DO UPDATE SET monthly_goal=excluded.monthly_goal",
                    (int(year), r["category"], float(r["monthly_goal"])),
                )
        st.success("Saved goals.")
        st.rerun()

elif page == "Export":
    st.subheader("Export")

    if df_year.empty:
        st.info("No data for this year.")
        st.stop()

    exp_table, inc_table, savings_table, savings_rate_ytd = compute_summary(df_year, expense_cats, income_cats)

    st.download_button(
        "Download Sheet View as Excel",
        data=export_sheet_view_to_excel(year, exp_table, inc_table, savings_table),
        file_name=f"BudgetOS_{year}_SheetView.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.markdown("### Raw exports")
    st.download_button("Download ledger CSV", data=df_year.to_csv(index=False), file_name=f"ledger_{year}.csv", mime="text/csv")
    st.download_button(
        "Download ledger JSON",
        data=json.dumps({"year": year, "transactions": df_year.fillna('').to_dict(orient='records')}, indent=2),
        file_name=f"ledger_{year}.json",
        mime="application/json",
    )

elif page == "Admin: Ledger":
    st.subheader("Ledger (Transactions)")

    if df_year.empty:
        st.info("No transactions for this year.")
        st.stop()

    locked = get_locked_months(year)

    c1, c2, c3, c4 = st.columns([2,1,1,1])
    with c1:
        q = st.text_input("Search description", "")
    with c2:
        kind = st.selectbox("Kind", ["(all)","expense","income"])
    with c3:
        show_excluded = st.selectbox("Excluded", ["Include","Only excluded","Only non-excluded"])
    with c4:
        acct = st.selectbox("Account", ["(all)"] + sorted(df_year["account"].fillna("").unique().tolist()))

    d = df_year.copy()
    if q:
        d = d[d["description"].str.contains(q, case=False, na=False)]
    if kind != "(all)":
        d = d[d["kind"] == kind]
    if show_excluded == "Only excluded":
        d = d[d["excluded"] == 1]
    elif show_excluded == "Only non-excluded":
        d = d[d["excluded"] == 0]
    if acct != "(all)":
        d = d[d["account"] == acct]

    d["month"] = d["txn_date"].str.slice(5,7)
    d["locked"] = d["month"].apply(lambda m: m in locked)

    st.markdown("<span class='small-note'>Edit directly in the table, then click <b>Save edits</b>. Locked months won't be modified.</span>", unsafe_allow_html=True)

    # Only allow editing of a few columns
    edit_cols = ["id","txn_date","description","amount","kind","account","category","excluded","notes","locked"]
    edit_df = d[edit_cols].copy()

    # Category options depend on kind; simplest: show union
    cat_union = sorted(set(expense_cats + income_cats))

    edited = st.data_editor(
        edit_df,
        use_container_width=True,
        hide_index=True,
        disabled=["id","locked"],
        column_config={
            "excluded": st.column_config.CheckboxColumn("excluded"),
            "category": st.column_config.SelectboxColumn("category", options=[""] + cat_union, required=False),
        },
        height=520,
        key="ledger_editor",
    )

    if st.button("Save edits", type="primary"):
        # Diff rows
        original = edit_df.set_index("id")
        new = edited.set_index("id")

        changes = 0
        for txn_id in new.index:
            if bool(new.loc[txn_id, "locked"]):
                continue
            diff = {}
            for col in ["txn_date","description","amount","kind","account","category","excluded","notes"]:
                ov = original.loc[txn_id, col]
                nv = new.loc[txn_id, col]
                if pd.isna(ov) and pd.isna(nv):
                    continue
                if str(ov) != str(nv):
                    # Normalize blanks
                    if col in ["category","notes"] and (nv == "" or (isinstance(nv, float) and pd.isna(nv))):
                        nv = None
                    diff[col] = nv
            if diff:
                update_txn_by_id(int(txn_id), diff)
                changes += 1

        st.success(f"Saved edits to {changes} transactions (skipped locked months).")
        st.rerun()

elif page == "Admin: Categories":
    st.subheader("Categories (Admin)")

    st.markdown("Controls Sheet View order and dropdown options. Add instead of rename/delete for longevity.")
    tab1, tab2, tab3 = st.tabs(["Expense Categories","Income Categories","Preferences JSON"])

    with tab1:
        cats_df = pd.DataFrame({"category": expense_cats})
        edited = st.data_editor(cats_df, use_container_width=True, hide_index=True, num_rows="dynamic")
        c1, c2 = st.columns([1,1])
        with c1:
            if st.button("Save expense categories", type="primary"):
                new = [c.strip() for c in edited["category"].fillna("").tolist() if c.strip()]
                save_expense_categories(new)
                st.success("Saved.")
                st.rerun()
        with c2:
            st.download_button(
                "Download expense_categories.json",
                data=json.dumps(expense_cats, indent=2),
                file_name="expense_categories.json",
                mime="application/json",
            )

    with tab2:
        inc_df = pd.DataFrame({"category": income_cats})
        edited2 = st.data_editor(inc_df, use_container_width=True, hide_index=True, num_rows="dynamic")
        c1, c2 = st.columns([1,1])
        with c1:
            if st.button("Save income categories", type="primary"):
                new = [c.strip() for c in edited2["category"].fillna("").tolist() if c.strip()]
                save_income_categories(new)
                st.success("Saved.")
                st.rerun()
        with c2:
            st.download_button(
                "Download income_categories.json",
                data=json.dumps(income_cats, indent=2),
                file_name="income_categories.json",
                mime="application/json",
            )

    with tab3:
        prefs_text = st.text_area("preferences.json", value=json.dumps(prefs, indent=2), height=420)
        c1, c2 = st.columns([1,1])
        with c1:
            if st.button("Save preferences.json", type="primary"):
                try:
                    obj = json.loads(prefs_text)
                    if not isinstance(obj, dict):
                        st.error("Must be a JSON object.")
                    else:
                        save_prefs(obj)
                        st.success("Saved.")
                        st.rerun()
                except Exception as e:
                    st.error(f"Invalid JSON: {e}")
        with c2:
            st.download_button(
                "Download preferences.json",
                data=json.dumps(prefs, indent=2),
                file_name="preferences.json",
                mime="application/json",
            )
