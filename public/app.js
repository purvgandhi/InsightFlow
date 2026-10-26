// InsightFlow dashboard. Every number on the page comes from the /api endpoints (PostgreSQL).
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  meta: null,
  filters: { start: "", end: "", merchant: "", method: "" },
  gran: "week",
  table: { page: 1, pageSize: 25, q: "", status: "", sort: "created_at:desc", flagged: false },
  charts: {},
  lastTrend: null,
  lastBreakdowns: null,
  controller: null,
};

// ---------- formatting ----------
const inr0 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const inr2 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const int = new Intl.NumberFormat("en-IN");
const pct = (x, d = 1) => (x === null || x === undefined ? "—" : `${(x * 100).toFixed(d)}%`);
const money = (x) => (x === null || x === undefined ? "—" : inr0.format(x));
const money2 = (x) => (x === null || x === undefined ? "—" : inr2.format(x));
const compactInr = (x) => {
  const a = Math.abs(x);
  if (a >= 1e7) return `₹${(x / 1e7).toFixed(1)}Cr`;
  if (a >= 1e5) return `₹${(x / 1e5).toFixed(1)}L`;
  if (a >= 1e3) return `₹${(x / 1e3).toFixed(0)}K`;
  return `₹${x}`;
};
let TZ = "Asia/Kolkata";
const dateTime = (iso) => new Date(iso).toLocaleString("en-IN", { timeZone: TZ, day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
const bucketLabel = (d, gran) => {
  const dt = new Date(`${d}T00:00:00Z`);
  const opts = gran === "month" ? { month: "short", year: "numeric" } : { day: "2-digit", month: "short" };
  const s = dt.toLocaleDateString("en-IN", { ...opts, timeZone: "UTC" });
  return gran === "week" ? `Wk ${s}` : s;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ---------- API ----------
class ApiError extends Error {}
async function api(path, params = {}, signal) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v !== null && v !== undefined && v !== false));
  let res;
  try {
    res = await fetch(`/api/${path}${qs.toString() ? `?${qs}` : ""}`, { signal, headers: { Accept: "application/json" } });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError("Network error: could not reach the API.");
  }
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) {
    const detail = body?.message || (typeof body?.detail === "string" ? body.detail : body?.detail?.[0]?.msg);
    throw new ApiError(detail || `Request failed (${res.status})`);
  }
  return body;
}

function showError(msg) {
  const el = $("#error");
  el.innerHTML = `${esc(msg)} <button type="button" id="retry">Retry</button>`;
  el.hidden = false;
  $("#retry").onclick = () => { el.hidden = true; state.meta ? refresh() : init(); };
}

// ---------- URL state (so refresh / sharing keeps the view) ----------
function readUrl() {
  const p = new URLSearchParams(location.search);
  for (const k of Object.keys(state.filters)) if (p.get(k)) state.filters[k] = p.get(k);
  if (["day", "week", "month"].includes(p.get("gran"))) state.gran = p.get("gran");
}
function writeUrl() {
  const p = new URLSearchParams(Object.entries({ ...state.filters, gran: state.gran }).filter(([, v]) => v));
  history.replaceState(null, "", `${location.pathname}?${p}`);
}

// ---------- KPI tiles ----------
const KPI_TILES = [
  { key: "total_transactions", label: "Total transactions", fmt: (k) => int.format(k.total_transactions),
    hint: (k) => `${int.format(k.successful)} successful · ${int.format(k.failed)} failed · ${int.format(k.pending + k.cancelled)} pending/cancelled`,
    formula: "COUNT(*) of all payment attempts in the range, any status" },
  { key: "gtv", label: "Gross transaction value", fmt: (k) => money(k.gtv), primary: true,
    hint: () => "Successful payments, before refunds", formula: "SUM(amount) WHERE status = 'success'" },
  { key: "refund_amount", label: "Refund amount", fmt: (k) => money(k.refund_amount),
    hint: (k) => `${int.format(k.refunded_transactions)} payments refunded`, formula: "SUM(refund amount) for successful payments made in the range" },
  { key: "refund_rate", label: "Refund rate", fmt: (k) => pct(k.refund_rate, 2),
    hint: (k) => `By value · ${pct(k.refund_count_rate)} by count`, formula: "Refund amount ÷ GTV" },
  { key: "net_transaction_value", label: "Net transaction value", fmt: (k) => money(k.net_transaction_value), primary: true,
    hint: () => "GTV minus refunds", formula: "GTV − refund amount" },
  { key: "net_revenue", label: "Net revenue (fees)", fmt: (k) => money2(k.net_revenue), primary: true,
    hint: (k) => `${money(k.fee_revenue)} fees − ${money(k.fee_reversed)} reversed`, formula: "SUM(fee_amount) of successful payments − fees reversed pro-rata on refunds" },
  { key: "aov", label: "Avg transaction value", fmt: (k) => money2(k.aov),
    hint: () => "Per successful payment", formula: "GTV ÷ number of successful payments" },
  { key: "success_rate", label: "Success rate", fmt: (k) => pct(k.success_rate),
    hint: () => "Pending & cancelled excluded", formula: "successful ÷ (successful + failed)" },
];

