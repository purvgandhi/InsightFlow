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
const ICONS = {
  count: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  wallet: '<path d="M3 7a2 2 0 0 1 2-2h13v4M3 7v10a2 2 0 0 0 2 2h15V9H5a2 2 0 0 1-2-2zM16 14h.01"/>',
  refund: '<path d="M9 14L4 9l5-5M4 9h11a5 5 0 0 1 0 10h-3"/>',
  percent: '<path d="M19 5L5 19M7 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM17 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"/>',
  net: '<path d="M6 4h12M6 9h12M9 4c4 0 6 2 6 5s-2 5-6 5H6l9 7"/>',
  revenue: '<path d="M3 17l6-6 4 4 8-8M15 7h6v6"/>',
  avg: '<path d="M4 20V10M10 20V4M16 20v-8M3 20h18"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
};
const KPI_TILES = [
  { key: "gtv", label: "Gross transaction value", icon: "wallet", fmt: (k) => money(k.gtv), highlight: true,
    hint: () => "Successful payments, before refunds", formula: "SUM(amount) WHERE status = 'success'" },
  { key: "total_transactions", label: "Total transactions", icon: "count", fmt: (k) => int.format(k.total_transactions),
    hint: (k) => `${int.format(k.successful)} successful · ${int.format(k.failed)} failed · ${int.format(k.pending + k.cancelled)} pending/cancelled`,
    formula: "COUNT(*) of all payment attempts in the range, any status" },
  { key: "refund_amount", label: "Refund amount", icon: "refund", fmt: (k) => money(k.refund_amount),
    hint: (k) => `${int.format(k.refunded_transactions)} payments refunded`, formula: "SUM(refund amount) for successful payments made in the range" },
  { key: "refund_rate", label: "Refund rate", icon: "percent", fmt: (k) => pct(k.refund_rate, 2),
    hint: (k) => `By value · ${pct(k.refund_count_rate)} by count`, formula: "Refund amount ÷ GTV" },
  { key: "net_transaction_value", label: "Net transaction value", icon: "net", fmt: (k) => money(k.net_transaction_value),
    hint: () => "GTV minus refunds", formula: "GTV − refund amount" },
  { key: "net_revenue", label: "Net revenue (fees)", icon: "revenue", fmt: (k) => money2(k.net_revenue),
    hint: (k) => `${money(k.fee_revenue)} fees − ${money(k.fee_reversed)} reversed`, formula: "SUM(fee_amount) of successful payments − fees reversed pro-rata on refunds" },
  { key: "aov", label: "Avg transaction value", icon: "avg", fmt: (k) => money2(k.aov),
    hint: () => "Per successful payment", formula: "GTV ÷ number of successful payments" },
  { key: "success_rate", label: "Success rate", icon: "check", fmt: (k) => pct(k.success_rate),
    hint: () => "Pending & cancelled excluded", formula: "successful ÷ (successful + failed)" },
];
const icon = (name) => `<span class="icon" aria-hidden="true"><svg viewBox="0 0 24 24">${ICONS[name]}</svg></span>`;

