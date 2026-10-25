// Small SVG chart builders (no external libraries; CSP allows same-origin scripts only).
// Every chart has axis titles with units, a hover tooltip, and a legend when it shows more than one
// series. Configured values (injected delay, fault windows) are drawn as reference lines/bands and
// labelled "configured", so they are never confused with measurements.
import { niceTicks } from "./stats.js";

const NS = "http://www.w3.org/2000/svg";

function el(tag, attrs = {}, parent = null) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== undefined && v !== null) e.setAttribute(k, String(v));
  }
  if (parent) parent.appendChild(e);
  return e;
}

function text(parent, x, y, str, cls, attrs = {}) {
  const t = el("text", { x, y, class: cls, ...attrs }, parent);
  t.textContent = str;
  return t;
}

function frame(container, { height = 280, marginLeft = 64, marginBottom = 56, marginTop = 16 } = {}) {
  container.replaceChildren();
  container.classList.add("chart");
  const width = 760;
  const m = { top: marginTop, right: 20, bottom: marginBottom, left: marginLeft };
  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", preserveAspectRatio: "xMidYMid meet" },
    container);
  const tip = document.createElement("div");
  tip.className = "tooltip";
  tip.hidden = true;
  container.appendChild(tip);
  return { svg, tip, width, height, m, iw: width - m.left - m.right, ih: height - m.top - m.bottom };
}

function showTip(f, evt, lines) {
  f.tip.replaceChildren(...lines.map((l, i) => {
    const d = document.createElement("div");
    d.textContent = l;
    if (i === 0) d.className = "tip-title";
    return d;
  }));
  f.tip.hidden = false;
  const box = f.svg.getBoundingClientRect();
  const x = evt.clientX - box.left + 14;
  const y = evt.clientY - box.top + 10;
  f.tip.style.left = `${Math.min(x, box.width - 220)}px`;
  f.tip.style.top = `${y}px`;
}

function hideTip(f) { f.tip.hidden = true; }

// Axis ticks are "nice" numbers; show them exactly (no rounding that would repeat or skip values).
function tickLabel(t) { return String(Number(t.toFixed(6))); }

function yAxis(f, lo, hi, label) {
  const ticks = niceTicks(lo, hi, 5);
  const top = ticks[ticks.length - 1];
  const bottom = ticks[0];
  const y = (v) => f.m.top + f.ih - ((v - bottom) / (top - bottom)) * f.ih;
  const g = el("g", { class: "axis" }, f.svg);
  for (const t of ticks) {
    el("line", { x1: f.m.left, x2: f.m.left + f.iw, y1: y(t), y2: y(t), class: t === bottom ? "baseline" : "grid" }, g);
    text(g, f.m.left - 8, y(t) + 4, tickLabel(t), "tick", { "text-anchor": "end" });
  }
  text(g, 16, f.m.top + f.ih / 2, label, "axis-title",
    { transform: `rotate(-90 16 ${f.m.top + f.ih / 2})`, "text-anchor": "middle" });
  return y;
}

function legend(container, items) {
  const lg = document.createElement("div");
  lg.className = "legend";
  for (const it of items) {
    const s = document.createElement("span");
    s.className = "legend-item";
    const sw = document.createElement("i");
    sw.className = `swatch ${it.cls}`;
    s.appendChild(sw);
    s.appendChild(document.createTextNode(it.label));
    lg.appendChild(s);
  }
  container.insertBefore(lg, container.firstChild);
}

/**
 * Run-level dot plot: one column per condition; small dots = per-run values, bar = median of runs,
 * vertical line = bootstrap 95% CI of that median (when available).
 * series: [{name, cls, points: {condition: {values: [..], median, ci: [lo, hi] | null}}}]
 */
