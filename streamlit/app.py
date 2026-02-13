import json
from pathlib import Path
from datetime import datetime
import pandas as pd
import streamlit as st

HERE = Path(__file__).parent
CATS_PATH = HERE / "expense_categories.json"
PREFS_PATH = HERE / "preferences.json"

st.set_page_config(page_title="Chase CSV → Monthly Category Table", layout="wide")

def norm(s: str) -> str:
    return " ".join(str(s or "").strip().upper().split())

IGNORE_SUBSTRINGS = ["PAYMENT THANK YOU - WEB"]

@st.cache_data
def load_categories():
    return json.loads(CATS_PATH.read_text(encoding="utf-8"))

def load_prefs():
    return json.loads(PREFS_PATH.read_text(encoding="utf-8"))

def save_prefs(prefs: dict):
    PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")

def detect_columns(cols):
    cols = list(cols)
    lower = [c.lower() for c in cols]
    def find(cands):
        for c in cands:
            if c.lower() in lower:
                return cols[lower.index(c.lower())]
        for c in cands:
            for i, v in enumerate(lower):
                if c.lower() in v:
                    return cols[i]
        return None
    return {
        "date": find(["Transaction Date","Posting Date","Date","Post Date"]),
        "desc": find(["Description","Transaction Description","Details","Merchant","Name"]),
        "amount": find(["Amount","Transaction Amount"]),
        "debit": find(["Debit"]),
        "credit": find(["Credit"]),
        "type": find(["Type"])
    }

def parse_amount(row, cols):
    def clean(v):
        if pd.isna(v) or v == "":
            return 0.0
        s = str(v).replace("$","").replace(",","").strip()
        try:
            return float(s)
        except:
            return 0.0
    if cols["amount"]:
        return clean(row[cols["amount"]])
    debit = clean(row[cols["debit"]]) if cols["debit"] else 0.0
    credit = clean(row[cols["credit"]]) if cols["credit"] else 0.0
    return credit - debit

def should_ignore(desc, typ, ignore_payments: bool):
    if not ignore_payments:
        return False
    d = norm(desc)
    if any(sub in d for sub in IGNORE_SUBSTRINGS):
        return True
    t = norm(typ)
    if t and "PAYMENT" in t:
        return True
    return False

def build_pref_index(prefs: dict):
    exact = {norm(k): v for k, v in prefs.items()}
    contains = sorted([(norm(k), v) for k, v in prefs.items() if len(norm(k)) >= 6], key=lambda x: len(x[0]), reverse=True)
    return exact, contains

def categorize(desc, exact, contains):
    d = norm(desc)
    if d in exact:
        return exact[d]
    for k, cat in contains:
        if k in d:
            return cat
    return None

st.title("Chase CSV → Monthly Category Table (local autosave)")

with st.sidebar:
    st.header("Settings")
    debits_only = st.checkbox("Count spending as debits only", value=True)
    ignore_payments = st.checkbox("Ignore payments like Payment Thank You - Web", value=True)

cats = load_categories()
prefs = load_prefs()

uploaded = st.file_uploader("Upload Chase CSV", type=["csv"])
if not uploaded:
    st.info("Upload a CSV to begin.")
    st.stop()

df = pd.read_csv(uploaded)
cols = detect_columns(df.columns)

if not cols["date"] or not cols["desc"] or (not cols["amount"] and not (cols["debit"] or cols["credit"])):
    st.error("Couldn't detect required columns. Need Date, Description, and Amount (or Debit/Credit).")
    st.write({"detected": cols, "columns": list(df.columns)})
    st.stop()

# Parse
exact, contains = build_pref_index(prefs)

def parse_date(x):
    try:
        return pd.to_datetime(x)
    except:
        return pd.NaT

parsed = pd.DataFrame({
    "date": df[cols["date"]].apply(parse_date),
    "description": df[cols["desc"]].astype(str).fillna(""),
    "type": df[cols["type"]].astype(str).fillna("") if cols["type"] else "",
})

parsed["amount"] = df.apply(lambda r: parse_amount(r, cols), axis=1)

parsed = parsed[parsed["date"].notna()]
parsed = parsed[~parsed["description"].str.strip().eq("")]

// ignore payments
parsed = parsed[~parsed.apply(lambda r: should_ignore(r["description"], r["type"], ignore_payments), axis=1)]

parsed["category"] = parsed["description"].apply(lambda d: categorize(d, exact, contains))

years = sorted(parsed["date"].dt.year.unique().tolist())
year = st.selectbox("Year", years, index=len(years)-1 if years else 0)

# Unmatched
unmatched = parsed[parsed["category"].isna()].copy()
st.subheader("Unmatched merchants")
if unmatched.empty:
    st.success("Everything matched a category for this CSV.")
else:
    unmatched["desc_norm"] = unmatched["description"].apply(norm)
    grp = unmatched.groupby("desc_norm").agg(
        example=("description","first"),
        count=("description","size"),
        total_amt=("amount","sum"),
    ).reset_index()

    # estimate spend
    if debits_only:
        grp["est_spend"] = unmatched.assign(sp=unmatched["amount"].where(unmatched["amount"] < 0, 0).abs()) \
            .groupby("desc_norm")["sp"].sum().values
    else:
        grp["est_spend"] = unmatched.assign(sp=(-unmatched["amount"]).clip(lower=0)) \
            .groupby("desc_norm")["sp"].sum().values

    grp = grp.sort_values("est_spend", ascending=False)

    for _, row in grp.iterrows():
        with st.container(border=True):
            st.write(f"**{row['example']}**  •  {int(row['count'])} tx  •  est. spend: ${row['est_spend']:.2f}")
            c1, c2, c3 = st.columns([2,2,1])
            with c1:
                pick = st.selectbox("Pick category", [""] + cats, key=f"pick_{row['desc_norm']}")
            with c2:
                typed = st.text_input("…or type category", value=pick, key=f"type_{row['desc_norm']}")
            with c3:
                if st.button("Apply", key=f"apply_{row['desc_norm']}"):
                    chosen = typed.strip()
                    if not chosen:
                        st.warning("Choose or type a category first.")
                    else:
                        if chosen not in cats:
                            cats.append(chosen)
                            CATS_PATH.write_text(json.dumps(cats, indent=2), encoding="utf-8")

                        prefs[row["example"]] = chosen
                        save_prefs(prefs)
                        st.success("Saved to preferences.json — re-run by refreshing the page.")
                        st.stop()

st.divider()
st.subheader("Monthly spending table")

# Prepare spending
use = parsed[parsed["date"].dt.year == year].copy()

if debits_only:
    use = use[use["amount"] < 0].copy()
    use["spend"] = use["amount"].abs()
else:
    use["spend"] = (-use["amount"]).clip(lower=0)

use = use[use["category"].notna()]

use["month"] = use["date"].dt.month

pivot = use.pivot_table(index="category", columns="month", values="spend", aggfunc="sum", fill_value=0.0)

# Ensure all categories appear
for cat in cats:
    if cat not in pivot.index:
        pivot.loc[cat] = 0.0
pivot = pivot.loc[cats]

# Add Total row
total = pivot.sum(axis=0)
pivot_out = pd.concat([pd.DataFrame([total], index=["Total"]), pivot], axis=0)

# Rename month numbers to labels
month_names = {i: datetime(2000, i, 1).strftime("%b") for i in range(1,13)}
pivot_out = pivot_out.rename(columns=month_names)

st.dataframe(pivot_out.style.format("${:,.2f}"), use_container_width=True)

st.caption("Tip: This Streamlit version saves preferences.json to disk when you click Apply.")