function renderKpiSkeleton() {
  $("#kpis").setAttribute("aria-busy", "true");
  $("#kpis").innerHTML = KPI_TILES.map((t) => `
    <div class="kpi"><div class="label">${t.label}</div>
      <div class="skeleton" style="height:30px;width:70%"></div><div class="skeleton" style="height:12px;width:50%"></div></div>`).join("");
}
function renderKpis(k) {
  $("#kpis").setAttribute("aria-busy", "false");
  $("#kpis").innerHTML = KPI_TILES.map((t) => `
    <div class="kpi${t.primary ? " primary" : ""}">
      <div class="label">${t.label}<span class="info" title="${esc(t.formula)}" aria-label="Formula: ${esc(t.formula)}">i</span></div>
      <div class="value">${t.fmt(k)}</div>
      <div class="hint">${esc(t.hint(k))}</div>
    </div>`).join("");
}

// ---------- charts ----------
function baseOptions() {
  const grid = css("--grid"), muted = css("--muted"), ink = css("--ink");
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: css("--surface"), titleColor: ink, bodyColor: ink, borderColor: css("--axis"), borderWidth: 1,
        padding: 10, boxPadding: 4, usePointStyle: true,
      },
    },
    scales: {
      x: { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: muted, maxRotation: 0, autoSkipPadding: 16 } },
      y: { grid: { color: grid }, border: { display: false }, ticks: { color: muted, callback: (v) => compactInr(v) }, beginAtZero: true },
    },
  };
}

function setEmpty(canvasId, isEmpty, msg = "No data for the selected filters") {
  const box = document.getElementById(canvasId).parentElement;
  box.querySelector(".empty")?.remove();
  document.getElementById(canvasId).style.display = isEmpty ? "none" : "";
  if (isEmpty) box.insertAdjacentHTML("beforeend", `<div class="empty">${esc(msg)}</div>`);
}

function drawChart(id, config) {
  state.charts[id]?.destroy();
  state.charts[id] = new Chart(document.getElementById(id), config);
}