function renderKpiSkeleton() {
  $("#kpis").setAttribute("aria-busy", "true");
  $("#kpis").innerHTML = KPI_TILES.map((t) => `
    <div class="kpi${t.highlight ? " highlight" : ""}">${icon(t.icon)}<div class="label">${t.label}</div>
      <div class="skeleton" style="height:30px;width:70%"></div><div class="skeleton" style="height:12px;width:50%"></div></div>`).join("");
}
function renderKpis(k) {
  $("#kpis").setAttribute("aria-busy", "false");
  $("#kpis").innerHTML = KPI_TILES.map((t) => `
    <div class="kpi${t.highlight ? " highlight" : ""}">
      ${icon(t.icon)}
      <div class="label">${t.label}<span class="info" title="${esc(t.formula)}" aria-label="Formula: ${esc(t.formula)}">i</span></div>
      <div class="value">${t.fmt(k)}</div>
      <div class="hint">${esc(t.hint(k))}</div>
    </div>`).join("");
  $("#hero-gtv").textContent = money(k.gtv);
  const fmtDay = (d) => new Date(`${d}T00:00:00Z`).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
  $("#hero-range").textContent = `${fmtDay(state.filters.start)} – ${fmtDay(state.filters.end)} · ${int.format(k.successful)} successful payments`;
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

function areaGradient(ctx) {
  const { chart } = ctx;
  if (!chart.chartArea) return "rgba(56, 189, 248, 0.2)";
  const g = chart.ctx.createLinearGradient(0, chart.chartArea.top, 0, chart.chartArea.bottom);
  g.addColorStop(0, "rgba(56, 189, 248, 0.38)");
  g.addColorStop(1, "rgba(56, 189, 248, 0.02)");
  return g;
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
        datasets: series.map((x, i) => ({
          label: x.label, data: s.map((r) => r[x.key]), borderColor: x.color, pointBackgroundColor: x.color,
          // GTV is drawn as a soft light-blue area; refunds and net value stay as lines on the same ₹ axis.
          fill: i === 0 ? "origin" : false,
          backgroundColor: i === 0 ? areaGradient : x.color,
          borderWidth: i === 0 ? 2.5 : 2, tension: 0.35, pointRadius: s.length > 40 ? 0 : 3, pointHoverRadius: 5,
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
  const total = m.reduce((n, r) => n + r.gtv, 0);
  $("#donut-total").textContent = total ? compactInr(total) : "—";
  $(".donut-center").hidden = !m.length;
  setEmpty("method-chart", !m.length);
  $("#method-legend").innerHTML = m.map((r) => `
    <li><i style="background:${methodColor(r.payment_method)}"></i><span>${methodLabel(r.payment_method)}</span>
      <span class="v">${money(r.gtv)}</span><span class="p">${pct(r.gtv / total)}</span></li>`).join("");
  if (m.length) {
    const o = baseOptions();
    delete o.scales;
    o.cutout = "68%";
    o.interaction = { mode: "nearest", intersect: true };
    o.plugins.tooltip.callbacks = { label: (c) => ` ${money(c.parsed)} · ${pct(c.parsed / total)} · ${int.format(m[c.dataIndex].successful)} payments` };
    drawChart("method-chart", {
      type: "doughnut",
      data: { labels: m.map((r) => methodLabel(r.payment_method)),
              datasets: [{ data: m.map((r) => r.gtv), backgroundColor: m.map((r) => methodColor(r.payment_method)),
                           borderColor: css("--surface"), borderWidth: 3, borderRadius: 6, hoverOffset: 6 }] },
      options: o,
    });
  }
}

// Each method keeps its own colour slot regardless of filters (colour follows the entity, not its rank).
const METHOD_SLOT = { upi: "--series-1", card: "--series-2", netbanking: "--series-3", wallet: "--series-4" };
const methodColor = (m) => css(METHOD_SLOT[m] || "--neutral");
const methodLabel = (m) => (m === "upi" ? "UPI" : cap(m));

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
      <td><span class="id-pill">${esc(r.transaction_id)}</span></td><td>${dateTime(r.created_at)}</td><td><span class="id-pill">${esc(r.customer_id)}</span></td>
      <td>${esc(r.merchant_id)}</td><td>${esc(methodLabel(r.payment_method))}</td><td>${statusPill(r.status)}</td>
      <td class="num"><span class="amt-pill">${money2(r.amount)}</span></td><td class="num">${r.refunded_amount ? money2(r.refunded_amount) : "—"}</td>
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
    if (m.dataset.is_synthetic) {
      $("#dataset-badge").className = "badge badge-demo";
      $("#dataset-badge").textContent = "Demo Data";
      $("#btn-reset-demo").hidden = true;
    } else {
      $("#dataset-badge").className = "badge badge-imported";
      $("#dataset-badge").textContent = "Imported Data";
      $("#btn-reset-demo").hidden = false;
    }
    $("#dataset-label").textContent = m.dataset.dataset_label;
  }
  $("#f-merchant").innerHTML = '<option value="">All merchants</option>' + m.merchants.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
  $("#f-method").innerHTML = '<option value="">All payment methods</option>' + m.payment_methods.map((x) => `<option value="${esc(x)}">${x === "upi" ? "UPI" : esc(cap(x))}</option>`).join("");
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

function openTab(name) {
  document.querySelectorAll("[data-tab]").forEach((x) => { const on = x.dataset.tab === name; x.classList.toggle("active", on); x.setAttribute("aria-selected", on); });
  document.querySelectorAll(".tab-panel").forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
}
document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => openTab(b.dataset.tab)));

// Sidebar: highlight the clicked item; items pointing at the records card also switch its tab.
document.querySelectorAll(".nav a").forEach((a) => a.addEventListener("click", () => {
  document.querySelectorAll(".nav a").forEach((x) => x.classList.toggle("active", x === a));
  if (a.dataset.openTab) openTab(a.dataset.openTab);
}));

// Top-bar search drives the transactions table search.
$("#g-search").addEventListener("input", (e) => {
  $("#t-search").value = e.target.value;
  $("#t-search").dispatchEvent(new Event("input"));
});
$("#g-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { openTab("transactions"); $("#records").scrollIntoView(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) { e.preventDefault(); $("#g-search").focus(); }
});

