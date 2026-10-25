# Research Question, Hypotheses and Experimental Design

SETU-X · Parv Agarwal, Junior Research Associate (Technical), AI NLP ML Research Laboratory, IIT Patna
Status: Phase 1 design. **Nothing in this document is a result.** The "expected direction" for each experiment is a hypothesis to be tested in Phase 8.

Citation keys refer to `docs/references.bib`. The evidence behind each key is summarised in `docs/literature_review.md`.

---

## 1. What kind of contribution this is

SETU-X is an **experimental teaching and demonstration platform**, with a small, honestly scoped research component. It is not a claim of novel systems research. The pieces it combines already exist separately:

- Microservice benchmarks on real clusters [gan2019deathstar; vonkistowski2018teastore; ueda2016workload].
- Fault and delay injection tools [heorhiadi2016gremlin; meiklejohn2021filibuster; toxiproxy; istiofault].
- Placement studies built on models or simulators [brogi2017fog; motacruz2024placement].
- Methodology guidance for cloud performance experiments [papadopoulos2021methodological; hoefler2015scientific].

What SETU-X adds is an integration that runs on one laptop: real containers, explicit (simulated) environment placement, controlled link delay and faults, configurable resilience policies, and measurement and reporting that follow those methodology principles. It also states explicitly which conclusions a local simulation **cannot** support.

## 2. Research question

> **RQ.** In a containerized microservice application whose services are assigned to heterogeneous, *simulated* cloud environments, how do (a) inter-environment network delay, (b) dependency failures, and (c) client-side resilience policies (bounded timeouts, bounded retries, circuit breaking, and fallback to a replica in another simulated failure domain) affect the distribution of end-to-end latency and the request success rate? Which of these effects can a controlled local simulation establish, and which require measurements on real providers?

### Sub-questions

| ID | Sub-question | Linked hypotheses / experiments |
|---|---|---|
| RQ1 | How does injected delay on one dependency propagate to end-to-end latency, and does the call structure (sequential vs parallel fan-out) change that propagation? | H1 · Exp. A, B |
| RQ2 | What do timeouts, retries and circuit breaking cost and gain when a dependency is slow, failing or unreachable? | H2, H5 · Exp. C, D |
| RQ3 | When does moving a service to another environment reduce end-to-end latency, and when does it not? | H4 · Exp. F |
| RQ4 | How much availability does fallback to a replica in another simulated failure domain recover, and at what latency and recovery-time cost? | H3 · Exp. G |
| RQ5 | Where does this host saturate, and how do latency percentiles behave as load approaches that point? (Exploratory, bounded to the test host) | — · Exp. E |
| RQ6 | Which conclusions transfer to real clouds, and which do not? | Threats to validity; real-cloud roadmap |

## 3. Hypotheses

The hypotheses are falsifiable and phrased with the measurement that would reject them. "Injected delay" means the delay SETU-X's client wrapper adds on purpose (§5.3). "Measured latency" is wall-clock time observed by the load generator.

**H1. Critical-path propagation of delay.**
If an injected delay *d* is added to a dependency on the request's critical path, median end-to-end latency rises by about *d* per sequential traversal. If the dependency is called **in parallel** with another dependency whose latency is ≥ *d*, the median rises by less than *d*.
*Grounding:* fan-out makes service latency depend on the slowest component [dean2013tail]; a single slow dependency can dominate end-to-end tail latency in microservice graphs [gan2019deathstar].
*Rejected if:* in sequential mode the median shift differs from *d* by more than the run-to-run variability of the baseline (95% CI over runs). Or, in parallel mode, the shift is not smaller than in sequential mode.

**H2. Timeouts and circuit breakers trade errors for bounded latency and less load on the failing dependency.**
With an unresponsive dependency:
- (a) Without a timeout, latency is bounded only by the outer client timeout.
- (b) A bounded timeout *T* with *r* retries caps latency at about *T·(1+r)* plus backoff, while producing failed or degraded responses.
- (c) An open circuit breaker reduces both the number of calls reaching the failing dependency and the latency of failed responses (fail fast), compared with timeout alone.

*Grounding:* circuit breaker pattern analysis [montesi2016circuit]; partial-failure handling bugs such as misconfigured timeouts and missing fallbacks [meiklejohn2021filibuster].
*Rejected if:* the measured p99 under timeout-only exceeds the computed bound, or breaker-on does not reduce downstream call counts.

**H3. Fallback across simulated failure domains improves success at a latency cost.**
When the primary inventory replica fails, routing to a secondary replica in another simulated environment raises the success rate compared with no fallback. In return, latency rises by roughly the extra link delay plus failure-detection time. Recovery time depends on the breaker's reset timeout.
*Grounding:* hybrid-cloud and multi-cloud motivation [mell2011nist; petcu2013multicloud; grozev2014intercloud], tempered by evidence that multi-cloud benefits are not automatic [oparamartins2016lockin].
*Caveat stated up front:* both "failure domains" run on one Docker host. They share a kernel, CPU and network, so H3 tests the **application-level** routing logic only, not infrastructure independence.