function renderTrend(data) {
  state.lastTrend = data;
  const s = data.series;
  const series = [
    { label: "Gross transaction value", key: "gtv", color: css("--series-1") },
    { label: "Refunds", key: "refund_amount", color: css("--series-2") },
    { label: "Net transaction value", key: "net_transaction_value", color: css("--series-3") },
  ];
  $("#trend-legend").innerHTML = series.map((x) => `<span><i style="background:${x.color}"></i>${x.label}</span>`).join("");
  const empty = !s.length || s.every((r) => r.gtv === 0);
  setEmpty("trend-chart", empty);
  const opts = baseOptions();
  opts.plugins.tooltip.callbacks = {
    title: (items) => `${items[0].label}${s[items[0].dataIndex].partial ? " (partial period)" : ""}`,
    label: (c) => ` ${c.dataset.label}: ${money(c.parsed.y)}`,
  };
  const partialNote = s.some((r) => r.partial);
  $("#trend-legend").insertAdjacentHTML("beforeend", partialNote
    ? `<span style="color:var(--muted)">* partial period: cut off by the date range</span>` : "");
  if (!empty) {
    drawChart("trend-chart", {
      type: "line",
      data: {
        labels: s.map((r) => `${bucketLabel(r.bucket, data.granularity)}${r.partial ? "*" : ""}`),
        datasets: series.map((x) => ({
          label: x.label, data: s.map((r) => r[x.key]), borderColor: x.color, backgroundColor: x.color,
          borderWidth: 2, tension: 0.25, pointRadius: s.length > 40 ? 0 : 3, pointHoverRadius: 5,
          pointBorderColor: css("--surface"), pointBorderWidth: 2,
        })),
      },
      options: opts,
    });
  }
  $("#trend-table").innerHTML = `<thead><tr><th>Period</th><th class="num">Transactions</th><th class="num">GTV</th><th class="num">Refunds</th><th class="num">Net value</th><th class="num">Net revenue</th></tr></thead>
    <tbody>${s.map((r) => `<tr><td>${bucketLabel(r.bucket, data.granularity)}${r.partial ? " (partial)" : ""}</td><td class="num">${int.format(r.transactions)}</td>
      <td class="num">${money2(r.gtv)}</td><td class="num">${money2(r.refund_amount)}</td><td class="num">${money2(r.net_transaction_value)}</td>
      <td class="num">${money2(r.net_revenue)}</td></tr>`).join("")}</tbody>`;
}

const STATUS_COLOR = { success: "--good", failed: "--critical", pending: "--warning", cancelled: "--neutral" };
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function renderBreakdowns(b) {
  state.lastBreakdowns = b;
  const st = b.by_status;
  setEmpty("status-chart", !st.length);
  if (st.length) {
    const o = baseOptions();
    o.indexAxis = "y";
    o.scales.x = { ...o.scales.y, ticks: { color: css("--muted"), precision: 0 } };
    o.scales.y = { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: css("--ink-2") } };
    o.plugins.tooltip.callbacks = { label: (c) => ` ${int.format(c.parsed.x)} attempts · ${money(st[c.dataIndex].value)}` };
    drawChart("status-chart", {
      type: "bar",
      data: { labels: st.map((r) => cap(r.status)),
              datasets: [{ data: st.map((r) => r.transactions), backgroundColor: st.map((r) => css(STATUS_COLOR[r.status] || "--neutral")),
                           borderRadius: 4, borderSkipped: "start", barThickness: 22 }] },
      options: o,
    });
  }
  const m = b.by_payment_method;
  setEmpty("method-chart", !m.length);
  if (m.length) {
    const o = baseOptions();
    o.indexAxis = "y";
    o.scales.x = { ...o.scales.y, ticks: { ...o.scales.y.ticks, maxRotation: 0, autoSkipPadding: 12 } };
    o.scales.y = { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: css("--ink-2") } };
    o.plugins.tooltip.callbacks = { label: (c) => ` ${money(c.parsed.x)} · ${int.format(m[c.dataIndex].successful)} payments` };
    drawChart("method-chart", {
      type: "bar",
      data: { labels: m.map((r) => r.payment_method === "upi" ? "UPI" : cap(r.payment_method)),
              datasets: [{ data: m.map((r) => r.gtv), backgroundColor: css("--series-1"), borderRadius: 4, borderSkipped: "start", barThickness: 22 }] },
      options: o,
    });
  }
}

// ---------- transactions table ----------
const statusPill = (s) => `<span class="status s-${esc(s)}">${esc(cap(s))}</span>`;
const flagChips = (flags) => flags.map((f) => `<span class="flag">${esc(f.replaceAll("_", " ").toLowerCase())}</span>`).join("");