function showBanner(msg, type = "success") {
  const b = $("#banner-notification");
  if (!b) return;
  b.className = `banner banner-${type}`;
  $("#banner-msg").textContent = msg;
  $("#banner-icon").textContent = type === "success" ? "✓" : "✕";
  b.hidden = false;
  setTimeout(() => { b.hidden = true; }, 8000);
}

let currentImportFile = null;

function setupImportModal() {
  const modal = $("#modal-import");
  const openBtn = $("#btn-open-import");
  const closeBtn = $("#modal-import-close");
  const cancelBtn = $("#btn-cancel-import");
  const dropzone = $("#csv-dropzone");
  const fileInput = $("#csv-file-input");
  const execBtn = $("#btn-execute-import");
  const resetBtn = $("#btn-reset-demo");
  const bannerClose = $("#banner-close");

  if (!modal || !openBtn) return;

  if (bannerClose) {
    bannerClose.addEventListener("click", () => {
      const b = $("#banner-notification");
      if (b) b.hidden = true;
    });
  }

  openBtn.addEventListener("click", () => {
    currentImportFile = null;
    fileInput.value = "";
    $("#import-preview-section").hidden = true;
    execBtn.disabled = true;
    modal.showModal();
  });

  const closeModal = () => modal.close();
  closeBtn.addEventListener("click", closeModal);
  cancelBtn.addEventListener("click", closeModal);
  modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });

  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    if (e.dataTransfer.files && e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
  });
  fileInput.addEventListener("change", (e) => {
    if (e.target.files && e.target.files[0]) handleFile(e.target.files[0]);
  });

  async function handleFile(file) {
    if (!file.name.toLowerCase().endsWith(".csv")) {
      showError("Please select a valid .csv file.");
      return;
    }
    if (file.size > 5 * 1024 * 1024) {
      showError("File size exceeds 5MB limit.");
      return;
    }
    currentImportFile = file;
    $("#prev-filename").textContent = file.name;
    $("#prev-status-pill").textContent = "Validating with Pandas...";
    $("#import-preview-section").hidden = false;
    $("#prev-error-box").hidden = true;
    $("#prev-issues-box").hidden = true;
    execBtn.disabled = true;

    const fd = new FormData();
    fd.append("file", file);

    try {
      const res = await fetch("/api/import/preview", { method: "POST", body: fd });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "Validation failed");
      }
      const data = await res.json();
      $("#prev-total").textContent = int.format(data.total_rows);
      $("#prev-valid").textContent = int.format(data.valid_rows);
      $("#prev-rejected").textContent = int.format(data.rejected_rows);

      if (data.missing_columns && data.missing_columns.length > 0) {
        $("#prev-error-box").textContent = `Missing required column(s): ${data.missing_columns.join(", ")}`;
        $("#prev-error-box").hidden = false;
        $("#prev-status-pill").textContent = "Missing Columns";
        execBtn.disabled = true;
      } else if (!data.can_import) {
        $("#prev-error-box").textContent = "No valid rows could be imported. Please inspect the issues below.";
        $("#prev-error-box").hidden = false;
        $("#prev-status-pill").textContent = "Validation Failed";
        execBtn.disabled = true;
      } else {
        $("#prev-status-pill").textContent = `${data.valid_rows} rows ready`;
        execBtn.disabled = false;
      }

      // Render issues
      if (data.issues_sample && data.issues_sample.length > 0) {
        $("#prev-issues-box").hidden = false;
        $("#prev-issues-count").textContent = data.rejected_rows;
        $("#prev-issues-tbody").innerHTML = data.issues_sample.map((iss) => `
          <tr>
            <td class="num">${iss.row_number}</td>
            <td><code>${esc(iss.record_id || "—")}</code></td>
            <td><span class="badge" style="background:#fee2e2;color:#991b1b">${esc(iss.issue_code)}</span></td>
            <td>${esc(iss.message)}</td>
          </tr>
        `).join("");
      }

      // Render preview rows
      if (data.preview_rows && data.preview_rows.length > 0) {
        $("#prev-sample-tbody").innerHTML = data.preview_rows.map((r) => `
          <tr>
            <td><code>${esc(r.transaction_id || "")}</code></td>
            <td>${esc(r.customer_id || "")}</td>
            <td>${esc(r.merchant_id || "")}</td>
            <td class="num">${r.amount ? money(Number(r.amount)) : "—"}</td>
            <td><span class="badge ${badgeClass(r.status)}">${esc(r.status || "")}</span></td>
            <td>${esc(r.payment_method || "")}</td>
            <td>${r.created_at ? dateTime(r.created_at) : "—"}</td>
          </tr>
        `).join("");
      }
    } catch (e) {
      $("#prev-status-pill").textContent = "Error";
      $("#prev-error-box").textContent = e.message;
      $("#prev-error-box").hidden = false;
      execBtn.disabled = true;
    }
  }

  execBtn.addEventListener("click", async () => {
    if (!currentImportFile) return;
    execBtn.disabled = true;
    $("#btn-import-spinner").hidden = false;
    $("#btn-import-text").textContent = "Importing & Computing...";

    const mode = document.querySelector('input[name="import-mode"]:checked')?.value || "replace";
    const fd = new FormData();
    fd.append("file", currentImportFile);
    fd.append("mode", mode);

    try {
      const res = await fetch("/api/import", { method: "POST", body: fd });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "Import execution failed");
      }
      const result = await res.json();
      modal.close();
      const loaded = result.summary?.transactions_inserted ?? result.summary?.rows_loaded ?? 0;
      const issues = result.summary?.issues ?? 0;
      showBanner(`Successfully imported ${int.format(loaded)} transactions (${int.format(issues)} issues/rejected). Dashboard recalculated!`);
      init();
    } catch (e) {
      showError(e.message);
    } finally {
      execBtn.disabled = false;
      $("#btn-import-spinner").hidden = true;
      $("#btn-import-text").textContent = "Import Valid Records";
    }
  });

  resetBtn.addEventListener("click", async () => {
    if (!confirm("Restore the dashboard to the synthetic demo dataset (4,633 transactions)?")) return;
    try {
      const res = await fetch("/api/reset-demo", { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "Reset failed");
      }
      showBanner("Restored synthetic demo dataset (4,633 transactions).");
      init();
    } catch (e) {
      showError(e.message);
    }
  });
}

window.addEventListener("DOMContentLoaded", () => {
  if (!window.Chart) { showError("Chart library failed to load. Check your connection and retry."); }
  setupImportModal();
  init();
});
