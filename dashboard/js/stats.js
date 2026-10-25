// Pure helpers used by the dashboard (no DOM). Unit-tested with `node --test dashboard/tests`.
// The dashboard never computes reported results itself: summary.json (computed by the runner from
// the files on disk) is the source for every result chart. These helpers only parse files for the
// per-second timeline view and format numbers.

/** Type-7 (linear interpolation) percentile, identical to setu_x.experiments.metrics.percentile. */
export function percentile(values, q) {
  if (!values.length) throw new Error("percentile of an empty list");
  if (q < 0 || q > 100) throw new Error("q must be in [0, 100]");
  const xs = [...values].sort((a, b) => a - b);
  const h = ((xs.length - 1) * q) / 100;
  const lo = Math.floor(h);
  const hi = Math.min(lo + 1, xs.length - 1);
  return xs[lo] + (h - lo) * (xs[hi] - xs[lo]);
}

/** RFC 4180 CSV parser (quoted fields, doubled quotes, CRLF or LF). Returns an array of objects. */
export function parseCsv(text) {
  const rows = [];
  let field = "";
  let row = [];
  let i = 0;
  let quoted = false;
  while (i < text.length) {
    const c = text[i];
    if (quoted) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
        quoted = false; i++; continue;
      }
      field += c; i++; continue;
    }
    if (c === '"') { quoted = true; i++; continue; }
    if (c === ",") { row.push(field); field = ""; i++; continue; }
    if (c === "\r") { i++; continue; }
    if (c === "\n") { row.push(field); rows.push(row); row = []; field = ""; i++; continue; }
    field += c; i++;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  if (!rows.length) return [];
  const header = rows[0];
  return rows.slice(1).filter((r) => r.length > 1 || r[0] !== "").map((r) => {
    if (r.length !== header.length) throw new Error(`CSV row has ${r.length} fields, header has ${header.length}`);
    const o = {};
    header.forEach((h, k) => { o[h] = r[k]; });
    return o;
  });
}

const OK = new Set(["confirmed", "rejected"]);
const FAILED = new Set(["unavailable", "error", "client_timeout", "client_error"]);

/** Classify an outcome string into ok | degraded | failed | dropped. */
export function outcomeClass(outcome) {
  if (OK.has(outcome)) return "ok";
  if (outcome === "degraded") return "degraded";
  if (outcome === "dropped") return "dropped";
  if (FAILED.has(outcome)) return "failed";
  throw new Error(`unknown outcome '${outcome}'`);
}

/**
 * Per-second buckets of one run's rows (strings from parseCsv), keyed by the scheduled send second
 * relative to the start of the measurement phase. Warm-up rows (negative offsets) are kept and
 * marked so the chart can show them separately.
 */
export function perSecond(rows) {
  const buckets = new Map();
  for (const r of rows) {
    const t = Math.floor(Number(r.measure_offset_s));
    if (!buckets.has(t)) buckets.set(t, { t, ok: 0, degraded: 0, failed: 0, dropped: 0, lat: [] });
    const b = buckets.get(t);
    const cls = outcomeClass(r.outcome);
    b[cls] += 1;
    if (cls === "ok") b.lat.push(Number(r.latency_ms));
  }
  return [...buckets.values()].sort((a, b) => a.t - b.t).map((b) => ({
    t: b.t, warmup: b.t < 0, ok: b.ok, degraded: b.degraded, failed: b.failed, dropped: b.dropped,
    sent: b.ok + b.degraded + b.failed,
    p50_ms: b.lat.length ? percentile(b.lat, 50) : null,
  }));
}

/** Rows of one condition-level metric from two summaries, for the comparison view. */
export function compareSummaries(a, b, metric) {
  const names = [...new Set([...Object.keys(a.conditions || {}), ...Object.keys(b.conditions || {})])];
  return names.map((name) => {
    const ra = a.conditions?.[name]?.run_level?.[metric] ?? null;
    const rb = b.conditions?.[name]?.run_level?.[metric] ?? null;
    const ma = ra?.median ?? null;
    const mb = rb?.median ?? null;
    return {
      condition: name,
      a: ra, b: rb,
      difference: ma !== null && mb !== null ? mb - ma : null,
      inBoth: ra !== null && rb !== null,
    };
  });
}

/** Problems that make two experiments hard to compare (shown as warnings, never hidden). */
export function comparabilityWarnings(ma, mb) {
  const w = [];
  if (ma.scenario_id !== mb.scenario_id) w.push(`different scenarios (${ma.scenario_id} vs ${mb.scenario_id})`);
  if (ma.environment_mode !== mb.environment_mode) {
    w.push(`different modes (${ma.environment_mode} vs ${mb.environment_mode})`);
  }
  if (ma.git_commit !== mb.git_commit) w.push(`different code versions (${ma.git_commit} vs ${mb.git_commit})`);
  if (ma.scenario_sha256 !== mb.scenario_sha256) w.push("scenario files differ (sha256)");
  if (JSON.stringify(ma.overrides || {}) !== JSON.stringify(mb.overrides || {})) w.push("different overrides");
  if (ma.status !== "completed" || mb.status !== "completed") w.push("at least one experiment did not complete");
  return w;
}

export function fmtMs(v) { return v === null || v === undefined ? "—" : `${Number(v).toFixed(1)} ms`; }
export function fmtPct(v) { return v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(1)}%`; }
export function fmtNum(v, digits = 1) { return v === null || v === undefined ? "—" : Number(v).toFixed(digits); }

/** "Nice" axis ticks covering [lo, hi]. */
export function niceTicks(lo, hi, count = 5) {
  if (!(hi > lo)) { hi = lo + 1; }
  const span = hi - lo;
  const step0 = span / count;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
  const start = Math.floor(lo / step) * step;
  const ticks = [];
  for (let v = start; v <= hi + step * 1e-9; v += step) ticks.push(Number(v.toFixed(10)));
  if (ticks[ticks.length - 1] < hi) ticks.push(Number((ticks[ticks.length - 1] + step).toFixed(10)));
  return ticks;
}
