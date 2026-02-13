# BudgetOS MVP (Local Streamlit App)

A modern local UI to replace the “run CSV through JSON + spreadsheet” workflow.

## What it does
- Import your **preferences.json** mapping (first time only)
- Import a **Chase statement CSV**
- Auto-categorize expenses using your **preferences JSON** (exact string match: `Description -> Category`)
- For **new/unmapped** charges, categorize them immediately on the **Import** page (wizard + bulk mapping)
  - Saving updates `preferences.json` and applies to existing transactions (unlocked months only)
- Manual entry for:
  - Additional **expense** transactions from other sources (Venmo/BOA/etc.)
  - **Income** (manual only)
- A **Sheet View** that mirrors your spreadsheet logic:
  - Monthly totals by category (Jan–Dec)
  - YTD totals
  - Avg per month (/12), Avg per week (/52), Avg per day (/364)
  - Total spent, total net income, monthly + YTD savings rate, total saved
- **Goals** page:
  - Year-wide monthly goals per category
  - Live “on track / over” status for a selected month range
- **Statement Close / Lock months**
  - Lock a month to prevent accidental changes to totals (importing, categorizing, and editing)

## Quickstart (Mac / Linux)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Files
- `app.py` – the app
- `budgetos.db` – created on first run (SQLite, local only)
- `config/preferences.json` – mapping file (`Description -> Category`). You can upload/download it in the UI.
- `config/expense_categories.json` – ordered list of expense categories (controls Sheet View order)
- `config/income_categories.json` – income categories
- `config/accounts.json` – account dropdown values for manual entry

## Notes
- Spend is calculated from signed amounts:
  - Chase charges are negative → converted to positive spend in summaries
  - Refunds (positive amounts) reduce spend
- Income is positive amounts and shown in the income section.