export function dotPlot(container, { categories, series, yLabel, fmt = (v) => String(v), yMin = 0, yMax = null,
  refLines = [], describe = null }) {
  const f = frame(container, { marginBottom: categories.length > 5 ? 70 : 52 });
  const all = [];
  for (const s of series) {
    for (const c of categories) {
      const p = s.points[c];
      if (!p) continue;
      all.push(...p.values.filter((v) => v !== null), ...(p.ci || []));
    }
  }
  refLines.forEach((r) => all.push(r.y));
  if (!all.length) {
    container.replaceChildren(emptyNote("No values for this metric (too few samples per run, or no completed runs)."));
    return;
  }
  const hi = yMax ?? Math.max(...all) * 1.08;
  const y = yAxis(f, Math.min(yMin, ...all), hi, yLabel);
  const band = f.iw / categories.length;
  const off = (k) => (series.length === 1 ? 0 : (k - (series.length - 1) / 2) * Math.min(26, band / (series.length + 1)));
  categories.forEach((c, i) => {
    const cx = f.m.left + band * (i + 0.5);
    const lx = cx;
    const ly = f.m.top + f.ih + 16;
    const rotate = categories.length > 5;
    text(f.svg, lx, ly, c, "tick", rotate
      ? { "text-anchor": "end", transform: `rotate(-30 ${lx} ${ly})` } : { "text-anchor": "middle" });
    series.forEach((s, k) => {
      const p = s.points[c];
      if (!p) return;
      const x = cx + off(k);
      const g = el("g", { class: `mark ${s.cls}` }, f.svg);
      if (p.ci) el("line", { x1: x, x2: x, y1: y(p.ci[0]), y2: y(p.ci[1]), class: "ci" }, g);
      p.values.forEach((v, j) => {
        if (v === null) return;
        const jitter = p.values.length > 1 ? (j / (p.values.length - 1) - 0.5) * 8 : 0;
        el("circle", { cx: x + jitter, cy: y(v), r: 4, class: "dot" }, g);
      });
      if (p.median !== null && p.median !== undefined) {
        el("line", { x1: x - 9, x2: x + 9, y1: y(p.median), y2: y(p.median), class: "median" }, g);
      }
      const hit = el("rect", { x: x - 14, y: f.m.top, width: 28, height: f.ih, class: "hit" }, g);
      const lines = [`${c}${series.length > 1 ? ` · ${s.name}` : ""}`,
        `median of runs: ${p.median === null || p.median === undefined ? "—" : fmt(p.median)}`,
        `95% CI (bootstrap over runs): ${p.ci ? `${fmt(p.ci[0])} – ${fmt(p.ci[1])}` : "— (needs ≥ 2 runs)"}`,
        `per run: ${p.values.map((v) => (v === null ? "n/a" : fmt(v))).join(", ")}`];
      hit.addEventListener("mousemove", (e) => showTip(f, e, lines));
      hit.addEventListener("mouseleave", () => hideTip(f));
    });
  });
  for (const r of refLines) {
    el("line", { x1: f.m.left, x2: f.m.left + f.iw, y1: y(r.y), y2: y(r.y), class: "ref" }, f.svg);
    text(f.svg, f.m.left + f.iw - 4, y(r.y) - 5, r.label, "ref-label", { "text-anchor": "end" });
  }
  if (series.length > 1) legend(container, series.map((s) => ({ cls: s.cls, label: s.name })));
  if (describe) f.svg.setAttribute("aria-label", describe);
}

/**
 * Stacked per-second counts. keys: [{key, label, cls}]; bands: [{from, to, label}] (configured
 * fault windows, measurement-time seconds).
 */
export function timeBars(container, { seconds, keys, yLabel, xLabel, bands = [], describe = null }) {
  const f = frame(container, { height: 266, marginTop: bands.length ? 32 : 16 });
  if (!seconds.length) {
    container.replaceChildren(emptyNote("No requests in this window yet."));
    return;
  }
  const t0 = Math.min(...seconds.map((s) => s.t));
  const t1 = Math.max(...seconds.map((s) => s.t)) + 1;
  const x = (t) => f.m.left + ((t - t0) / (t1 - t0)) * f.iw;
  const maxY = Math.max(1, ...seconds.map((s) => keys.reduce((a, k) => a + (s[k.key] || 0), 0)));
  const y = yAxis(f, 0, maxY, yLabel);
  drawBands(f, bands, x, t0, t1);
  const w = Math.max(1, f.iw / (t1 - t0) - 2);
  for (const s of seconds) {
    let acc = 0;
    const g = el("g", {}, f.svg);
    for (const k of keys) {
      const v = s[k.key] || 0;
      if (!v) continue;
      el("rect", { x: x(s.t) + 1, width: w, y: y(acc + v), height: Math.max(0, y(acc) - y(acc + v) - (acc ? 2 : 0)),
        class: `bar ${k.cls}${s.warmup ? " warm" : ""}`, rx: 1.5 }, g);
      acc += v;
    }
    const hit = el("rect", { x: x(s.t), width: w + 2, y: f.m.top, height: f.ih, class: "hit" }, g);
    const lines = [`second ${s.t}${s.warmup ? " (warm-up, excluded from results)" : ""}`,
      ...keys.map((k) => `${k.label}: ${s[k.key] || 0}`)];
    hit.addEventListener("mousemove", (e) => showTip(f, e, lines));
    hit.addEventListener("mouseleave", () => hideTip(f));
  }
  xAxis(f, t0, t1, x, xLabel);
  legend(container, keys.map((k) => ({ cls: k.cls, label: k.label })));
  if (describe) f.svg.setAttribute("aria-label", describe);
}

