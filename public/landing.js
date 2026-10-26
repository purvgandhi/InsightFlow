// Landing page: fills the "Live snapshot" card from the same API the dashboard uses.
"use strict";

const $ = (sel) => document.querySelector(sel);
const inr0 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const int = new Intl.NumberFormat("en-IN");
const pct = (x, d = 1) => (x === null || x === undefined ? "—" : `${(x * 100).toFixed(d)}%`);
const monthLabel = (d) => new Date(`${d}T00:00:00Z`).toLocaleDateString("en-IN", { month: "short", timeZone: "UTC" });

async function get(path) {
  const res = await fetch(`/api/${path}`, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

function drawSpark(series) {
  const svg = $("#lp-spark");
  const W = 320, H = 90, pad = 4;
  const vals = series.map((s) => s.gtv);
  const max = Math.max(...vals, 1);
  const x = (i) => (series.length === 1 ? W / 2 : pad + (i * (W - 2 * pad)) / (series.length - 1));
  const y = (v) => H - pad - (v / max) * (H - 2 * pad);
  const pts = series.map((s, i) => `${x(i).toFixed(1)},${y(s.gtv).toFixed(1)}`);
  const ns = "http://www.w3.org/2000/svg";
  svg.innerHTML = "";
  const area = document.createElementNS(ns, "path");
  area.setAttribute("d", `M${x(0)},${H - pad} L${pts.join(" L")} L${x(series.length - 1)},${H - pad} Z`);
  area.setAttribute("fill", "rgba(37, 99, 235, 0.07)");
  const line = document.createElementNS(ns, "polyline");
  line.setAttribute("points", pts.join(" "));
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "#2563eb");
  line.setAttribute("stroke-width", "2");
  line.setAttribute("vector-effect", "non-scaling-stroke");
  line.setAttribute("stroke-linejoin", "round");
  svg.append(area, line);
  // Invisible wide hit targets give each month a native tooltip.
  series.forEach((s, i) => {
    const hit = document.createElementNS(ns, "rect");
    const w = (W - 2 * pad) / Math.max(series.length - 1, 1);
    hit.setAttribute("x", x(i) - w / 2); hit.setAttribute("y", 0);
    hit.setAttribute("width", w); hit.setAttribute("height", H);
    hit.setAttribute("fill", "transparent");
    const t = document.createElementNS(ns, "title");
    t.textContent = `${monthLabel(s.bucket)}${s.partial ? " (partial)" : ""}: ${inr0.format(s.gtv)}`;
    hit.append(t);
    svg.append(hit);
  });
  $("#lp-axis").innerHTML = series.map((s) => `<span>${monthLabel(s.bucket)}</span>`).join("");
}

async function loadSnapshot() {
  try {
    const meta = await get("meta");
    if (!meta.transactions) throw new Error("no data");
    const [k, tr, an, dq] = await Promise.all([
      get("kpis"), get("trends?granularity=month"), get("anomalies"), get("data-quality"),
    ]);
    const kp = k.kpis;
    $("#lp-gtv").textContent = inr0.format(kp.gtv);
    $("#lp-net").textContent = inr0.format(kp.net_revenue);
    $("#lp-refund").textContent = pct(kp.refund_rate, 2);
    $("#lp-success").textContent = pct(kp.success_rate);
    $("#lp-txns").textContent = int.format(kp.total_transactions);
    $("#lp-flags").textContent = int.format(an.summary.reduce((n, r) => n + r.flagged, 0));
    $("#lp-rejected").textContent = int.format(dq.runs[0]?.rows_rejected ?? 0);
    if (meta.dataset) {
      $("#lp-badge").hidden = false;
      $("#lp-badge").textContent = meta.dataset.is_synthetic ? "Demo Data" : "Imported Data";
      $("#lp-badge").className = `badge ${meta.dataset.is_synthetic ? "badge-demo" : "badge-imported"}`;
    }
    const fmt = (d) => new Date(`${d}T00:00:00Z`).toLocaleDateString("en-IN", { month: "short", year: "numeric", timeZone: "UTC" });
    $("#lp-range").textContent = `${fmt(meta.min_date)} – ${fmt(meta.max_date)}`;
    if (tr.series.length) drawSpark(tr.series);
  } catch {
    $("#lp-error").hidden = false;
  }
}

loadSnapshot();