async function loadTransactions(signal) {
  const t = state.table;
  const [sort, order] = t.sort.split(":");
  $("#t-body").innerHTML = `<tr><td colspan="9"><div class="skeleton" style="height:14px"></div></td></tr>`.repeat(5);
  const data = await api("transactions", { ...state.filters, q: t.q, status: t.status, flagged_only: t.flagged,
    sort, order, page: t.page, page_size: t.pageSize }, signal);
  $("#t-body").innerHTML = data.items.length ? data.items.map((r) => `
    <tr class="clickable" data-id="${esc(r.transaction_id)}" tabindex="0">
      <td class="mono">${esc(r.transaction_id)}</td><td>${dateTime(r.created_at)}</td><td class="mono">${esc(r.customer_id)}</td>
      <td>${esc(r.merchant_id)}</td><td>${esc(r.payment_method === "upi" ? "UPI" : cap(r.payment_method))}</td><td>${statusPill(r.status)}</td>
      <td class="num">${money2(r.amount)}</td><td class="num">${r.refunded_amount ? money2(r.refunded_amount) : "—"}</td>
      <td>${flagChips(r.flags)}</td>
    </tr>`).join("") : `<tr><td colspan="9"><div class="empty">No transactions match these filters</div></td></tr>`;
  const from = data.total ? (data.page - 1) * data.page_size + 1 : 0;
  const to = Math.min(data.page * data.page_size, data.total);
  $("#t-info").textContent = `${int.format(from)}–${int.format(to)} of ${int.format(data.total)}`;
  $("#t-prev").disabled = data.page <= 1;
  $("#t-next").disabled = to >= data.total;
}

async function openDrilldown(id) {
  const dlg = $("#drawer");
  $("#d-title").textContent = id;
  $("#d-body").innerHTML = `<div class="skeleton" style="height:120px"></div>`;
  dlg.showModal();
  try {
    const t = await api(`transactions/${encodeURIComponent(id)}`);
    $("#d-body").innerHTML = `
      <dl class="kv">
        <dt>Status</dt><dd>${statusPill(t.status)}</dd>
        <dt>Amount</dt><dd>${money2(t.amount)}</dd>
        <dt>Platform fee</dt><dd>${money2(t.fee_amount)}</dd>
        <dt>Refunded</dt><dd>${money2(t.refunded_amount)}${t.refunded_amount >= t.amount ? " (full)" : t.refunded_amount > 0 ? " (partial)" : ""}</dd>
        <dt>Date (IST)</dt><dd>${dateTime(t.created_at)}</dd>
        <dt>Customer</dt><dd class="mono">${esc(t.customer_id)}</dd>
        <dt>Merchant</dt><dd>${esc(t.merchant_id)}</dd>
        <dt>Method</dt><dd>${esc(t.payment_method)}</dd>
      </dl>
      <div><h3>Refunds</h3>${t.refunds.length ? `<div class="table-wrap"><table><thead><tr><th>Refund</th><th>Date (IST)</th><th>Reason</th><th class="num">Amount</th></tr></thead>
        <tbody>${t.refunds.map((r) => `<tr><td class="mono">${esc(r.refund_id)}</td><td>${dateTime(r.refunded_at)}</td><td>${esc(r.reason || "—")}</td><td class="num">${money2(r.amount)}</td></tr>`).join("")}</tbody></table></div>`
        : `<p class="note">No refunds.</p>`}</div>
      <div><h3>Anomaly flags</h3>${t.flags.length ? t.flags.map((f) => `<p class="note"><span class="sev sev-${esc(f.severity)}">${esc(f.severity)}</span> <strong>${esc(f.rule_code)}</strong>: ${esc(f.explanation)}</p>`).join("")
        : `<p class="note">No anomalies flagged.</p>`}</div>`;
  } catch (e) {
    $("#d-body").innerHTML = `<div class="banner banner-error">${esc(e.message)}</div>`;
  }
}

// ---------- anomalies & data quality ----------
const RULE_TEXT = {
  POSSIBLE_DOUBLE_CHARGE: "Same customer, merchant & amount within 2 min",
  FAILED_ATTEMPT_BURST: "3+ failed attempts within 10 min",
  HIGH_VALUE: "8x+ the merchant's median payment",
  QUICK_FULL_REFUND: "Fully refunded within 1 hour",
};

