// SETU-X dashboard. Talks only to the controller (same origin). No result values are hard-coded:
// everything shown comes from the controller, gateway views, or the files of a finished experiment.
import { api, ApiError, fetchText } from "./api.js";
import { dotPlot, emptyNote, lineChart, timeBars } from "./charts.js";
import { comparabilityWarnings, compareSummaries, fmtMs, fmtNum, fmtPct, parseCsv, perSecond } from "./stats.js";

const $ = (id) => document.getElementById(id);
const state = { mode: null, envs: [], placement: null, links: {}, scenarios: [], experiments: [], running: null,
  pollTimer: null, liveTimer: null, results: new Map(), csv: new Map() };
const FILES = ["observations.csv", "observations.jsonl", "summary.json", "manifest.json", "events.jsonl"];

// ------------------------------------------------------------------------------------------ DOM helpers
function h(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else e.setAttribute(k, String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    e.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  return e;
}

/** Replace an element's children, ignoring null/undefined/false (optional parts of a view). */
function put(id, ...kids) {
  $(id).replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
}

function table(headers, rows, { empty = "Nothing to show yet." } = {}) {
  if (!rows.length) return emptyNote(empty);
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, headers.map((x) => h("th", { scope: "col" }, x)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => h("td", {}, c)))))));
}

function badge(textValue, cls) { return h("span", { class: `badge ${cls}` }, textValue); }

function statusCell(status) {
  const good = ["ready", "completed", "ok", "closed"].includes(status);
  const warn = ["running", "analysing", "half_open", "stopped", "not_configured"].includes(status);
  const cls = good ? "st-good" : warn ? "st-warn" : "st-bad";
  const icon = good ? "✓" : warn ? "◐" : "✕";
  return h("span", { class: `status ${cls}` }, h("span", { class: "st-icon", "aria-hidden": "true" }, icon), ` ${status}`);
}

function showError(where, err) {
  const msg = err instanceof ApiError ? err.describe() : String(err && err.message ? err.message : err);
  const target = where ? $(where) : null;
  if (target) {
    target.replaceChildren(h("div", { class: "error", role: "alert" }, h("strong", {}, "Error: "), msg));
  } else {
    const box = h("div", { class: "error", role: "alert" }, h("strong", {}, "Error: "), msg,
      h("button", { type: "button", class: "close", "aria-label": "dismiss", onclick: () => box.remove() }, "×"));
    $("errors").appendChild(box);
  }
}

function ok(where, message) { $(where).replaceChildren(h("div", { class: "success", role: "status" }, message)); }

async function guarded(where, fn, { clear = true } = {}) {
  try {
    if (where && clear) $(where).replaceChildren();
    return await fn();
  } catch (e) {
    showError(where, e);
    return undefined;
  }
}

function busy(button, on) { button.disabled = on; button.classList.toggle("busy", on); }

// ------------------------------------------------------------------------------------------ tabs
function showTab(name) {
  const known = ["overview", "control", "experiments", "results", "compare", "about"];
  const tab = known.includes(name) ? name : "overview";
  for (const t of known) {
    $(`tab-${t}`).hidden = t !== tab;
    document.querySelector(`[data-tab="${t}"]`).classList.toggle("active", t === tab);
  }
  if (tab === "control") refreshControl();
  if (tab === "experiments") { loadScenarios(); refreshHistory(); }
  if (tab === "results" || tab === "compare") refreshHistory();
}

// ------------------------------------------------------------------------------------------ overview
async function refreshOverview() {
  try {
    const [envs, services, placement] = await Promise.all([
      api("GET", "/api/environments"), api("GET", "/api/services"), api("GET", "/api/placement")]);
    state.mode = envs.mode;
    state.envs = envs.environments;
    state.placement = placement;
    state.links = placement.link_rtt_ms;
    $("conn").textContent = `controller reachable · updated ${new Date().toLocaleTimeString()}`;
    $("conn").className = "conn ok";
    $("mode-badge").textContent = `mode: ${envs.mode.toUpperCase()}`;
    $("mode-badge").className = `badge mode-${envs.mode}`;
    $("sim-notice").hidden = false;
    renderHealth(services.services);
    renderGraph(placement, services.services, envs.mode);
    renderAdapters(envs.environments);
  } catch (e) {
    $("conn").textContent = e instanceof ApiError && e.status ? `controller error (HTTP ${e.status})` : "controller NOT reachable";
    $("conn").className = "conn bad";
    showError("health", e);
  }
}

function renderHealth(rows) {
  put("health", table(
    ["Service", "Instance", "Environment label", "Role", "Enabled", "Readiness", "Probe round trip"],
    rows.map((r) => [r.service, r.instance,
      h("span", {}, r.env, " ", r.simulated ? badge("SIMULATED LABEL", "sim") : null), r.role,
      r.enabled ? "yes" : "no", statusCell(r.status), r.latency_ms === null ? "—" : `${fmtNum(r.latency_ms, 1)} ms`])));
}

function renderAdapters(envs) {
  const rows = envs.filter((e) => e.real_provider_adapter).map((e) => [
    e.provider.toUpperCase(), statusCell(e.real_provider_adapter.status), e.real_provider_adapter.status_detail || ""]);
  put("adapters", 
    h("p", { class: "hint" }, "Real AWS/Azure/GCP integration is not implemented; these adapters only validate configuration. Nothing here creates cloud resources."),
    table(["Provider", "Status", "Detail"], rows));
}

function rtt(links, a, b) {
  if (a === b) return 0;
  for (const [k, v] of Object.entries(links)) {
    const [x, y] = k.split("--");
    if ((x === a && y === b) || (x === b && y === a)) return v;
  }
  return null;
}

