// Chase CSV → Monthly Category Table (web)
// No build tools: run via a local server (python -m http.server)

const els = {
  csvFile: document.getElementById("csvFile"),
  categoriesFile: document.getElementById("categoriesFile"),
  prefsFile: document.getElementById("prefsFile"),
  loadBtn: document.getElementById("loadBtn"),
  clearBtn: document.getElementById("clearBtn"),
  status: document.getElementById("status"),
  unmatched: document.getElementById("unmatched"),
  tableWrap: document.getElementById("tableWrap"),
  drilldown: document.getElementById("drilldown"),
  debitsOnly: document.getElementById("debitsOnly"),
  ignorePayments: document.getElementById("ignorePayments"),
  downloadPrefsBtn: document.getElementById("downloadPrefsBtn"),
  saveLocalBtn: document.getElementById("saveLocalBtn"),
  yearSelect: document.getElementById("yearSelect"),
};

let categories = [];
let preferences = {};
let transactions = []; // parsed from CSV
let activeYear = null;

// strings to ignore (normalized)
const IGNORE_DESC_SUBSTRINGS = [
  "PAYMENT THANK YOU - WEB",
];

function norm(s) {
  return String(s ?? "")
    .trim()
    .replace(/\s+/g, " ")
    .toUpperCase();
}

function money(n) {
  if (!n || Math.abs(n) < 0.005) return "$0.00";
  return n.toLocaleString(undefined, { style: "currency", currency: "USD" });
}

function toDate(dateStr) {
  const s = String(dateStr ?? "").trim();
  if (!s) return null;
  const d1 = new Date(s);
  if (!isNaN(d1.getTime())) return d1;
  const m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2,4})$/);
  if (m) {
    const mm = parseInt(m[1], 10);
    const dd = parseInt(m[2], 10);
    let yy = parseInt(m[3], 10);
    if (yy < 100) yy += 2000;
    const d = new Date(yy, mm - 1, dd);
    if (!isNaN(d.getTime())) return d;
  }
  return null;
}

function setStatus(msg) { els.status.textContent = msg; }
function enableControls(enabled) {
  els.loadBtn.disabled = !enabled;
  els.clearBtn.disabled = !enabled;
}

async function loadDefaults() {
  const localCats = localStorage.getItem("expense_categories_json");
  const localPrefs = localStorage.getItem("preferences_json");

  categories = localCats ? JSON.parse(localCats) : await fetch("./expense_categories.json").then(r => r.json());
  preferences = localPrefs ? JSON.parse(localPrefs) : await fetch("./preferences.json").then(r => r.json());
}

function buildPreferenceIndex() {
  const exact = new Map();
  const keys = Object.keys(preferences || {});
  for (const k of keys) exact.set(norm(k), preferences[k]);

  const containsList = keys
    .map(k => [norm(k), preferences[k]])
    .filter(([kNorm]) => kNorm.length >= 6)
    .sort((a, b) => b[0].length - a[0].length);

  return { exact, containsList };
}

function categorize(desc, prefIndex) {
  const d = norm(desc);
  if (!d) return null;
  const direct = prefIndex.exact.get(d);
  if (direct) return direct;
  for (const [kNorm, cat] of prefIndex.containsList) {
    if (d.includes(kNorm)) return cat;
  }
  return null;
}

function shouldIgnoreTx(desc, typeMaybe) {
  if (!els.ignorePayments.checked) return false;
  const d = norm(desc);
  if (IGNORE_DESC_SUBSTRINGS.some(sub => d.includes(sub))) return true;

  // If the CSV provides a Type column and it says PAYMENT, ignore those too.
  const t = norm(typeMaybe);
  if (t && t.includes("PAYMENT")) return true;

  return false;
}

function detectColumns(headers) {
  const h = headers.map(x => String(x ?? "").trim());
  const find = (cands) => {
    for (const c of cands) {
      const idx = h.findIndex(x => x.toLowerCase() === c.toLowerCase());
      if (idx >= 0) return h[idx];
    }
    for (const c of cands) {
      const idx = h.findIndex(x => x.toLowerCase().includes(c.toLowerCase()));
      if (idx >= 0) return h[idx];
    }
    return null;
  };

  const dateCol = find(["Transaction Date", "Trans Date", "Posting Date", "Post Date", "Date"]);
  const descCol = find(["Description", "Transaction Description", "Details", "Merchant", "Name"]);
  const amountCol = find(["Amount", "Transaction Amount"]);
  const debitCol = find(["Debit"]);
  const creditCol = find(["Credit"]);
  const typeCol = find(["Type"]);

  return { dateCol, descCol, amountCol, debitCol, creditCol, typeCol };
}