function renderAnomalies(a) {
  const total = a.summary.reduce((n, r) => n + r.flagged, 0);
  $("#anomaly-count").textContent = total || "";
  $("#a-summary").innerHTML = a.summary.map((r) => `
    <div class="rule"><span class="sev sev-${esc(r.severity)}">${esc(r.severity)}</span>
      <strong>${int.format(r.flagged)}</strong><span>${esc(r.rule_code)}<br>${esc(RULE_TEXT[r.rule_code] || "")}</span></div>`).join("");
  $("#a-body").innerHTML = a.items.length ? a.items.map((r) => `
    <tr class="clickable" data-id="${esc(r.transaction_id)}" tabindex="0">
      <td><span class="sev sev-${esc(r.severity)}">${esc(r.severity)}</span></td><td class="mono">${esc(r.rule_code)}</td>
      <td class="mono">${esc(r.transaction_id)}</td><td>${dateTime(r.created_at)}</td><td class="num">${money2(r.amount)}</td>
      <td class="wrap">${esc(r.explanation)}</td></tr>`).join("")
    : `<tr><td colspan="6"><div class="empty">No anomalies in this range</div></td></tr>`;
}

async function loadQuality() {
  try {
    const q = await api("data-quality");
    const total = q.summary.reduce((n, r) => n + r.issues, 0);
    $("#quality-count").textContent = total || "";
    const run = q.runs[0];
    $("#q-runs").innerHTML = run ? `Last import: <strong>${esc(run.dataset_label)}</strong> · ${int.format(run.rows_received)} rows received,
      ${int.format(run.rows_loaded)} loaded, ${int.format(run.rows_rejected)} rejected · ${dateTime(run.loaded_at)}` : "No imports yet.";
    $("#q-summary").innerHTML = q.summary.map((r) => `<div class="rule"><strong>${int.format(r.issues)}</strong>
      <span class="mono">${esc(r.issue_code)}</span><br><span>${esc(r.source_file)}</span></div>`).join("");
    $("#q-body").innerHTML = q.items.length ? q.items.map((r) => `<tr><td>${esc(r.source_file)}</td><td class="num">${r.row_number ?? "—"}</td>
      <td class="mono">${esc(r.record_id || "—")}</td><td class="mono">${esc(r.issue_code)}</td><td class="wrap">${esc(r.message)}</td></tr>`).join("")
      : `<tr><td colspan="5"><div class="empty">No data-quality issues recorded</div></td></tr>`;
  } catch (e) {
    $("#q-body").innerHTML = `<tr><td colspan="5"><div class="empty">${esc(e.message)}</div></td></tr>`;
  }
}

// ---------- orchestration ----------
async function refresh() {
  writeUrl();
  state.controller?.abort();  // cancel in-flight requests from an older filter state
  const ctrl = (state.controller = new AbortController());
  $("#error").hidden = true;
  renderKpiSkeleton();
  const f = state.filters;
  try {
    const [k, tr, b, an] = await Promise.all([
      api("kpis", f, ctrl.signal),
      api("trends", { ...f, granularity: state.gran }, ctrl.signal),
      api("breakdowns", f, ctrl.signal),
      api("anomalies", f, ctrl.signal),
      loadTransactions(ctrl.signal),
    ]);
    renderKpis(k.kpis);
    renderTrend(tr);
    renderBreakdowns(b);
    renderAnomalies(an);
  } catch (e) {
    if (e.name === "AbortError") return;
    $("#kpis").innerHTML = "";
    showError(e.message);
  }
}

async function refreshTable() {
  try { await loadTransactions(); } catch (e) { if (e.name !== "AbortError") showError(e.message); }
}

function applyPreset(preset) {
  const { min_date, max_date } = state.meta;
  document.querySelectorAll("[data-preset]").forEach((b) => b.classList.toggle("active", b.dataset.preset === preset));
  if (preset === "all") { state.filters.start = min_date; state.filters.end = max_date; }
  else {
    // Presets are anchored to the latest transaction, not today, so the demo dataset stays visible.
    const end = new Date(`${max_date}T00:00:00Z`);
    const start = new Date(end); start.setUTCDate(start.getUTCDate() - Number(preset) + 1);
    state.filters.start = start.toISOString().slice(0, 10);
    state.filters.end = max_date;
    if (state.filters.start < min_date) state.filters.start = min_date;
  }
  $("#f-start").value = state.filters.start;
  $("#f-end").value = state.filters.end;
}

