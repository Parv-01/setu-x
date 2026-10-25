// Run: node --test dashboard/tests/*.test.mjs   (Node 18+; no packages needed)
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  comparabilityWarnings, compareSummaries, fmtMs, fmtPct, niceTicks, outcomeClass, parseCsv, percentile, perSecond,
} from "../js/stats.js";

test("percentile matches type 7 (same values as the Python implementation's tests)", () => {
  assert.equal(percentile([1, 2, 3, 4], 50), 2.5);
  assert.ok(Math.abs(percentile([1, 2, 3, 4], 90) - 3.7) < 1e-12);
  assert.ok(Math.abs(percentile([15, 20, 35, 40, 50], 40) - 29) < 1e-12);
  assert.equal(percentile([7], 99), 7);
  assert.throws(() => percentile([], 50));
  assert.throws(() => percentile([1], 101));
});

test("parseCsv handles quotes, doubled quotes, CRLF and empty fields", () => {
  const rows = parseCsv('a,b,c\r\n1,"x, y",\r\n2,"say ""hi""",z\n');
  assert.deepEqual(rows, [{ a: "1", b: "x, y", c: "" }, { a: "2", b: 'say "hi"', c: "z" }]);
  assert.deepEqual(parseCsv(""), []);
  assert.throws(() => parseCsv("a,b\n1,2,3\n"));
});

test("outcome classes follow the methodology", () => {
  assert.equal(outcomeClass("confirmed"), "ok");
  assert.equal(outcomeClass("rejected"), "ok");
  assert.equal(outcomeClass("degraded"), "degraded");
  assert.equal(outcomeClass("client_timeout"), "failed");
  assert.equal(outcomeClass("dropped"), "dropped");
  assert.throws(() => outcomeClass("bogus"));
});

test("perSecond buckets by scheduled second, keeps warm-up separate, medians of successes only", () => {
  const r = (t, outcome, lat) => ({ measure_offset_s: String(t), outcome, latency_ms: lat === null ? "" : String(lat) });
  const sec = perSecond([r(-0.5, "confirmed", 100), r(0.1, "confirmed", 10), r(0.6, "confirmed", 30),
    r(0.9, "unavailable", 800), r(1.2, "dropped", null), r(1.5, "degraded", 20)]);
  assert.deepEqual(sec.map((s) => s.t), [-1, 0, 1]);
  assert.equal(sec[0].warmup, true);
  assert.deepEqual([sec[1].ok, sec[1].failed, sec[1].sent, sec[1].p50_ms], [2, 1, 3, 20]);
  assert.deepEqual([sec[2].dropped, sec[2].degraded, sec[2].sent, sec[2].p50_ms], [1, 1, 1, null]);
});

test("compareSummaries aligns conditions and reports missing ones", () => {
  const s = (conds) => ({ conditions: Object.fromEntries(conds.map(([n, m]) => [n, { run_level: { x: { median: m } } }])) });
  const rows = compareSummaries(s([["a", 10], ["b", 20]]), s([["a", 15], ["c", 1]]), "x");
  const by = Object.fromEntries(rows.map((r) => [r.condition, r]));
  assert.equal(by.a.difference, 5);
  assert.equal(by.b.inBoth, false);
  assert.equal(by.c.a, null);
});

test("comparability warnings list every difference", () => {
  const base = { scenario_id: "A", environment_mode: "local", git_commit: "abc", scenario_sha256: "1", overrides: {}, status: "completed" };
  assert.deepEqual(comparabilityWarnings(base, { ...base }), []);
  const w = comparabilityWarnings(base, { ...base, scenario_id: "B", environment_mode: "simulated", git_commit: "def",
    scenario_sha256: "2", overrides: { runs: 1 }, status: "stopped" });
  assert.equal(w.length, 6);
});

test("formatting and ticks", () => {
  assert.equal(fmtMs(null), "—");
  assert.equal(fmtMs(12.345), "12.3 ms");
  assert.equal(fmtPct(0.9876), "98.8%");
  const t = niceTicks(0, 97);
  assert.equal(t[0], 0);
  assert.ok(t[t.length - 1] >= 97 && t.length <= 7);
  assert.deepEqual(niceTicks(0, 1, 5), [0, 0.2, 0.4, 0.6, 0.8, 1]);
});