/** One-series line over time (gaps where y is null). */
export function lineChart(container, { points, yLabel, xLabel, bands = [], refLines = [], fmt = (v) => String(v),
  describe = null }) {
  const f = frame(container, { height: 266, marginTop: bands.length ? 32 : 16 });
  const vals = points.filter((p) => p.y !== null);
  if (!vals.length) {
    container.replaceChildren(emptyNote("No successful requests yet, so no latency to plot."));
    return;
  }
  const t0 = Math.min(...points.map((p) => p.t));
  const t1 = Math.max(...points.map((p) => p.t)) + 1;
  const x = (t) => f.m.left + ((t + 0.5 - t0) / (t1 - t0)) * f.iw;
  const xb = (t) => f.m.left + ((t - t0) / (t1 - t0)) * f.iw;
  const hi = Math.max(...vals.map((p) => p.y), ...refLines.map((r) => r.y)) * 1.1;
  const y = yAxis(f, 0, hi, yLabel);
  drawBands(f, bands, xb, t0, t1);
  let d = "";
  let pen = false;
  for (const p of points) {
    if (p.y === null) { pen = false; continue; }
    d += `${pen ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.y).toFixed(1)}`;
    pen = true;
  }
  el("path", { d, class: "line s1" }, f.svg);
  for (const p of vals) {
    const c = el("circle", { cx: x(p.t), cy: y(p.y), r: 2.5, class: "pt s1" }, f.svg);
    const hit = el("rect", { x: x(p.t) - 6, y: f.m.top, width: 12, height: f.ih, class: "hit" }, f.svg);
    const lines = [`second ${p.t}`, `${yLabel}: ${fmt(p.y)}`];
    hit.addEventListener("mousemove", (e) => { c.classList.add("on"); showTip(f, e, lines); });
    hit.addEventListener("mouseleave", () => { c.classList.remove("on"); hideTip(f); });
  }
  for (const r of refLines) {
    el("line", { x1: f.m.left, x2: f.m.left + f.iw, y1: y(r.y), y2: y(r.y), class: "ref" }, f.svg);
    text(f.svg, f.m.left + f.iw - 4, y(r.y) - 5, r.label, "ref-label", { "text-anchor": "end" });
  }
  xAxis(f, t0, t1, xb, xLabel);
  if (describe) f.svg.setAttribute("aria-label", describe);
}

function drawBands(f, bands, x, t0, t1) {
  for (const b of bands) {
    const a = Math.max(t0, b.from);
    const z = Math.min(t1, b.to);
    if (z <= a) continue;
    el("rect", { x: x(a), width: x(z) - x(a), y: f.m.top, height: f.ih, class: "band" }, f.svg);
    // bracket + label in the strip above the plot, so data marks never hide them
    el("path", { d: `M${x(a)},${f.m.top - 4} V${f.m.top - 10} H${x(z)} V${f.m.top - 4}`, class: "band-bracket" }, f.svg);
    text(f.svg, x(a), f.m.top - 14, b.label, "band-label");
  }
}

function xAxis(f, t0, t1, x, label) {
  const ticks = niceTicks(t0, t1, 8).filter((t) => t >= t0 && t <= t1);
  for (const t of ticks) {
    text(f.svg, x(t), f.m.top + f.ih + 16, String(t), "tick", { "text-anchor": "middle" });
  }
  text(f.svg, f.m.left + f.iw / 2, f.height - 8, label, "axis-title", { "text-anchor": "middle" });
}

export function emptyNote(message) {
  const d = document.createElement("p");
  d.className = "empty";
  d.textContent = message;
  return d;
}