async function init() {
  readUrl();
  renderKpiSkeleton();
  try {
    state.meta = await api("meta");
  } catch (e) {
    $("#kpis").innerHTML = "";
    return showError(e.message);
  }
  const m = state.meta;
  TZ = m.reporting_tz || TZ;
  if (!m.transactions) {
    $("#kpis").innerHTML = `<div class="kpi" style="grid-column:1/-1"><div class="empty">The database has no transactions yet. Run <code>python scripts/load_data.py</code> to import data.</div></div>`;
    return;
  }
  if (m.dataset) {
    $("#dataset").hidden = false;
    $("#dataset-badge").hidden = !m.dataset.is_synthetic;
    $("#dataset-label").textContent = m.dataset.dataset_label;
  }
  $("#f-merchant").insertAdjacentHTML("beforeend", m.merchants.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join(""));
  $("#f-method").insertAdjacentHTML("beforeend", m.payment_methods.map((x) => `<option value="${esc(x)}">${x === "upi" ? "UPI" : esc(cap(x))}</option>`).join(""));
  for (const id of ["#f-start", "#f-end"]) { $(id).min = m.min_date; $(id).max = m.max_date; }

  if (state.filters.start && state.filters.end) {
    $("#f-start").value = state.filters.start;
    $("#f-end").value = state.filters.end;
    document.querySelectorAll("[data-preset]").forEach((b) => b.classList.remove("active"));
  } else applyPreset("all");
  $("#f-merchant").value = state.filters.merchant;
  $("#f-method").value = state.filters.method;
  document.querySelectorAll("[data-gran]").forEach((b) => b.classList.toggle("active", b.dataset.gran === state.gran));

  refresh();
  loadQuality();
}

// ---------- events ----------
document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
  applyPreset(b.dataset.preset); state.table.page = 1; refresh();
}));
for (const [id, key] of [["#f-start", "start"], ["#f-end", "end"], ["#f-merchant", "merchant"], ["#f-method", "method"]]) {
  $(id).addEventListener("change", (e) => {
    if ((key === "start" || key === "end") && !e.target.value) { e.target.value = state.filters[key]; return; }
    state.filters[key] = e.target.value;
    if (key === "start" || key === "end") document.querySelectorAll("[data-preset]").forEach((b) => b.classList.remove("active"));
    if (state.filters.start > state.filters.end) return showError("The start date must be on or before the end date.");
    state.table.page = 1;
    refresh();
  });
}
document.querySelectorAll("[data-gran]").forEach((b) => b.addEventListener("click", async () => {
  state.gran = b.dataset.gran;
  document.querySelectorAll("[data-gran]").forEach((x) => x.classList.toggle("active", x === b));
  writeUrl();
  try { renderTrend(await api("trends", { ...state.filters, granularity: state.gran })); } catch (e) { showError(e.message); }
}));

let searchTimer;
$("#t-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.table.q = e.target.value.trim(); state.table.page = 1; refreshTable(); }, 300);
});
$("#t-status").addEventListener("change", (e) => { state.table.status = e.target.value; state.table.page = 1; refreshTable(); });
$("#t-sort").addEventListener("change", (e) => { state.table.sort = e.target.value; state.table.page = 1; refreshTable(); });
$("#t-flagged").addEventListener("change", (e) => { state.table.flagged = e.target.checked; state.table.page = 1; refreshTable(); });
$("#t-prev").addEventListener("click", () => { state.table.page--; refreshTable(); });
$("#t-next").addEventListener("click", () => { state.table.page++; refreshTable(); });

for (const body of ["#t-body", "#a-body"]) {
  $(body).addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openDrilldown(tr.dataset.id); });
  $(body).addEventListener("keydown", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr && e.key === "Enter") openDrilldown(tr.dataset.id); });
}

document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("[data-tab]").forEach((x) => { x.classList.toggle("active", x === b); x.setAttribute("aria-selected", x === b); });
  document.querySelectorAll(".tab-panel").forEach((p) => { p.hidden = p.id !== `tab-${b.dataset.tab}`; });
}));

// Re-draw charts with the right palette when the OS switches light/dark.
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (state.lastTrend) renderTrend(state.lastTrend);
  if (state.lastBreakdowns) renderBreakdowns(state.lastBreakdowns);
});

window.addEventListener("DOMContentLoaded", () => {
  if (!window.Chart) { showError("Chart library failed to load. Check your connection and retry."); }
  init();
});