function parseAmount(row, cols) {
  const clean = (v) => {
    if (v === null || v === undefined || v === "") return 0;
    const s = String(v).replace(/[$,]/g, "").trim();
    const n = Number(s);
    return isNaN(n) ? 0 : n;
  };
  if (cols.amountCol) return clean(row[cols.amountCol]);
  const debit = cols.debitCol ? clean(row[cols.debitCol]) : 0;
  const credit = cols.creditCol ? clean(row[cols.creditCol]) : 0;
  return credit - debit;
}

function monthIndex(d) { return d.getMonth(); }
function yearValue(d) { return d.getFullYear(); }

function computePivot(year, debitsOnly) {
  const months = 12;
  const totals = {};
  for (const cat of categories) totals[cat] = Array(months).fill(0);

  const perCellTx = {};
  const addTx = (cat, m, tx) => {
    const k = `${cat}|${m}`;
    if (!perCellTx[k]) perCellTx[k] = [];
    perCellTx[k].push(tx);
  };

  const yearTx = transactions.filter(t => t.date && yearValue(t.date) === year);

  for (const t of yearTx) {
    if (!t.category) continue;
    let amt = t.amount;

    if (debitsOnly) {
      if (amt > 0) continue;
      amt = Math.abs(amt);
    } else {
      amt = -amt; // "spend" positive
    }

    const m = monthIndex(t.date);
    if (!totals[t.category]) totals[t.category] = Array(12).fill(0);
    totals[t.category][m] += amt;
    addTx(t.category, m, t);
  }

  const totalRow = Array(12).fill(0);
  for (const cat of Object.keys(totals)) {
    for (let m = 0; m < 12; m++) totalRow[m] += totals[cat][m] || 0;
  }

  return { totals, totalRow, perCellTx };
}

function renderYearOptions() {
  const years = [...new Set(transactions.map(t => t.date ? yearValue(t.date) : null).filter(Boolean))].sort((a,b)=>a-b);
  els.yearSelect.innerHTML = "";
  for (const y of years) {
    const opt = document.createElement("option");
    opt.value = String(y);
    opt.textContent = String(y);
    els.yearSelect.appendChild(opt);
  }
  activeYear = years[years.length - 1] ?? null;
  if (activeYear) els.yearSelect.value = String(activeYear);
  els.yearSelect.disabled = !activeYear;
}

function renderUnmatched(prefIndex) {
  const missing = new Map();
  const debitsOnly = els.debitsOnly.checked;

  for (const t of transactions) {
    if (t.category) continue;
    const k = norm(t.description);
    if (!k) continue;
    if (!missing.has(k)) missing.set(k, { exampleDesc: t.description, count: 0, total: 0, rows: [] });
    const g = missing.get(k);
    g.count += 1;
    g.rows.push(t);
    if (debitsOnly) {
      if (t.amount < 0) g.total += Math.abs(t.amount);
    } else {
      g.total += Math.max(0, -t.amount);
    }
  }

  const groups = [...missing.values()].sort((a,b)=>b.total-a.total);

  els.unmatched.innerHTML = "";
  if (groups.length === 0) {
    els.unmatched.innerHTML = "<p class='muted'>Nice — everything in this CSV matched a category.</p>";
    els.downloadPrefsBtn.disabled = false;
    els.saveLocalBtn.disabled = false;
    return;
  }

  // datalist for type-to-filter
  let dl = document.getElementById("catDatalist");
  if (!dl) {
    dl = document.createElement("datalist");
    dl.id = "catDatalist";
    document.body.appendChild(dl);
  }
  dl.innerHTML = categories.map(c => `<option value="${escapeAttr(c)}"></option>`).join("");

  const frag = document.createDocumentFragment();

  for (const g of groups) {
    const div = document.createElement("div");
    div.className = "unmatchedItem";

    const top = document.createElement("div");
    top.className = "unmatchedTop";

    const left = document.createElement("div");
    left.innerHTML = `<div class="unmatchedDesc">${escapeHtml(g.exampleDesc)}</div>
      <div class="muted small"><span class="badge">${g.count} tx</span> <span class="badge">${money(g.total)} est. spend</span></div>`;

    const controls = document.createElement("div");
    controls.className = "row";

    const sel = document.createElement("select");
    sel.innerHTML = `<option value="">Pick category…</option>` + categories.map(c => `<option value="${escapeAttr(c)}">${escapeHtml(c)}</option>`).join("");

    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "…or type category";
    input.setAttribute("list", "catDatalist");
    input.style.minWidth = "180px";

    const apply = document.createElement("button");
    apply.textContent = "Apply";
    apply.className = "primary";

    const applyCategory = (chosen) => {
      if (!chosen) return;
      // If they typed a new category not in the list, add it.
      if (!categories.includes(chosen)) categories.push(chosen);

      preferences[g.exampleDesc] = chosen;
      for (const t of transactions) {
        if (norm(t.description) === norm(g.exampleDesc)) t.category = chosen;
      }

      // Optional autosave
      if (localStorage.getItem("autosave_enabled") === "true") {
        localStorage.setItem("preferences_json", JSON.stringify(preferences));
        localStorage.setItem("expense_categories_json", JSON.stringify(categories));
      }

      const newIndex = buildPreferenceIndex();
      renderUnmatched(newIndex);
      renderTable();
    };

    sel.addEventListener("change", () => {
      input.value = sel.value;
    });

    apply.addEventListener("click", () => applyCategory(input.value.trim()));

    controls.appendChild(sel);
    controls.appendChild(input);
    controls.appendChild(apply);

    top.appendChild(left);
    top.appendChild(controls);

    const sample = g.rows.slice(0, 5).map(t => {
      const d = t.date ? t.date.toLocaleDateString() : "";
      return `<div class="muted small">${escapeHtml(d)} · ${money(Math.abs(t.amount))}</div>`;
    }).join("");

    div.appendChild(top);
    div.insertAdjacentHTML("beforeend", `<div class="mt">${sample}</div>`);
    frag.appendChild(div);
  }

  els.unmatched.appendChild(frag);
  els.downloadPrefsBtn.disabled = false;
  els.saveLocalBtn.disabled = false;
}

