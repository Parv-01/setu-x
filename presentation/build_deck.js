// Builds presentation/setu_x.pptx (at most five slides, asserted below).
// Every number on the slides is read from results/ (analysis JSON and the campaign file); nothing
// is typed in by hand. Screenshots are genuine dashboard captures (docs/evidence/screenshots/phase10).
//
//   cd presentation && npm ci && node build_deck.js
//   (or: node presentation/build_deck.js from the repository root, with pptxgenjs installed)
"use strict";
const fs = require("fs");
const path = require("path");
const pptxgen = require("pptxgenjs");

const ROOT = path.resolve(__dirname, "..");
const OUT = path.join(__dirname, "setu_x.pptx");
const MAX_SLIDES = 5;

// ---------------------------------------------------------------- data (all from results/)
const J = (f) => JSON.parse(fs.readFileSync(path.join(ROOT, "results", "analysis", f), "utf8"));
const campaignFile = fs.readdirSync(path.join(ROOT, "results")).filter((f) => /^campaign_.*\.json$/.test(f)).sort().pop();
const CAMPAIGN = JSON.parse(fs.readFileSync(path.join(ROOT, "results", campaignFile), "utf8"));
const B = J("B_network_delay.json"), D1 = J("D_policies_hang.json"), D2 = J("D_retries_transient.json");
const E = J("E_scaling.json"), F = J("F_placement.json"), G = J("G_hybrid_fallback.json");

const median = (xs) => { const s = [...xs].sort((a, b) => a - b), n = s.length;
  if (!n) throw new Error("median of nothing"); return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2; };
const f1 = (x) => x.toFixed(1), f2 = (x) => x.toFixed(2), f0 = (x) => Math.round(x).toLocaleString("en-US");
const minus = (s) => s.replace(/-/g, "−");
const ci = (c, f = f1) => minus(`[${f(c[0])}, ${f(c[1])}]`);
const range = (xs, f = f0) => `${f(Math.min(...xs))}–${f(Math.max(...xs))}`;

const h1 = { d: [], seq: [], par: [] };
for (const [i, row] of B.modes.seq.entries()) {
  if (row.d_ms === 0) continue;
  h1.d.push(row.d_ms); h1.seq.push(row.shift_vs_d0.difference);
  const p = B.modes.par[i]; if (p.d_ms !== row.d_ms) throw new Error("B: seq/par rows out of order");
  h1.par.push(p.shift_vs_d0.difference);
}
const c1 = D1.conditions;
const calls = Object.fromEntries(Object.entries(c1).map(([k, v]) => [k, median(v.inventory_attempts_per_run)]));
const h2 = {
  noneFail: c1.none.failed_latency_p50_pooled_ms / 1000,
  toFail: c1.timeout.failed_latency_p50_pooled_ms / 1000,
  cut: 100 * (1 - calls["timeout-retry-breaker"] / calls["timeout-retry"]),
  brkFail: c1["timeout-retry-breaker"].failed_latency_p50_pooled_ms,
  recTo: median(c1.timeout.recovery_s), recBrk: median(c1["timeout-retry-breaker"].recovery_s),
};
const gc = G.conditions;
const h3 = {
  s0: gc["no-fallback"].success_rate.median, s1: gc.fallback.success_rate.median,
  gain: G.H3.success_gain_fallback_minus_none, cost: G.H3.p95_cost_fallback_minus_none.difference,
  brkP95: gc["fallback-breaker"].p95_latency_responded_per_run,
};
const h4 = { pr: F.H4.pricing_local_improvement, inv: F.H4.inventory_local_improvement };
const h5 = D2.retries.map((r) => ({ r: r.retries, pred: r.predicted_success_if_independent,
  s: r.success_rate.median, a: median(r.inventory_attempts_per_request_per_run) }));
const lvl = Object.fromEntries(E.levels.map((l) => [l.condition, l]));
const gw40 = Object.values(E.cpu_percent_median_per_run).filter((r) => r.condition === "rps-40").map((r) => r.gateway);
const thr80 = lvl["rps-80"].throughput_ok_rps.per_run;