**H4. Placement benefit depends on the dependency graph.**
Moving a service into the gateway's environment reduces median end-to-end latency only if that service's link delay is on the critical path. With parallel calls, moving the *non-slowest* dependency yields little or no improvement.
*Grounding:* latency-aware placement models [brogi2017fog; motacruz2024placement]; microservice call graphs and dependencies shape performance [luo2021alibaba].
*Rejected if:* moving the non-critical dependency yields a median improvement comparable to moving the critical one.

**H5. Bounded retries help transient failures but amplify load.**
Under probabilistic (transient) errors at rate *p*, *r* retries raise the success rate toward 1 − *p*^(1+*r*) (if failures are independent). They multiply downstream calls by up to (1 + *r*) and increase tail latency.
*Grounding:* hedging and retries add load and need limiting [dean2013tail].
*Rejected if:* measured success deviates from the independence prediction beyond sampling error, or downstream call counts do not increase.

## 4. Experiment-to-hypothesis map

One factor changes at a time unless stated. All experiments run in **simulated multi-environment mode** unless marked otherwise.

| Exp. | Purpose | Independent variable (levels) | Dependent variables | Controlled variables | Expected direction (hypothesis, not result) |
|---|---|---|---|---|---|
| A Baseline | Reference distribution; check measurement overhead | none (all link delays 0, no faults) | latency (mean, median, p90/p95/p99), throughput, error rate | load rate, duration, call mode, image versions | low, stable latency; ~0% errors |
| B Network delay | RQ1/H1 | injected delay on gateway→pricing link: 0, 25, 50, 100 ms; call mode: sequential vs parallel | end-to-end latency distribution; per-dependency latency | load, other links 0 ms, no faults | sequential: median shift ≈ *d*; parallel: shift ≤ *d* |
| C Service failure | RQ2 | primary inventory: healthy → injected 100% errors / hang / container stopped for a bounded window | success rate over time, error types, time to recover | load, policies fixed (timeout on, breaker off) | errors bounded to the fault window; no hangs |
| D Resilience policies | RQ2/H2, H5 | policy: none / timeout / timeout+retry / timeout+retry+breaker | latency, success, downstream call count, breaker transitions | same fault (hang or 50% transient errors), load | H2 and H5 directions above |
| E Workload scaling | RQ5 (exploratory) | offered load steps (e.g. 5 → 80 req/s) | throughput, latency percentiles, errors, container CPU/mem if available | delays 0, no faults | latency rises sharply near saturation **of this host** |
| F Placement | RQ3/H4 | placement of pricing and inventory: co-located with gateway vs distributed | end-to-end latency | link-delay matrix fixed, load, call mode | improvement only when the critical-path service moves |
| G Hybrid fallback | RQ4/H3 | fallback off vs on; primary failed for a bounded window | success rate, latency, recovery time, routing decisions | load, link matrix, breaker settings | fallback on → higher success, higher latency |
| H Cost model (optional) | illustrate cost reasoning | illustrative unit prices (user-supplied) | modeled cost per 1k requests | — | **modeled only**; never presented as provider prices |

## 5. Definitions used throughout

### 5.1 Modes

- `local`: plain containers, no environment labels or link delay.
- `simulated`: containers labelled `local`, `aws-simulated`, `azure-simulated`, `gcp-simulated`. Link delays and faults come from configuration. Every export carries `"environment_mode": "simulated"`.
- `cloud`: optional real-provider deployment, off by default. See `docs/architecture.md` §6.

### 5.2 Unit of replication

The **run** (one experiment repetition with a fresh warm-up) is the unit of replication. Requests within a run are correlated (shared queues, caches, time), so they are not independent replicates [papadopoulos2021methodological; eismann2020microservices]. Confidence intervals for between-condition comparisons are computed over run-level statistics (e.g. per-run medians). Per-run percentiles are never averaged. Pooled percentiles are computed from pooled raw observations and reported alongside the per-run spread.

### 5.3 Injected vs measured delay

Injected delay is a configured parameter and is exported in its own column (`injected_delay_ms`). Measured latency includes injected delay plus real processing, serialization, scheduling and container networking. Analysis reports both, so the "measured overhead" (measured minus injected) is visible.

### 5.4 Metrics (summary; precise formulas in `docs/architecture.md` §8)

Total, successful and failed requests; HTTP status distribution; throughput (successful requests per second over the measurement window); latency mean, median, p90, p95, p99 (p99 only reported when a run has ≥ 1,000 samples, otherwise marked "insufficient samples"); timeout count; retry count; circuit-breaker transitions; per-dependency latency; recovery time (from fault end until the success rate over a sliding 1 s window returns to ≥ 95% of baseline); container CPU and memory when `docker stats` is available, marked "not measured" otherwise.

## 6. What this project will not claim

- That any provider is faster, cheaper or more reliable than another. No comparable real-provider measurements exist in the first release.
- That local Docker measurements represent production cloud performance. Real cloud networks show variability that local runs do not reproduce [uta2020bigdata; leitner2016patterns; schad2010runtime; iosup2011variability].
- That application-level delay injection reproduces real WAN behaviour (jitter, loss, bandwidth limits, congestion). See [jurgelionis2011netem; becker2022emulation] on the limits even of kernel-level emulation.
- That a circuit breaker or health endpoint guarantees availability or correctness.