function renderTable() {
  if (!activeYear) {
    els.tableWrap.classList.add("muted");
    els.tableWrap.textContent = "Upload a CSV to see results.";
    return;
  }

  const debitsOnly = els.debitsOnly.checked;
  const { totals, totalRow, perCellTx } = computePivot(activeYear, debitsOnly);

  const monthNames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

  const table = document.createElement("table");

  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  hr.innerHTML = `<th>Category</th>` + monthNames.map(m => `<th>${m}</th>`).join("");
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");

  const trTotal = document.createElement("tr");
  trTotal.className = "totalRow";
  trTotal.innerHTML = `<td>Total</td>` + totalRow.map(v => `<td class="num">${money(v)}</td>`).join("");
  tbody.appendChild(trTotal);

  for (const cat of categories) {
    const row = document.createElement("tr");
    const cells = totals[cat] || Array(12).fill(0);
    const first = document.createElement("td");
    first.textContent = cat;
    row.appendChild(first);

    for (let m = 0; m < 12; m++) {
      const v = cells[m] || 0;
      const td = document.createElement("td");
      td.className = `num clickable ${v === 0 ? "zero" : ""}`;
      td.textContent = money(v);
      td.title = "Click to view transactions";
      td.addEventListener("click", () => showDrilldown(cat, m, perCellTx));
      row.appendChild(td);
    }
    tbody.appendChild(row);
  }

  table.appendChild(tbody);
  els.tableWrap.classList.remove("muted");
  els.tableWrap.innerHTML = "";
  els.tableWrap.appendChild(table);
}

function showDrilldown(cat, monthIdx, perCellTx) {
  const monthNames = ["January","February","March","April","May","June","July","August","September","October","November","December"];
  const k = `${cat}|${monthIdx}`;
  const rows = (perCellTx[k] || []).slice().sort((a,b)=>a.date-b.date);

  const wrap = document.createElement("div");
  wrap.className = "card";
  wrap.style.marginTop = "14px";

  wrap.innerHTML = `<div class="row"><strong>${escapeHtml(cat)}</strong>
    <span class="badge">${monthNames[monthIdx]} ${activeYear}</span>
    <span class="badge">${rows.length} tx</span></div>`;

  if (rows.length === 0) {
    wrap.insertAdjacentHTML("beforeend", `<p class="muted mt">No transactions in this cell.</p>`);
  } else {
    const list = document.createElement("div");
    list.className = "mt";
    list.innerHTML = rows.map(t => {
      const d = t.date ? t.date.toLocaleDateString() : "";
      const amt = els.debitsOnly.checked ? Math.abs(t.amount) : -t.amount;
      return `<div class="row" style="justify-content: space-between; gap: 16px; padding: 6px 0; border-bottom: 1px solid var(--border);">
        <div>
          <div>${escapeHtml(t.description)}</div>
          <div class="muted small">${escapeHtml(d)}</div>
        </div>
        <div class="num" style="min-width:120px;">${money(Math.max(0, amt))}</div>
      </div>`;
    }).join("");
    wrap.appendChild(list);
  }

  els.drilldown.innerHTML = "";
  els.drilldown.appendChild(wrap);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}