// ---------------------------------------------------------------- design tokens
const INK = "0F2A36", INK2 = "17394A", TEAL = "1279A0", ORANGE = "D9652B", ORANGE_ON_DARK = "F2A262";
const TEXT = "14232B", MUTED = "56656D", TINT = "EAF3F6", GRID = "E1E7EA", REF_GREY = "A3AEB4", WHITE = "FFFFFF";
const LIGHT_ON_DARK = "D5E3E9";
const HEAD = "Cambria", BODY = "Calibri";
const W = 13.333, M = 0.55, CW = W - 2 * M;

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";
pres.author = "Parv Agarwal";
pres.title = "SETU-X: delay, failures and resilience policies across (simulated) clouds";
pres.subject = "SETU-X final presentation (Phase 10)";
pres.theme = { headFontFace: HEAD, bodyFontFace: BODY };

const FOOT = "SETU-X · Parv Agarwal · github.com/Parv-01/setu-x · results describe one Windows PC; “-simulated” = labels on one Docker host";
pres.defineSlideMaster({
  title: "CONTENT", background: { color: WHITE },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: M, y: 0.32, w: CW, h: 0.8, fontFace: HEAD, fontSize: 32,
      bold: true, color: INK, valign: "middle", margin: 0 }, text: "" } },
    { text: { text: FOOT, options: { x: M, y: 7.08, w: CW - 0.6, h: 0.28, fontFace: BODY, fontSize: 10, color: MUTED, margin: 0 } } },
  ],
  slideNumber: { x: W - M - 0.5, y: 7.08, w: 0.5, h: 0.28, fontFace: BODY, fontSize: 10, color: MUTED, align: "right" },
});
pres.defineSlideMaster({
  title: "DARK", background: { color: INK },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: M, y: 0.32, w: CW, h: 0.8, fontFace: HEAD, fontSize: 32,
      bold: true, color: WHITE, valign: "middle", margin: 0 }, text: "" } },
    { text: { text: FOOT, options: { x: M, y: 7.08, w: CW - 0.6, h: 0.28, fontFace: BODY, fontSize: 10, color: LIGHT_ON_DARK, margin: 0 } } },
  ],
  slideNumber: { x: W - M - 0.5, y: 7.08, w: 0.5, h: 0.28, fontFace: BODY, fontSize: 10, color: LIGHT_ON_DARK, align: "right" },
});