function renderGraph(snapshot, services, mode) {
  const NS = "http://www.w3.org/2000/svg";
  const mk = (tag, attrs, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
    if (parent) parent.appendChild(e);
    return e;
  };
  const label = (parent, x, y, s, cls = "g-text", anchor = "middle") => {
    const t = mk("text", { x, y, class: cls, "text-anchor": anchor }, parent);
    t.textContent = s;
    return t;
  };
  const p = snapshot.placement;
  const health = Object.fromEntries(services.map((s) => [s.instance, s.status]));
  const inst = [];
  for (const [svc, cfg] of Object.entries(p.services)) {
    for (const i of cfg.instances) inst.push({ svc, routing: cfg.routing, ...i });
  }
  const W = 760;
  const H = 30 + inst.length * 92;
  const svg = mk("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "Dependency graph: gateway calls inventory and pricing instances" });
  const gx = 40;
  const gy = H / 2 - 32;
  mk("rect", { x: gx, y: gy, width: 170, height: 64, rx: 8, class: `g-node ${health.gateway === "ready" ? "up" : "down"}` }, svg);
  label(svg, gx + 85, gy + 26, "gateway");
  label(svg, gx + 85, gy + 46, `local · ${p.call_mode} calls`, "g-sub");
  inst.forEach((i, k) => {
    const y = 40 + k * 92;
    const x = 470;
    mk("rect", { x: x - 12, y: y - 18, width: 284, height: 76, rx: 10, class: i.env === "local" ? "g-env local" : "g-env" }, svg);
    label(svg, x - 4, y - 4, `${i.env}${i.env.endsWith("-simulated") ? " (label only)" : ""}`, "g-env-label", "start");
    const up = health[i.name] === "ready";
    mk("rect", { x, y: y + 4, width: 260, height: 46, rx: 8, class: `g-node ${up ? "up" : "down"}${i.enabled ? "" : " disabled"}` }, svg);
    label(svg, x + 130, y + 23, `${i.name} · ${i.svc}`);
    label(svg, x + 130, y + 41, `${i.role}${i.enabled ? "" : " · disabled"} · ${up ? "ready" : health[i.name] || "unknown"}`, "g-sub");
    const d = rtt(snapshot.link_rtt_ms, "local", i.env);
    const x1 = gx + 170;
    const y1 = gy + 32;
    const x2 = x;
    const y2 = y + 27;
    mk("path", { d: `M${x1},${y1} C${x1 + 120},${y1} ${x2 - 120},${y2} ${x2},${y2}`,
      class: `g-edge${i.role === "secondary" ? " fallback" : ""}` }, svg);
    const ed = mode === "simulated" ? `${d} ms configured` : "no injected delay (local mode)";
    const lbl = label(svg, x2 - 24, y2 - 8, i.role === "secondary" ? `fallback · ${ed}` : ed, "g-edge-label", "end");
    lbl.setAttribute("paint-order", "stroke");
  });
  put("graph", svg);
  const r = p.resilience;
  put("placement-summary", 
    h("span", {}, `placement v${snapshot.version} (${snapshot.source})`),
    h("span", {}, `timeout ${r.timeout_ms} ms · outer deadline ${r.outer_deadline_ms} ms`),
    h("span", {}, `retries ${r.retries} · breaker ${r.circuit_breaker.enabled ? "on" : "off"} · fallback ${r.fallback ? "on" : "off"}`),
    h("span", {}, `routing: inventory ${p.services.inventory.routing}, pricing ${p.services.pricing.routing}`));
}

// ------------------------------------------------------------------------------------------ control
async function refreshControl() {
  await guarded("placement-msg", async () => {
    const snap = await api("GET", "/api/placement");
    state.placement = snap;
    state.links = snap.link_rtt_ms;
    if (!state.envs.length) state.envs = (await api("GET", "/api/environments")).environments;
    renderPlacementForm(snap);
    renderLinksForm(snap.link_rtt_ms);
  }, { clear: false });
  refreshFaults();
  refreshGateway();
}

function num(name, value, min, max, step = 1) {
  return h("label", {}, name.label, h("input", { name: name.key, type: "number", min, max, step, value }));
}

function renderPlacementForm(snap) {
  const p = snap.placement;
  $("placement-version").textContent = `v${snap.version} · source: ${snap.source} · ${snap.sha256.slice(0, 12)}`;
  const envNames = state.envs.map((e) => e.name);
  const r = p.resilience;
  const instRows = [];
  for (const [svc, cfg] of Object.entries(p.services)) {
    for (const i of cfg.instances) {
      instRows.push(h("tr", { "data-instance": i.name, "data-service": svc },
        h("td", {}, i.name), h("td", {}, svc),
        h("td", {}, h("select", { name: "env", "aria-label": `${i.name} environment` },
          envNames.map((e) => h("option", { value: e, selected: e === i.env }, e)))),
        h("td", {}, h("select", { name: "role", "aria-label": `${i.name} role` },
          ["primary", "secondary"].map((x) => h("option", { value: x, selected: x === i.role }, x)))),
        h("td", {}, h("input", { name: "weight", type: "number", min: 0, max: 100, value: i.weight, "aria-label": `${i.name} weight` })),
        h("td", {}, h("input", { name: "enabled", type: "checkbox", checked: i.enabled, "aria-label": `${i.name} enabled` }))));
    }
  }
  const policy = (svc) => h("label", {}, `${svc} routing`, h("select", { name: `routing_${svc}` },
    ["primary-fallback", "round-robin", "weighted"].map((x) => h("option", { value: x, selected: x === p.services[svc].routing }, x))));
  put("placement-form", 
    h("div", { class: "row" },
      h("label", {}, "Call mode", h("select", { name: "call_mode" },
        ["parallel", "sequential"].map((x) => h("option", { value: x, selected: x === p.call_mode }, x)))),
      policy("inventory"), policy("pricing")),
    h("div", { class: "table-wrap" }, h("table", { class: "edit" },
      h("thead", {}, h("tr", {}, ["Instance", "Service", "Environment label", "Role", "Weight", "Enabled"].map((x) => h("th", {}, x)))),
      h("tbody", {}, instRows))),
    h("div", { class: "row" },
      num({ label: "Timeout per attempt (ms)", key: "timeout_ms" }, r.timeout_ms, 10, 10000),
      num({ label: "Connect timeout (ms)", key: "connect_timeout_ms" }, r.connect_timeout_ms, 10, 10000),
      num({ label: "Outer deadline (ms)", key: "outer_deadline_ms" }, r.outer_deadline_ms, 100, 10000),
      num({ label: "Retries (0–3)", key: "retries" }, r.retries, 0, 3),
      num({ label: "Backoff base (ms)", key: "backoff_base_ms" }, r.backoff.base_ms, 1, 1000),
      num({ label: "Backoff max (ms)", key: "backoff_max_ms" }, r.backoff.max_ms, 1, 5000)),
    h("div", { class: "row" },
      h("label", { class: "check" }, h("input", { name: "fallback", type: "checkbox", checked: r.fallback }), "Fallback to secondary"),
      h("label", { class: "check" }, h("input", { name: "cb_enabled", type: "checkbox", checked: r.circuit_breaker.enabled }), "Circuit breaker"),
      num({ label: "Breaker: consecutive failures", key: "cb_failures" }, r.circuit_breaker.consecutive_failures, 1, 100),
      num({ label: "Breaker: reset timeout (ms)", key: "cb_reset" }, r.circuit_breaker.reset_timeout_ms, 100, 60000)));
}

function readPlacementForm() {
  const f = $("placement-form");
  const p = structuredClone(state.placement.placement);
  const v = (n) => f.querySelector(`[name="${n}"]`);
  p.call_mode = v("call_mode").value;
  for (const svc of Object.keys(p.services)) p.services[svc].routing = v(`routing_${svc}`).value;
  for (const tr of f.querySelectorAll("tr[data-instance]")) {
    const inst = p.services[tr.dataset.service].instances.find((i) => i.name === tr.dataset.instance);
    inst.env = tr.querySelector('[name="env"]').value;
    inst.role = tr.querySelector('[name="role"]').value;
    inst.weight = Number(tr.querySelector('[name="weight"]').value);
    inst.enabled = tr.querySelector('[name="enabled"]').checked;
  }
  const r = p.resilience;
  r.timeout_ms = Number(v("timeout_ms").value);
  r.connect_timeout_ms = Number(v("connect_timeout_ms").value);
  r.outer_deadline_ms = Number(v("outer_deadline_ms").value);
  r.retries = Number(v("retries").value);
  r.backoff = { base_ms: Number(v("backoff_base_ms").value), max_ms: Number(v("backoff_max_ms").value) };
  r.fallback = v("fallback").checked;
  r.circuit_breaker = { enabled: v("cb_enabled").checked, consecutive_failures: Number(v("cb_failures").value),
    reset_timeout_ms: Number(v("cb_reset").value) };
  return p;
}

function renderLinksForm(links) {
  $("links-mode-note").textContent = state.mode === "simulated"
    ? "The stack runs in simulated mode: these delays are injected now."
    : "The stack runs in LOCAL mode: these values are stored but NOT injected until you restart with SETU_MODE=simulated.";
  put("links-form", ...Object.entries(links).map(([k, v]) =>
    h("label", {}, `${k.replace("--", " ↔ ")} (ms)`, h("input", { name: k, type: "number", min: 0, max: 2000, step: 1, value: v }))));
}

async function refreshFaults() {
  await guarded("faults", async () => {
    const data = await api("GET", "/api/faults");
    const sel = $("fault-target");
    const current = sel.value;
    sel.replaceChildren(...data.faults.map((f) => h("option", { value: f.target, selected: f.target === current }, f.target)));
    put("faults", table(["Target", "Reachable", "Active fault", "Remaining", "Requests matched / injected", ""],
      data.faults.map((f) => [f.target, f.reachable ? "yes" : "no",
        f.active ? `${f.active.mode}${f.active.mode === "slow" ? ` +${f.active.delay_ms} ms` : ""} · p=${f.active.probability}` : "none",
        f.active ? `${fmtNum(f.active.remaining_s, 1)} s` : "—",
        f.active ? `${f.active.matched} / ${f.active.injected}` : "—",
        f.active ? h("button", { type: "button", class: "small", onclick: () => clearFault(f.target) }, "Clear") : ""])));
  });
}

async function clearFault(target) {
  await guarded("fault-msg", async () => {
    await api("DELETE", `/api/faults/${encodeURIComponent(target)}`);
    ok("fault-msg", `Fault on ${target} cleared.`);
    refreshFaults();
  });
}

async function refreshGateway() {
  await guarded("decisions", async () => {
    const d = await api("GET", "/api/gateway/routing/decisions?limit=15");
    put("decisions", table(["Time (UTC)", "Service", "Policy", "Chosen", "Env", "Reason", "Candidates"],
      d.decisions.map((x) => [x.ts ? x.ts.slice(11, 23) : "", x.service, x.policy, x.instance, x.env, x.reason,
        (x.candidates || []).join(" → ")]), { empty: "No routing decisions yet. Send a test order." }));
  });
  await guarded("breakers", async () => {
    const b = await api("GET", "/api/gateway/resilience/breakers");
    put("breakers", b.enabled
      ? table(["Instance", "State", "Consecutive failures", "Transitions"],
        b.breakers.map((x) => [x.instance, statusCell(x.state), x.consecutive_failures, x.transitions]),
        { empty: "Breaker enabled; no breaker has been used since the last reset." })
      : emptyNote("Circuit breaker is disabled in the current placement."));
  });
  await guarded("events", async () => {
    const ev = await api("GET", "/api/gateway/resilience/events?limit=20");
    put("events", table(["Time (UTC)", "Type", "Details"],
      ev.events.map((e) => {
        const { seq, ts, type, ...rest } = e;
        return [ts ? ts.slice(11, 23) : "", type, Object.entries(rest).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ")];
      }), { empty: "No retries, fallbacks or breaker events yet." }));
  });
}

async function sendOrder() {
  const f = $("order-form");
  const body = { sku: f.sku.value, quantity: Number(f.quantity.value), customer_tier: f.customer_tier.value };
  const btn = $("order-send");
  busy(btn, true);
  const t0 = performance.now();
  try {
    const r = await api("POST", "/api/gateway/orders", body);
    const browserMs = performance.now() - t0;
    const inv = r.inventory || {};
    const res = r.resilience || {};
    put("order-result", h("div", { class: "card" },
      h("div", {}, h("strong", {}, `${r.status.toUpperCase()}`), ` · order ${r.order_id} · request ${r.request_id}`),
      h("div", { class: "kv" },
        h("span", {}, `inventory: ${inv.instance} (${inv.env}) · available ${inv.available}`),
        h("span", {}, r.pricing ? `price: ${r.pricing.total_cents} ${r.pricing.currency} via ${r.pricing.price_source}` : "no price (rejected)"),
        h("span", {}, `gateway total (measured): ${fmtMs(r.timings_ms.total)}`),
        h("span", {}, `injected delay on critical path (configured): ${fmtMs(res.injected_delay_ms_critical_path)}`),
        h("span", {}, `attempts ${res.attempts} · retries ${res.retries} · fallbacks ${res.fallbacks}`),
        h("span", {}, `browser round trip via controller: ${fmtMs(browserMs)} (host clock, includes the proxy hop)`))));
    refreshGateway();
  } catch (e) {
    showError("order-result", e);
    refreshGateway();
  } finally {
    busy(btn, false);
  }
}

// ------------------------------------------------------------------------------------------ experiments
async function loadScenarios() {
  await guarded("exp-msg", async () => {
    if (!state.mode) state.mode = (await api("GET", "/api/environments")).mode;
    const data = await api("GET", "/api/scenarios");
    state.scenarios = data.scenarios;
    const sel = $("scenario-select");
    const current = sel.value;
    sel.replaceChildren(...data.scenarios.map((s) => h("option", { value: s.name, selected: s.name === current, disabled: !s.valid },
      s.valid ? `${s.name} — ${s.title} (${s.requires_mode})` : `${s.name} — INVALID`)));
    renderScenarioDetail();
  });
}

function currentScenario() { return state.scenarios.find((s) => s.name === $("scenario-select").value); }

function renderScenarioDetail() {
  const s = currentScenario();
  if (!s) { put("scenario-detail", emptyNote("No scenario files found in configs/scenarios.")); return; }
  const mismatch = s.requires_mode !== state.mode;
  put("scenario-detail", 
    mismatch ? h("div", { class: "error" }, `This scenario needs SETU_MODE=${s.requires_mode}, but the stack runs in ${state.mode} mode. Restart the stack in ${s.requires_mode} mode first (see docs/testing_on_windows.md §5).`) : null,
    s.comment ? h("pre", { class: "comment" }, s.comment) : null,
    table(["Condition", "Rate (req/s)", "Call mode", "Changes", "Faults (measurement time, configured)"],
      s.condition_detail.map((c) => [c.name, fmtNum(c.rate_rps, 1), c.call_mode || "(file)",
        [Object.keys(c.resilience).length ? `resilience ${JSON.stringify(c.resilience)}` : "",
          Object.keys(c.instance_env).length ? `placement ${JSON.stringify(c.instance_env)}` : "",
          Object.keys(c.links).length ? `links ${JSON.stringify(c.links)}` : ""].filter(Boolean).join("; ") || "—",
        c.faults.length ? c.faults.map((f) => `${f.mode} on ${f.target} at ${f.at_s}–${f.at_s + f.duration_s} s${f.probability < 1 ? ` p=${f.probability}` : ""}`).join("; ") : "none"])));
  const f = $("exp-form");
  f.rate_rps.placeholder = s.rate_rps;
  f.warmup_s.placeholder = s.warmup_s;
  f.duration_s.placeholder = s.duration_s;
  f.runs.placeholder = s.runs;
  put("override-links", ...(s.requires_mode === "simulated"
    ? Object.keys(state.links).map((k) => h("label", {}, `${k.replace("--", " ↔ ")} default (ms)`,
      h("input", { name: `link:${k}`, type: "number", min: 0, max: 2000, step: 1,
        placeholder: s.default_links[k] ?? state.links[k] })))
    : [h("p", { class: "hint" }, "Link-delay overrides apply to simulated-mode scenarios only.")]));
  updateEstimate();
}

function readOverrides() {
  const f = $("exp-form");
  const o = {};
  for (const k of ["rate_rps", "warmup_s", "duration_s", "runs"]) {
    if (f[k].value !== "") o[k] = Number(f[k].value);
  }
  const links = {};
  for (const inp of f.querySelectorAll('input[name^="link:"]')) {
    if (inp.value !== "") links[inp.name.slice(5)] = Number(inp.value);
  }
  if (Object.keys(links).length) o.links = links;
  return o;
}

function updateEstimate() {
  const s = currentScenario();
  if (!s) return;
  const o = readOverrides();
  const runs = o.runs ?? s.runs;
  const per = (o.warmup_s ?? s.warmup_s) + (o.duration_s ?? s.duration_s) + 1 + 1.5;
  const total = s.conditions.length * runs * per;
  $("estimate").textContent = `${s.conditions.length} conditions × ${runs} runs ≈ ${Math.round(total / 60 * 10) / 10} min `
    + "(the runner refuses scenarios longer than 2 h or faster than 100 req/s).";
}

async function startExperiment() {
  const s = currentScenario();
  if (!s) return;
  const body = { scenario: s.name, label: $("exp-form").label.value.trim() };
  const o = readOverrides();
  if (Object.keys(o).length) body.overrides = o;
  const btn = $("exp-start");
  busy(btn, true);
  try {
    const r = await api("POST", "/api/experiments", body);
    ok("exp-msg", `Started ${r.experiment_id} (${r.progress.total_runs} runs).`);
    watchExperiment(r.experiment_id);
  } catch (e) {
    showError("exp-msg", e);
  } finally {
    busy(btn, false);
  }
}

function watchExperiment(id) {
  state.running = id;
  $("live-panel").hidden = false;
  $("exp-stop").disabled = false;
  clearInterval(state.liveTimer);
  const tick = async () => {
    try {
      const live = await api("GET", `/api/experiments/${encodeURIComponent(id)}/live`);
      renderLive(live);
      if (["completed", "failed", "stopped"].includes(live.status)) {
        clearInterval(state.liveTimer);
        $("exp-stop").disabled = true;
        state.running = null;
        const info = await api("GET", `/api/experiments/${encodeURIComponent(id)}`);
        const valid = info.validation ? (info.validation.ok ? "records validated" : "VALIDATION PROBLEMS") : "";
        const msg = `Experiment ${id} ${info.status}${info.error ? `: ${info.error}` : ""}. ${valid}`;
        if (info.status === "failed") showError("exp-msg", new Error(msg)); else ok("exp-msg", msg);
        $("exp-msg").appendChild(h("button", { type: "button", onclick: () => openResults(id) }, "Open results"));
        refreshHistory();
      }
    } catch (e) {
      showError("live-label", e);
    }
  };
  tick();
  state.liveTimer = setInterval(tick, 1000);
}

function renderLive(live) {
  const p = live.progress;
  const pct = p.scheduled_requests ? Math.round((100 * p.completed_requests) / p.scheduled_requests) : 0;
  put("live-progress", 
    h("div", { class: "kv" },
      h("span", {}, `${live.experiment_id}`), h("span", {}, `status: ${live.status}`),
      h("span", {}, `run ${p.run}/${p.total_runs}`), h("span", {}, `condition: ${p.condition ?? "—"}`),
      h("span", {}, `phase: ${p.phase ?? "—"}`), h("span", {}, `requests ${p.completed_requests}/${p.scheduled_requests}`)),
    h("progress", { max: 100, value: pct, "aria-label": "current run progress" }));
  $("live-label").textContent = live.label;
  const run = live.run;
  const bands = run ? run.faults.map((f) => ({ from: f.at_s, to: f.at_s + f.duration_s, label: `${f.mode} on ${f.target} (configured)` })) : [];
  timeBars($("live-counts"), {
    seconds: live.seconds.map((s) => ({ ...s, warmup: s.t < 0 })),
    keys: [{ key: "ok", label: "success", cls: "s1" }, { key: "degraded", label: "degraded", cls: "st-warn-fill" },
      { key: "failed", label: "failed", cls: "st-bad-fill" }, { key: "dropped", label: "dropped by generator", cls: "muted-fill" }],
    yLabel: "requests per second", xLabel: "scheduled send time (s from start of measurement; < 0 = warm-up)", bands,
    describe: "Live requests per second by outcome",
  });
  lineChart($("live-latency"), {
    points: live.seconds.map((s) => ({ t: s.t, y: s.p50_ms })), yLabel: "median latency (ms)",
    xLabel: "scheduled send time (s from start of measurement)", bands, fmt: (v) => `${fmtNum(v, 0)}`,
    describe: "Live median latency per second",
  });
  put("live-runs", table(["Run", "Condition", "Success", "Availability", "Median latency", "Measured rows"],
    live.completed_runs.map((r) => [r.run_id, r.condition, fmtPct(r.success_rate), fmtPct(r.availability), fmtMs(r.median_latency_ok_ms), r.measure_rows]),
    { empty: "No run finished yet." }));
}

async function stopExperiment() {
  if (!state.running) return;
  const btn = $("exp-stop");
  busy(btn, true);
  try {
    await api("POST", `/api/experiments/${encodeURIComponent(state.running)}/stop`, undefined, { timeoutMs: 60000 });
    ok("exp-msg", "Stop requested; completed runs are kept.");
  } catch (e) {
    showError("exp-msg", e);
  } finally {
    busy(btn, false);
  }
}

async function refreshHistory() {
  await guarded("history", async () => {
    const data = await api("GET", "/api/experiments");
    state.experiments = data.experiments;
    const running = data.experiments.find((e) => ["running", "analysing"].includes(e.status));
    if (running && state.running !== running.experiment_id) watchExperiment(running.experiment_id);
    put("history", table(["Experiment", "Scenario", "Status", "Started (UTC)", "Label", ""],
      data.experiments.map((e) => [e.experiment_id, e.scenario, statusCell(e.status), (e.started_at || "").slice(0, 19).replace("T", " "),
        e.label || "", ["completed", "stopped", "failed"].includes(e.status)
          ? h("button", { type: "button", class: "small", onclick: () => openResults(e.experiment_id) }, "Results") : ""]),
      { empty: "No experiments yet. Results appear here only after you run one; the dashboard never shows example data." }));
    fillExperimentSelects();
  });
}

function fillExperimentSelects() {
  const done = state.experiments.filter((e) => ["completed", "stopped", "failed"].includes(e.status));
  for (const id of ["result-select", "cmp-a", "cmp-b"]) {
    const sel = $(id);
    const current = sel.value;
    sel.replaceChildren(h("option", { value: "" }, done.length ? "— choose —" : "no finished experiments"),
      ...done.map((e) => h("option", { value: e.experiment_id, selected: e.experiment_id === current },
        `${e.experiment_id}${e.label ? ` · ${e.label}` : ""} (${e.status})`)));
  }
}

// ------------------------------------------------------------------------------------------ results
const METRICS = [
  { key: "median_latency_ok_ms", label: "Median latency of successful requests (ms)", fmt: (v) => fmtNum(v, 1) },
  { key: "p95_latency_ok_ms", label: "p95 latency of successful requests (ms; needs ≥ 200 per run)", fmt: (v) => fmtNum(v, 1) },
  { key: "p99_latency_ok_ms", label: "p99 latency of successful requests (ms; needs ≥ 1,000 per run)", fmt: (v) => fmtNum(v, 1) },
  { key: "success_rate", label: "Success rate (fraction of scheduled requests)", fmt: (v) => fmtNum(v, 3), yMax: 1 },
  { key: "availability", label: "Availability (success + degraded, fraction)", fmt: (v) => fmtNum(v, 3), yMax: 1 },
  { key: "throughput_ok_rps", label: "Throughput of successful requests (req/s)", fmt: (v) => fmtNum(v, 1) },
  { key: "retries", label: "Retries per run (count)", fmt: (v) => fmtNum(v, 0) },
  { key: "fallback_requests", label: "Requests served by fallback per run (count)", fmt: (v) => fmtNum(v, 0) },
  { key: "recovery_s", label: "Recovery time after the fault (s)", fmt: (v) => fmtNum(v, 2) },
];

async function loadExperiment(id) {
  if (state.results.has(id)) return state.results.get(id);
  const base = `/api/experiments/${encodeURIComponent(id)}/files`;
  const [summary, manifest] = await Promise.all([api("GET", `${base}/summary.json`), api("GET", `${base}/manifest.json`)]);
  const entry = { summary, manifest };
  state.results.set(id, entry);
  return entry;
}

function openResults(id) {
  location.hash = "#results";
  fillExperimentSelects();
  $("result-select").value = id;
  renderResults();
}

async function renderResults() {
  const id = $("result-select").value;
  for (const pid of ["result-charts", "result-timeline-panel", "result-export"]) $(pid).hidden = true;
  if (!id) { put("result-head", emptyNote("Choose a finished experiment.")); return; }
  await guarded("result-head", async () => {
    const { summary, manifest } = await loadExperiment(id);
    const v = summary.validation || {};
    put("result-head", 
      h("div", { class: "kv" },
        h("span", {}, `${summary.experiment_id}`), h("span", {}, `scenario ${manifest.scenario_id}`),
        statusCell(summary.status), h("span", {}, `finished ${(manifest.finished_at || "").slice(0, 19).replace("T", " ")} UTC`),
        h("span", {}, `mode ${manifest.environment_mode}`),
        h("span", {}, manifest.git_commit === "unknown"
          ? "commit not recorded (started without scripts/run_experiment.ps1)" : `commit ${manifest.git_commit}`),
        h("span", {}, `runs ${summary.runs_completed}/${summary.runs_planned}`),
        h("span", {}, `window ${summary.warmup_s} s warm-up + ${summary.measurement_window_s} s`),
        v.ok ? badge("RECORDS VALIDATED", "measured") : badge("VALIDATION PROBLEMS", "warn")),
      manifest.overrides && Object.keys(manifest.overrides).length
        ? h("p", { class: "hint" }, `Run with overrides: ${JSON.stringify(manifest.overrides)}`) : null,
      manifest.label ? h("p", { class: "hint" }, `Label: ${manifest.label}`) : null,
      (v.problems || []).length ? h("div", { class: "error" }, `Validation: ${v.problems.join("; ")}`) : null,
      (summary.warnings || []).length ? h("div", { class: "warnbox" }, h("strong", {}, "Warnings recorded during the experiment"),
        h("ul", {}, summary.warnings.map((w) => h("li", {}, w)))) : null,
      h("details", {}, h("summary", {}, "Measurement notes"), h("ul", {}, (summary.notes || []).map((n) => h("li", {}, n)))));
    if (!summary.per_run || !summary.per_run.length) {
      $("result-head").appendChild(emptyNote("This experiment has no completed runs, so there is nothing to plot."));
      return;
    }
    $("result-charts").hidden = false;
    $("result-timeline-panel").hidden = false;
    $("result-export").hidden = false;
    const msel = $("metric-select");
    const available = METRICS.filter((m) => Object.values(summary.conditions).some((c) => c.run_level[m.key]));
    const cur = msel.value;
    msel.replaceChildren(...available.map((m) => h("option", { value: m.key, selected: m.key === cur }, m.label)));
    renderMetricChart();
    renderResultTable(summary);
    put("result-comparisons", table(["Condition", "Metric", "Difference of medians (condition − reference)", "95% CI", "Runs"],
      summary.comparisons.results.map((c) => [c.condition, c.metric, fmtNum(c.difference_of_medians, 3),
        c.ci95 ? `${fmtNum(c.ci95[0], 3)} … ${fmtNum(c.ci95[1], 3)}` : "— (needs ≥ 2 runs each)", `${c.n_condition} vs ${c.n_reference}`]),
      { empty: "Only one condition, so there is nothing to compare." }),
      h("p", { class: "hint" }, `Reference: ${summary.comparisons.reference}. ${summary.comparisons.method}.`));
    put("run-select", ...manifest.runs.map((r) => h("option", { value: r.run_id }, `${r.run_id} (${r.rate_rps} req/s)`)));
    put("timeline-counts");
    put("timeline-latency");
    put("export-links", ...FILES.map((f) =>
      h("a", { class: "button", href: `/api/experiments/${encodeURIComponent(id)}/files/${f}`, download: `${id}-${f}` }, `Download ${f}`)));
  });
}

function seriesFor(summary, metric) {
  const pts = {};
  for (const [name, c] of Object.entries(summary.conditions)) {
    const m = c.run_level[metric];
    if (!m) continue;
    pts[name] = { values: m.per_run, median: m.median, ci: m.median_ci95 };
  }
  return pts;
}

async function renderMetricChart() {
  const id = $("result-select").value;
  if (!id) return;
  const { summary } = await loadExperiment(id);
  const m = METRICS.find((x) => x.key === $("metric-select").value) || METRICS[0];
  const cats = Object.keys(summary.conditions);
  dotPlot($("metric-chart"), { categories: cats, series: [{ name: id, cls: "s1", points: seriesFor(summary, m.key) }],
    yLabel: m.label.replace(/ \(.*?; .*?\)/, ""), fmt: m.fmt, yMax: m.yMax ?? null,
    describe: `${m.label} by condition, one dot per run` });
}

function renderResultTable(summary) {
  const injected = {};
  for (const r of summary.per_run) (injected[r.condition] ||= []).push(r.injected_delay_ms_mean);
  const rows = Object.entries(summary.conditions).map(([name, c]) => {
    const rl = c.run_level;
    const ci = rl.median_latency_ok_ms.median_ci95;
    const inj = (injected[name] || []).filter((x) => x !== null);
    return [name, c.runs, fmtMs(rl.median_latency_ok_ms.median), ci ? `${fmtNum(ci[0], 1)}–${fmtNum(ci[1], 1)} ms` : "—",
      rl.p95_latency_ok_ms.median === null ? "insufficient samples" : fmtMs(rl.p95_latency_ok_ms.median),
      fmtPct(rl.success_rate.median), fmtPct(rl.availability.median), `${fmtNum(rl.throughput_ok_rps.median, 1)} req/s`,
      inj.length ? `${fmtNum(inj[0], 0)} ms (configured)` : "—",
      rl.recovery_s ? (rl.recovery_s.median === null ? "not recovered" : `${fmtNum(rl.recovery_s.median, 2)} s`) : "—"];
  });
  put("result-table", table(["Condition", "Runs", "Median latency (median of runs)", "95% CI", "p95 (median of runs)",
    "Success", "Availability", "Throughput", "Injected delay", "Recovery"], rows));
}

async function loadTimeline() {
  const id = $("result-select").value;
  const runId = $("run-select").value;
  if (!id || !runId) return;
  const btn = $("timeline-load");
  busy(btn, true);
  await guarded("timeline-counts", async () => {
    if (!state.csv.has(id)) {
      state.csv.set(id, parseCsv(await fetchText(`/api/experiments/${encodeURIComponent(id)}/files/observations.csv`)));
    }
    const rows = state.csv.get(id).filter((r) => r.run_id === runId);
    const { manifest } = await loadExperiment(id);
    const run = manifest.runs.find((r) => r.run_id === runId);
    const bands = (run.faults || []).filter((f) => f.ok).map((f) => ({
      from: f.actual_measure_offset_s, to: f.actual_measure_offset_s + f.spec.duration_s,
      label: `${f.spec.mode} on ${f.target} (configured window, actual start)` }));
    const sec = perSecond(rows);
    timeBars($("timeline-counts"), { seconds: sec,
      keys: [{ key: "ok", label: "success", cls: "s1" }, { key: "degraded", label: "degraded", cls: "st-warn-fill" },
        { key: "failed", label: "failed", cls: "st-bad-fill" }, { key: "dropped", label: "dropped by generator", cls: "muted-fill" }],
      yLabel: "requests per second", xLabel: "scheduled send time (s from start of measurement; < 0 = warm-up)", bands,
      describe: `Requests per second by outcome for ${runId}` });
    lineChart($("timeline-latency"), { points: sec.map((s) => ({ t: s.t, y: s.p50_ms })),
      yLabel: "median latency per second (ms)", xLabel: "scheduled send time (s from start of measurement)", bands,
      fmt: (v) => fmtNum(v, 0), describe: `Median latency per second for ${runId}` });
  });
  busy(btn, false);
}

// ------------------------------------------------------------------------------------------ compare
async function runCompare() {
  const a = $("cmp-a").value;
  const b = $("cmp-b").value;
  if (!a || !b) { showError("cmp-warnings", new Error("Choose two finished experiments.")); return; }
  if (a === b) { showError("cmp-warnings", new Error("Choose two different experiments.")); return; }
  await guarded("cmp-warnings", async () => {
    const [ea, eb] = await Promise.all([loadExperiment(a), loadExperiment(b)]);
    const warn = comparabilityWarnings(
      { ...ea.manifest, status: ea.summary.status }, { ...eb.manifest, status: eb.summary.status });
    put("cmp-warnings", warn.length
      ? h("div", { class: "warnbox" }, h("strong", {}, "Read with care: "), warn.join("; "), ".")
      : h("div", { class: "success" }, "Same scenario file, mode, code version and overrides."));
    const m = METRICS.find((x) => x.key === $("cmp-metric").value) || METRICS[0];
    const cats = [...new Set([...Object.keys(ea.summary.conditions), ...Object.keys(eb.summary.conditions)])];
    dotPlot($("cmp-chart"), { categories: cats,
      series: [{ name: `A: ${a}`, cls: "s1", points: seriesFor(ea.summary, m.key) },
        { name: `B: ${b}`, cls: "s2", points: seriesFor(eb.summary, m.key) }],
      yLabel: m.label.replace(/ \(.*?; .*?\)/, ""), fmt: m.fmt, yMax: m.yMax ?? null,
      describe: `${m.label}: experiment A vs B by condition` });
    const rows = compareSummaries(ea.summary, eb.summary, m.key).map((r) => [r.condition,
      r.a ? `${m.fmt(r.a.median)} ${r.a.median_ci95 ? `[${m.fmt(r.a.median_ci95[0])}, ${m.fmt(r.a.median_ci95[1])}]` : ""} (n=${r.a.n})` : "not in A",
      r.b ? `${m.fmt(r.b.median)} ${r.b.median_ci95 ? `[${m.fmt(r.b.median_ci95[0])}, ${m.fmt(r.b.median_ci95[1])}]` : ""} (n=${r.b.n})` : "not in B",
      r.difference === null ? "—" : m.fmt(r.difference)]);
    put("cmp-table", table(["Condition", "A: median of runs [95% CI]", "B: median of runs [95% CI]", "B − A"], rows),
      h("p", { class: "hint" }, "Intervals are within each experiment (bootstrap over its runs). No significance test is made across experiments; overlapping intervals do not prove equality."));
  });
}

// ------------------------------------------------------------------------------------------ wiring
function wire() {
  window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
  $("placement-apply").addEventListener("click", async () => {
    const btn = $("placement-apply");
    busy(btn, true);
    await guarded("placement-msg", async () => {
      const r = await api("PUT", "/api/placement", readPlacementForm());
      ok("placement-msg", `Placement v${r.version} applied (sha256 ${r.sha256.slice(0, 12)}). The gateway picks it up within its poll interval.`);
      refreshControl();
      refreshOverview();
    });
    busy(btn, false);
  });
  $("placement-reset").addEventListener("click", async () => {
    await guarded("placement-msg", async () => {
      const r = await api("POST", "/api/placement/reset");
      ok("placement-msg", `Reset to the files (v${r.version}).`);
      refreshControl();
      refreshOverview();
    });
  });
  $("links-apply").addEventListener("click", async () => {
    await guarded("links-msg", async () => {
      const links = {};
      for (const inp of $("links-form").querySelectorAll("input")) links[inp.name] = Number(inp.value);
      const r = await api("PUT", "/api/links", { link_rtt_ms: links });
      ok("links-msg", `Link delays applied (v${r.version}).${state.mode === "simulated" ? "" : " Not injected: the stack runs in local mode."}`);
      refreshOverview();
    });
  });
  $("fault-inject").addEventListener("click", async () => {
    const f = $("fault-form");
    const spec = { target: f.target.value, mode: f.mode.value, duration_s: Number(f.duration_s.value),
      probability: Number(f.probability.value) };
    if (f.mode.value === "slow") spec.delay_ms = Number(f.delay_ms.value);
    if (f.seed.value !== "") spec.seed = Number(f.seed.value);
    await guarded("fault-msg", async () => {
      await api("POST", "/api/faults", spec);
      ok("fault-msg", `Injected ${spec.mode} on ${spec.target} for ${spec.duration_s} s.`);
      refreshFaults();
    });
  });
  $("fault-clear-all").addEventListener("click", async () => {
    await guarded("fault-msg", async () => {
      await api("DELETE", "/api/faults");
      ok("fault-msg", "All faults cleared.");
      refreshFaults();
    });
  });
  $("order-send").addEventListener("click", sendOrder);
  $("gw-refresh").addEventListener("click", () => { refreshGateway(); refreshFaults(); });
  $("scenario-select").addEventListener("change", renderScenarioDetail);
  $("exp-form").addEventListener("input", updateEstimate);
  $("exp-start").addEventListener("click", startExperiment);
  $("exp-stop").addEventListener("click", stopExperiment);
  $("history-refresh").addEventListener("click", refreshHistory);
  $("result-select").addEventListener("change", renderResults);
  $("metric-select").addEventListener("change", renderMetricChart);
  $("timeline-load").addEventListener("click", loadTimeline);
  $("cmp-run").addEventListener("click", runCompare);
  put("cmp-metric", ...METRICS.map((m) => h("option", { value: m.key }, m.label)));
}

wire();
showTab(location.hash.slice(1) || "overview");
refreshOverview();
state.pollTimer = setInterval(refreshOverview, 5000);