function escapeAttr(s) { return String(s ?? "").replace(/"/g, "&quot;"); }

async function readJsonFile(file) {
  const text = await file.text();
  return JSON.parse(text);
}

function downloadJson(filename, obj) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function clearAll() {
  transactions = [];
  activeYear = null;
  els.unmatched.innerHTML = "";
  els.tableWrap.classList.add("muted");
  els.tableWrap.textContent = "Upload a CSV to see results.";
  els.drilldown.innerHTML = "";
  els.yearSelect.innerHTML = "";
  els.yearSelect.disabled = true;
  setStatus("");
  els.downloadPrefsBtn.disabled = true;
  els.saveLocalBtn.disabled = true;
  enableControls(false);
  els.csvFile.value = "";
}

async function processCsv(file) {
  setStatus("Parsing CSV…");
  const csvText = await file.text();

  const parsed = Papa.parse(csvText, { header: true, skipEmptyLines: true, dynamicTyping: false });
  const rows = parsed.data || [];
  if (!rows.length) { setStatus("No rows found in CSV."); return; }

  const headers = parsed.meta?.fields || Object.keys(rows[0] || {});
  const cols = detectColumns(headers);

  if (!cols.dateCol || !cols.descCol || (!cols.amountCol && !(cols.debitCol || cols.creditCol))) {
    setStatus("Couldn't detect required columns. Expected at least Date, Description, Amount.");
    console.log({ headers, cols });
    return;
  }

  const prefIndex = buildPreferenceIndex();

  transactions = rows.map((r) => {
    const date = toDate(r[cols.dateCol]);
    const description = String(r[cols.descCol] ?? "").trim();
    const typeMaybe = cols.typeCol ? r[cols.typeCol] : "";
    const amount = parseAmount(r, cols);

    if (shouldIgnoreTx(description, typeMaybe)) return null;

    const category = categorize(description, prefIndex);
    return { date, description, amount, category, raw: r };
  }).filter(Boolean).filter(t => t.date && t.description);

  renderYearOptions();
  renderUnmatched(prefIndex);
  renderTable();

  enableControls(true);
  els.downloadPrefsBtn.disabled = false;
  els.saveLocalBtn.disabled = false;
  setStatus(`Loaded ${transactions.length} transactions (after ignoring payments).`);
}

// --- wire up events ---
await loadDefaults();
setStatus("Ready. Upload a Chase CSV to begin.");
enableControls(false);

els.csvFile.addEventListener("change", () => { els.loadBtn.disabled = !els.csvFile.files?.[0]; });

els.loadBtn.addEventListener("click", async () => {
  try {
    if (els.categoriesFile.files?.[0]) categories = await readJsonFile(els.categoriesFile.files[0]);
    if (els.prefsFile.files?.[0]) preferences = await readJsonFile(els.prefsFile.files[0]);
    if (!els.csvFile.files?.[0]) return;
    await processCsv(els.csvFile.files[0]);
  } catch (e) {
    console.error(e);
    setStatus("Error: " + (e?.message || String(e)));
  }
});

els.clearBtn.addEventListener("click", clearAll);

els.debitsOnly.addEventListener("change", () => {
  const prefIndex = buildPreferenceIndex();
  renderUnmatched(prefIndex);
  renderTable();
});

els.ignorePayments.addEventListener("change", () => {
  // If we already have a CSV loaded, reprocess would be ideal, but for simplicity:
  // prompt user to re-click Process CSV (CSV input still present).
  setStatus("Toggle changed. Click Process CSV again to re-run filtering.");
});

els.yearSelect.addEventListener("change", () => {
  activeYear = Number(els.yearSelect.value);
  renderTable();
  els.drilldown.innerHTML = "";
});

els.downloadPrefsBtn.addEventListener("click", () => downloadJson("preferences.json", preferences));

els.saveLocalBtn.addEventListener("click", () => {
  localStorage.setItem("preferences_json", JSON.stringify(preferences));
  localStorage.setItem("expense_categories_json", JSON.stringify(categories));
  localStorage.setItem("autosave_enabled", "true");
  setStatus("Auto-save enabled in this browser. Future category assignments will save automatically.");
});