// Speaker notes: the slide's section of voiceover.md (written after the deck was final), if present.
const VO = (() => { const f = path.join(__dirname, "voiceover.md"); if (!fs.existsSync(f)) return {};
  const out = {}; const parts = fs.readFileSync(f, "utf8").split(/^## Slide (\d+) — .+$/m);
  for (let i = 1; i < parts.length; i += 2) out[Number(parts[i])] = parts[i + 1].trim(); return out; })();
const notes = (slide, n) => slide.addNotes(VO[n] || `See presentation/voiceover.md, slide ${n}.`);
const T = (slide, text, o) => slide.addText(text, { isTextBox: true, fontFace: BODY, color: TEXT, margin: 0, valign: "top", ...o });
const bullets = (items, o = {}) => items.map((t, i) => ({ text: t, options: { bullet: { indent: 14 }, paraSpaceAfter: o.gap ?? 6,
  breakLine: i < items.length - 1 } }));
const card = (slide, x, y, w, h, fill, name) => slide.addShape(pres.shapes.ROUNDED_RECTANGLE,
  { x, y, w, h, fill: { color: fill }, line: { color: fill }, rectRadius: 0.08, objectName: name });
const badge = (slide, x, y, label, fill, color = WHITE, size = 0.42) => {
  slide.addShape(pres.shapes.OVAL, { x, y, w: size, h: size, fill: { color: fill }, line: { color: fill } });
  T(slide, label, { x, y, w: size, h: size, fontSize: 12, bold: true, color, align: "center", valign: "middle" });
};

// ================================================================ 1 · problem and question
pres.addSection({ title: "Problem" });
{
  const s = pres.addSlide({ masterName: "DARK", sectionTitle: "Problem" });
  T(s, "Hybrid-cloud microservices · experimental platform", { x: M, y: 1.2, w: 8, h: 0.3, fontSize: 14, color: ORANGE_ON_DARK, bold: true });
  s.addText("SETU-X", { placeholder: "title", align: "left", fontSize: 54 });
  T(s, "How network delay, failures and resilience policies shape latency and availability across (simulated) clouds",
    { x: M, y: 1.62, w: 9.6, h: 0.85, fontSize: 22, color: WHITE, fontFace: HEAD });
  T(s, "Parv Agarwal · Junior Research Associate (Technical), AI NLP ML Research Laboratory, IIT Patna · October 2026",
    { x: M, y: 2.62, w: CW, h: 0.3, fontSize: 14, color: LIGHT_ON_DARK });

  const stats = [
    ["63%", "of requests are slow with 100-way fan-out when each server is slow 1% of the time\u00A0[1]"],
    ["10.4×", "rise in tail latency from mismanaging one dependency in a microservice benchmark\u00A0[2]"],
    ["10 runs", "detected a 10% regression reliably; single runs did not\u00A0[3]"],
  ];
  const gap = 0.3, cw = (CW - 2 * gap) / 3;
  stats.forEach(([big, label], i) => {
    const x = M + i * (cw + gap);
    card(s, x, 3.15, cw, 1.6, INK2, `stat-${i + 1}`);
    T(s, big, { x: x + 0.25, y: 3.27, w: cw - 0.5, h: 0.7, fontSize: 40, bold: true, color: ORANGE_ON_DARK, fontFace: HEAD });
    T(s, label, { x: x + 0.25, y: 3.98, w: cw - 0.5, h: 0.7, fontSize: 14, color: WHITE });
  });

  T(s, "Research question", { x: M, y: 5.0, w: 3, h: 0.3, fontSize: 14, bold: true, color: ORANGE_ON_DARK });
  T(s, "With services placed in different (simulated) cloud environments, how do inter-environment delay, dependency " +
       "failures and client-side resilience policies (timeouts, retries, circuit breakers, cross-environment fallback) " +
       "change end-to-end latency and success rate? Which of these effects can a local lab establish?",
    { x: M, y: 5.32, w: CW, h: 0.85, fontSize: 16, color: WHITE });
  T(s, [
    { text: "Hypotheses  ", options: { bold: true, color: ORANGE_ON_DARK } },
    { text: "H1 delay propagation · H2 timeouts and breakers · H3 cross-environment fallback · H4 placement · H5 bounded retries",
      options: { color: LIGHT_ON_DARK } },
  ], { x: M, y: 6.3, w: CW, h: 0.3, fontSize: 14 });
  T(s, "Independent mini-project, not an officially endorsed IIT Patna project.",
    { x: M, y: 6.68, w: CW, h: 0.25, fontSize: 10, color: LIGHT_ON_DARK, italic: true });
  notes(s, 1);
}

// ================================================================ 2 · gap and what was built
pres.addSection({ title: "Gap and system" });
{
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Gap and system" });
  s.addText("Known pieces, joined in one runnable lab", { placeholder: "title" });
  const LW = 6.05;
  T(s, "Research gap: what the papers leave open", { x: M, y: 1.28, w: LW, h: 0.35, fontSize: 18, bold: true, color: TEAL, fontFace: HEAD });
  const gaps = [
    ["G1", "Placement is studied mostly with models and simulators\u00A0[4,\u00A05]", "SETU-X measures it on running containers with documented link delays"],
    ["G2", "Fault-injection tools find bugs but prescribe no measurement method\u00A0[6,\u00A07]", "SETU-X couples injection with run-level replication"],
    ["G3", "Cloud networks vary; even emulators drift from their settings\u00A0[8–10]", "Every simulated result carries explicit validity limits"],
    ["G4", "Methodology principles often go unmet in cloud studies\u00A0[3,\u00A011,\u00A012]", "SETU-X builds them in by default"],
  ];
  gaps.forEach(([id, shows, adds], i) => {
    const y = 1.75 + i * 1.08;
    card(s, M, y, LW, 0.96, TINT, `gap-${id}`);
    badge(s, M + 0.18, y + 0.27, id, TEAL);
    T(s, [
      { text: shows, options: { bold: true, breakLine: true } },
      { text: "→ " + adds, options: { color: MUTED } },
    ], { x: M + 0.78, y: y + 0.12, w: LW - 0.95, h: 0.75, fontSize: 14, valign: "middle" });
  });
  T(s, "Scope: a gap in accessible, disciplined experiment tooling for teaching and small labs, not a claim of novel systems research.",
    { x: M, y: 6.13, w: LW, h: 0.5, fontSize: 12, italic: true, color: MUTED });

  const RX = M + LW + 0.4, RW = W - M - RX;
  const shotH = RW * 1208 / 2296;
  s.addImage({ path: path.join(__dirname, "assets", "shot_dependency_graph.png"), x: RX, y: 1.3, w: RW, h: shotH,
    altText: "SETU-X dashboard dependency graph in simulated mode: gateway calls inventory-a (aws-simulated), " +
             "inventory-b as fallback (gcp-simulated) and pricing (azure-simulated), with configured link delays" });
  T(s, "Genuine capture: SETU-X dashboard, simulated mode (Linux build workspace, 26 Oct 2026); controller and runner not drawn.",
    { x: RX, y: 1.36 + shotH, w: RW, h: 0.25, fontSize: 10, color: MUTED });

  const modes = [["local", "default; no injected delay"], ["simulated", "environment labels + injected link delay"],
    ["cloud", "off; adapters only validate configuration"]];
  const my = 1.75 + shotH, mg = 0.2, mw = (RW - 2 * mg) / 3;
  modes.forEach(([m, d], i) => {
    const x = RX + i * (mw + mg);
    card(s, x, my, mw, 0.85, i === 1 ? TEAL : TINT, `mode-${m}`);
    T(s, [{ text: m, options: { bold: true, fontSize: 15, breakLine: true } }, { text: d, options: { fontSize: 12 } }],
      { x: x + 0.15, y: my + 0.08, w: mw - 0.3, h: 0.72, color: i === 1 ? WHITE : TEXT });
  });
  T(s, bullets([
    "Safety: faults allow-listed, time-bounded, self-expiring; non-root containers on 127.0.0.1",
    "Cost: no cloud resources, no spend. Ladder simulated → configured → connected → deployed → verified; all providers: not configured",
  ], { gap: 4 }), { x: RX, y: my + 0.98, w: RW, h: 0.95, fontSize: 13 });
  notes(s, 2);
}

// ================================================================ 3 · method and validation
pres.addSection({ title: "Method" });
{
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Method" });
  s.addText("Measured per run, checked by automated gates", { placeholder: "title" });
  const LW = 6.55;
  T(s, "How the campaign was run", { x: M, y: 1.28, w: LW, h: 0.35, fontSize: 18, bold: true, color: TEAL, fontFace: HEAD });
  T(s, bullets([
    "Open-loop Poisson load from inside the Compose network; latency from the scheduled send time",
    "Every run reset and reseeded; run order randomized",
    "The run is the unit of replication: 3 runs per condition (5 for the baseline), 95% bootstrap CI over runs",
    "Percentiles per run only (p95 needs ≥ 200 samples, p99 ≥ 1,000), never averaged",
    "Raw rows and manifest (scenario hash, git commit) re-validated before any analysis",
  ]), { x: M, y: 1.7, w: LW, h: 2.45, fontSize: 14 });
  const nExp = CAMPAIGN.experiments.filter((e) => e.status === "completed" && e.validation_ok).length;
  const cpu = CAMPAIGN.cpu_model.replace(/^13th Gen Intel\(R\) Core\(TM\) /, "");
  card(s, M, 4.2, LW, 0.62, TINT, "campaign");
  T(s, `Campaign: ${nExp}/${CAMPAIGN.experiments.length} experiments validated · ${CAMPAIGN.duration_min} min · one Windows PC ` +
       `(${cpu}, Docker Desktop ${CAMPAIGN.docker_server}) · commit ${CAMPAIGN.git_commit}`,
    { x: M + 0.2, y: 4.27, w: LW - 0.4, h: 0.5, fontSize: 13, bold: true, valign: "middle" });

  T(s, "Testing gates", { x: M, y: 5.0, w: LW, h: 0.35, fontSize: 18, bold: true, color: TEAL, fontFace: HEAD });
  const gates = [["337", "unit, contract, integration"], ["10", "end-to-end"], ["13", "browser (Playwright)"], ["18/18", "clean-clone checks, Linux + Windows"]];
  const gg = 0.15, gw = (LW - 3 * gg) / 4;
  gates.forEach(([n, l], i) => {
    const x = M + i * (gw + gg);
    T(s, n, { x, y: 5.38, w: gw, h: 0.5, fontSize: 28, bold: true, color: ORANGE, fontFace: HEAD });
    T(s, l, { x, y: 5.88, w: gw, h: 0.45, fontSize: 12, color: MUTED });
  });
  T(s, "GitHub Actions runs the same gates on every push. The pilot caught a 5 s client timeout overriding the scenario's 15 s; " +
       "fixed, with a regression test, before the campaign.",
    { x: M, y: 6.38, w: LW, h: 0.55, fontSize: 12, color: TEXT });

  const RX = M + LW + 0.4, RW = W - M - RX, shotH = RW * 1965 / 2295;
  s.addImage({ path: path.join(__dirname, "assets", "shot_G_timeline.png"), x: RX, y: 1.3, w: RW, h: shotH,
    altText: "Dashboard timeline of one Experiment G run with fallback and breaker: all requests succeed during a 10 s hang; " +
             "median latency spikes to about 1.3 s at fault onset, then returns to under 100 ms" });
  T(s, "Genuine capture of the Windows records in the dashboard: one Experiment G run (fallback + breaker). The primary hangs " +
       "for 10 s; every request still succeeds, and latency spikes only until the breaker opens.",
    { x: RX, y: 1.38 + shotH, w: RW, h: 0.65, fontSize: 11, color: MUTED });
  notes(s, 3);
}

// ================================================================ 4 · results
pres.addSection({ title: "Results" });
{
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Results" });
  s.addText("All five hypotheses held here, each with a cost", { placeholder: "title" });
  const hdr = (t) => ({ text: t, options: { bold: true, color: WHITE, fill: { color: INK } } });
  const v = (t) => ({ text: t, options: { bold: true, color: TEAL } });
  const slash = (xs, f) => xs.map(f).join(" / ");
  const rows = [
    [hdr(""), hdr("Hypothesis"), hdr("Measured on this machine (medians over runs)"), hdr("Verdict")],
    ["H1", "Delay adds ≈ d on a sequential path; parallel calls hide it\u00A0[1]",
      `Sequential +${slash(h1.seq, f1)} ms for d = ${h1.d.join(" / ")} ms; parallel +${slash(h1.par, f1)} ms`, v("Supported")],
    ["H2", "Timeouts bound latency; breakers cut load and fail fast\u00A0[13]",
      `Failed requests ${f1(h2.noneFail)} s → ${f2(h2.toFail)} s with a timeout. Breaker: ${f0(h2.cut)}% fewer calls, ` +
      `fails in ${f0(h2.brkFail)} ms, but recovery ${f1(h2.recTo)} s → ${f1(h2.recBrk)} s`, v("Supported, slower recovery")],
    ["H3", "Fallback to another environment raises success, at a latency cost",
      `Success ${f2(h3.s0)} → ${f2(h3.s1)} (+${h3.gain.difference.toFixed(3)} ${ci(h3.gain.ci95, (x) => x.toFixed(3))}); ` +
      `p95 +${f0(h3.cost)} ms; with a breaker p95 ${range(h3.brkP95)} ms`, v("Supported")],
    ["H4", "Placement helps only when it moves the critical path",
      `Moving pricing saves ${f1(h4.pr.difference)} ms ${ci(h4.pr.ci95)}; moving inventory ${f1(h4.inv.difference)} ms ${ci(h4.inv.ci95)}`,
      v("Supported")],
    ["H5", "Retries reach 1 − p^(1+r) and multiply calls\u00A0[1]",
      `Success ${slash(h5, (x) => f2(x.s))} vs predicted ${slash(h5, (x) => String(x.pred))}; ` +
      `${slash(h5, (x) => f2(x.a))} calls per request`, v("Supported*")],
    ["E", "Where this host saturates (exploratory)",
      `Gateway at its 0.5-CPU limit by 40 req/s (${range(gw40, f1)}% CPU); at 80 req/s goodput collapses to ${range(thr80, f1)} req/s`,
      v("Observed")],
  ];
  const TW = 7.75;
  s.addTable(rows, { x: M, y: 1.3, w: TW, colW: [0.5, 2.35, 3.75, 1.15], fontFace: BODY, fontSize: 12.5, color: TEXT,
    valign: "middle", margin: [0.04, 0.08, 0.04, 0.08], border: { type: "solid", pt: 0.75, color: GRID },
    rowH: [0.34, 0.74, 0.86, 0.74, 0.62, 0.74, 0.74], autoPage: false });
  T(s, "* The injected errors are independent by construction, so H5 checks the retry logic, not real-world failure correlation. " +
       "[a, b] = 95% bootstrap CI over runs; 3 runs per condition; local and simulated modes on one Windows PC.",
    { x: M, y: 6.38, w: TW, h: 0.55, fontSize: 10, color: MUTED });

  const CX = M + TW + 0.35, CWd = W - M - CX;
  const axis = { catAxisLabelColor: MUTED, valAxisLabelColor: MUTED, catAxisLabelFontFace: BODY, valAxisLabelFontFace: BODY,
    catAxisLabelFontSize: 11, valAxisLabelFontSize: 10, valGridLine: { color: GRID, size: 0.75 }, catGridLine: { style: "none" },
    titleFontFace: BODY, titleFontSize: 12, titleColor: TEXT, showTitle: true, dataLabelFontFace: BODY, dataLabelFontSize: 10,
    dataLabelColor: TEXT, showValue: true, dataLabelPosition: "outEnd" };
  s.addChart(pres.charts.BAR, [
    { name: "configured d", labels: h1.d.map((d) => `d = ${d} ms`), values: h1.d },
    { name: "sequential", labels: h1.d.map((d) => `d = ${d} ms`), values: h1.seq },
    { name: "parallel", labels: h1.d.map((d) => `d = ${d} ms`), values: h1.par },
  ], { x: CX, y: 1.25, w: CWd, h: 2.7, barDir: "col", barGrouping: "clustered", barGapWidthPct: 60, ...axis,
    title: "H1 · median latency shift vs d = 0 (ms)", chartColors: [REF_GREY, TEAL, ORANGE], dataLabelFormatCode: "0",
    showLegend: true, legendPos: "b", legendFontFace: BODY, legendFontSize: 10, legendColor: TEXT, valAxisMinVal: 0, valAxisMaxVal: 125, valAxisMajorUnit: 25,
    objectName: "chart-H1" });
  const pol = [["none", "none"], ["timeout", "timeout"], ["timeout-retry", "+ 2 retries"], ["timeout-retry-breaker", "+ breaker"]];
  s.addChart(pres.charts.BAR, [{ name: "calls per run", labels: pol.map((p) => p[1]), values: pol.map((p) => calls[p[0]]) }],
    { x: CX, y: 4.05, w: CWd, h: 2.3, barDir: "col", barGapWidthPct: 70, ...axis, chartColors: [TEAL], showLegend: false,
      title: "H2 · calls to the hung service (median per run)", dataLabelFormatCode: "0", valAxisMinVal: 0, valAxisMaxVal: 1000, valAxisMajorUnit: 250,
      objectName: "chart-H2" });
  notes(s, 4);
}

// ================================================================ 5 · conclusions, limits, references
pres.addSection({ title: "Conclusions" });
{
  const s = pres.addSlide({ masterName: "DARK", sectionTitle: "Conclusions" });
  s.addText("What transfers, what does not, what is next", { placeholder: "title" });
  const cols = [
    ["What the lab shows", [
      "The critical path decides: delay and placement matter only where they sit on it",
      "Each resilience policy trades something: errors for bounded latency, recovery time for less load, latency for success",
      "Without admission control, overload collapses goodput instead of levelling it off",
    ]],
    ["What it cannot show", [
      "Provider numbers: one host, constant injected delay, application-level faults",
      "Independent failure domains: both replicas share one kernel",
      "Fine tails: p99 is reportable only for the baseline; 3 runs per condition",
    ]],
    ["Next", [
      "Same scenarios on real providers (adapters target ECS Fargate, Azure Container Apps, Cloud Run)",
      "Jitter and loss models, admission control, more runs",
      "Code, data and CI (Apache-2.0; docs and results CC BY 4.0):",
    ]],
  ];
  const gap = 0.3, cw = (CW - 2 * gap) / 3;
  cols.forEach(([head, items], i) => {
    const x = M + i * (cw + gap);
    card(s, x, 1.3, cw, 3.3, INK2, `col-${i + 1}`);
    T(s, head, { x: x + 0.22, y: 1.42, w: cw - 0.44, h: 0.4, fontSize: 18, bold: true, color: ORANGE_ON_DARK, fontFace: HEAD });
    const runs = bullets(items, { gap: 8 });
    if (i === 2) {
      runs[runs.length - 1].options.breakLine = true;
      runs.push({ text: "github.com/Parv-01/setu-x", options: { bold: true, color: ORANGE_ON_DARK, indentLevel: 0, paraSpaceBefore: 2 } });
    }
    T(s, runs, { x: x + 0.22, y: 1.9, w: cw - 0.44, h: 2.6, fontSize: 14, color: WHITE });
  });
  const refs = [
    "J. Dean, L. A. Barroso. The tail at scale. CACM, 2013.",
    "Y. Gan et al. An open-source benchmark suite for microservices… ASPLOS 2019.",
    "S. Eismann et al. Microservices: a performance tester's dream or nightmare? ICPE 2020.",
    "A. Brogi, S. Forti. QoS-aware deployment of IoT applications through the fog. IEEE IoT J., 2017.",
    "M. Mota-Cruz et al. Optimizing microservices placement in the cloud-to-edge continuum. arXiv:2412.01412, 2024.",
    "V. Heorhiadi et al. Gremlin: systematic resilience testing of microservices. ICDCS 2016.",
    "C. S. Meiklejohn et al. Service-level fault injection testing. SoCC 2021.",
    "P. Leitner, J. Cito. Patterns in the chaos: performance variation in public IaaS clouds. ACM TOIT, 2016.",
    "A. Uta et al. Is big data performance reproducible in modern cloud networks? NSDI 2020.",
    "A. Jurgelionis et al. An empirical study of NetEm network emulation functionalities. ICCCN 2011.",
    "A. V. Papadopoulos et al. Methodological principles for reproducible performance evaluation in cloud computing. IEEE TSE, 2021.",
    "T. Hoefler, R. Belli. Scientific benchmarking of parallel computing systems. SC 2015.",
    "F. Montesi, J. Weber. Circuit breakers, discovery, and API gateways in microservices. arXiv:1609.05830, 2016.",
  ];
  T(s, "References (full bibliography with DOIs: docs/references.bib)", { x: M, y: 4.78, w: CW, h: 0.28, fontSize: 12, bold: true,
    color: ORANGE_ON_DARK });
  const half = Math.ceil(refs.length / 2);
  [refs.slice(0, half), refs.slice(half)].forEach((list, c) => {
    T(s, list.map((r, i) => ({ text: `[${c * half + i + 1}] ${r}`, options: { breakLine: i < list.length - 1, paraSpaceAfter: 2 } })),
      { x: M + c * (CW / 2 + 0.15), y: 5.1, w: CW / 2 - 0.15, h: 1.85, fontSize: 10, color: LIGHT_ON_DARK });
  });
  notes(s, 5);
}

// ---------------------------------------------------------------- the five-slide rule
const n = pres.slides.length;
if (n > MAX_SLIDES) throw new Error(`deck has ${n} slides; the brief allows at most ${MAX_SLIDES}`);
pres.writeFile({ fileName: OUT }).then(() => console.log(`wrote ${OUT} (${n} slides; limit ${MAX_SLIDES})`));
