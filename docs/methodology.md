# Measurement and Statistical Methodology

This document is the reference for how SETU-X experiments are run, recorded and analysed (Phase 5). It describes the method only. No results are reported here; results appear in Phase 8 and come only from executed runs whose records are in `results/`.

Code: `src/setu_x/experiments/` (scenario, schedule, loadgen, observation, records, metrics, statistics, runner, cli) and the runner service `src/setu_x/services/experiments/`. Every rule below has a test in `tests/unit/test_experiment_*.py` or `tests/integration/test_{loadgen,experiment_runner,runner_service}.py`.

## 1. Terms

| Term | Meaning |
|---|---|
| scenario | One file in `configs/scenarios/`. It defines a workload, a set of conditions, the number of runs, the run order and a seed. |
| condition | One level of the independent variable, for example "pricing link 50 ms, sequential calls". |
| run | One repetition of one condition: reset, warm-up, then measurement. **The run is the unit of replication.** |
| request (observation) | One scheduled order request inside a run. Requests in the same run share queues, caches and time, so they are not independent replicates [papadopoulos2021methodological; eismann2020microservices]. |
| warm-up | The first `warmup_s` seconds of a run. Recorded with `phase = warmup` and excluded from every metric. |
| measurement window | The `duration_s` seconds after warm-up. Times inside a run are given as `measure_offset_s` (0 = start of measurement; negative = warm-up). |

## 2. Where and how latency is measured

**Clock domain.** The load generator runs in its own container (`runner`) on the Compose network, next to the services. All its timing uses `time.perf_counter` (monotonic). This matters because of Phase 4 finding 6 (`docs/handover.md`): on Docker Desktop the Windows host and the Linux VM that runs the containers are different clock domains, and a host stopwatch read 0.797 s for a call that the gateway's own timer cut at 0.800 s. Measuring from the host would mix the two clocks. The manifest records the clock used. A run started from outside the runner container (the CLI on the host) gets a warning in the manifest.

**Open-loop arrivals.** Send times are computed before the run starts: constant spacing, or Poisson arrivals from a seeded exponential generator. Each request is sent at its scheduled time whether or not earlier requests have finished. A closed-loop client that waits for each response would send less load when the system slows down. That hides exactly the slowdowns an experiment is meant to measure.

**Latency definitions** (per request):

- `latency_ms` = response time − **scheduled** send time. This is the latency an open-loop client experiences. It includes any lag in the generator.
- `service_latency_ms` = response time − **actual** send time.
- `send_lag_ms` = actual − scheduled send time. Reported per run (`send_lag_ms` statistics in `summary.json`), so a generator that falls behind is visible rather than hidden.
- Gateway-reported `gateway_total_ms`, `inventory_ms` and `pricing_ms` are copied from the response. They are measured inside the gateway, around each dependency call including retries.
- `injected_delay_ms` is the configured simulated link delay on the critical path (max over parallel calls, sum over sequential calls). It is a parameter, not a measurement, and is kept in its own column so the measured overhead (measured − injected) can be computed.

**In-flight cap.** At most `max_in_flight` requests may be outstanding (default 64, at most 256). A request that would exceed the cap is recorded as `dropped` with error type `generator_in_flight_cap`. It is never silently skipped, and it counts in the denominator of every rate.

**Client timeout.** `request_timeout_s` (default 10 s) bounds every request at the generator. The gateway's own outer deadline (default 3 s) is normally shorter. A request that hits the client timeout is recorded as `client_timeout` with its censored latency. That latency is excluded from latency statistics but counted as a failure.

## 3. Records

Each experiment writes `results/<experiment_id>/` (named volume `setu-results` in the runner; `scripts/run_experiment.ps1` downloads it into `./results/<experiment_id>/`). The experiment ID is the UTC start time, the scenario ID and a random suffix. The writer refuses to reuse an existing directory.

| File | Content |
|---|---|
| `manifest.json` | Experiment ID, label, status, error, warnings. The scenario as written (text + SHA-256) and as merged. Git commit (passed in by the host script; `-dirty` if `src/`, `configs/` or Compose files have uncommitted changes). SETU-X, Python and package versions; platform; CPUs visible; the runner's CPU limit. Mode; health of gateway and controller at start; base placement and link matrix. The run order with per-run seeds. Then, per completed run: the placement version and SHA-256 actually used, the link matrix, the gateway-reset confirmation, the scheduled faults with their actual injection time and lateness, and run warnings. |
| `observations.jsonl`, `observations.csv` | One row per scheduled request, same columns in the same order (below). |
| `events.jsonl` | Per run: gateway resilience events (retries, fallbacks, breaker transitions and rejections), controller events (placement and link changes, faults injected and cleared), and the runner's fault-scheduling records. Each event is tagged with `run_id` and `source`. |
| `summary.json` | Validation report, per-run metrics, per-condition summaries, comparisons and notes, all computed by re-reading the observation files from disk. |
| `docker_stats.csv` (optional) | Written only by `run_experiment.ps1 -DockerStats`. These are `docker stats` samples taken on the **host** about once per second. They are context only and are labelled host-clock. |

Observation columns: `experiment_id, condition, run_index, run_id, seq, phase, request_id, scheduled_offset_s, measure_offset_s, t_send_utc, send_lag_ms, latency_ms, service_latency_ms, http_status, outcome, error_type, gateway_total_ms, inventory_ms, pricing_ms, inventory_instance, inventory_env, pricing_instance, pricing_env, fallback_used, retries, attempts, breaker_rejections, injected_delay_ms, placement_version`.

`request_id` is `<scenario>-<experiment suffix>-c<condition>-r<run>-<seq>`. It is sent as `X-Request-ID`, so every row can be joined to gateway logs and events. `fallback_used` refers to inventory, the only service with a secondary instance.

**Outcome classes.**

| outcome | HTTP | counted as |
|---|---|---|
| `confirmed` | 200, status confirmed | success |
| `rejected` | 409, insufficient stock (a correct business answer) | success |
| `degraded` | 200, status degraded (list-price fallback) | available, not success |
| `unavailable` | 503 from the gateway | failure |
| `error` | any other status | failure |
| `client_timeout`, `client_error` | none (generator-side) | failure |
| `dropped` | none (not sent) | neither; in denominators |

The default workload orders `SKU-002` (36 in stock) with quantity 1, so every correct answer is `confirmed`. A `rejected` would indicate a functional problem, not load.

**Validation** (`records.validate_records`, run after every experiment and by `cli analyse`) re-reads both observation files and checks:

- The two files agree row by row.
- Request IDs are unique.
- Every completed run has exactly as many rows as were scheduled, and no rows belong to unlisted runs.
- Every row passes the observation schema. The schema enforces, for example, that `confirmed` implies HTTP 200, that `dropped` has no status or latency, and that `phase` matches the sign of `measure_offset_s`.

Rows that fail are listed under `validation.flagged` and excluded from metrics. They are never repaired. `validation.ok` is false if anything is flagged or any check fails. The Phase 5 acceptance script additionally recomputes counts, success rates and medians from `observations.csv` with an independent PowerShell implementation and compares them with `summary.json`.

## 4. Per-run metrics

All of these use measurement-phase rows only.

| Metric | Definition |
|---|---|
| success rate | (confirmed + rejected) ÷ all scheduled measurement requests, dropped included |
| availability | (confirmed + rejected + degraded) ÷ the same denominator |
| failure rate | (unavailable + error + client_timeout + client_error) ÷ the same denominator |
| offered rate | scheduled measurement requests ÷ `duration_s` |
| throughput | successful requests ÷ `duration_s` (bounded by the offered rate in an open-loop test) |
| latency (`latency_ok_ms`) | of successful requests: n, mean, sample standard deviation, min, max, p50, p90, p95, p99 |
| latency of all responses (`latency_responded_ms`) | same statistics over every request that received an HTTP response, failures included |
| per-dependency latency | `inventory_ms` and `pricing_ms` statistics of successful requests |
| retries, fallbacks, breaker rejections | sums of the per-request gateway counters |
| recovery | per fault (below) |

**Percentiles** use linear interpolation between order statistics, type 7 in [hyndman1996sample], which is the NumPy and R default. A percentile *q* is reported only when the run has at least 10 ÷ (1 − *q*) samples, so that at least 10 observations lie above it. That means p90 needs n ≥ 100, p95 needs n ≥ 200 and p99 needs n ≥ 1,000. Otherwise the value is `null` and listed under `insufficient`. Scenario durations are chosen so the percentiles a hypothesis needs are available: Experiment A has 1,200 measured requests per run, enough for p99.

**Recovery time.** Time axis: scheduled send time relative to the start of measurement. An outcome is attributed to the moment its request was issued.

- *baseline* = the success rate of requests sent before the fault.
- The *rolling success rate* at time *t* is the success rate of requests sent in [*t* − *w*, *t*), with *w* = `recovery_window_s` (default 1 s), evaluated every 0.1 s. Time *t* is *healthy* when that rate is ≥ θ × baseline, with θ = `recovery_threshold` (default 0.95). An empty window is not healthy.
- *impacted* is true if any rolling window that overlaps the fault is not healthy. If the fault was fully masked (for example by fallback), `recovery_s` = 0.
- *recovery_s* is the time from fault **end** to the first *t* from which every *t* stays healthy for `recovery_hold_s` (default 3 s). It is `null`, with a note, if this does not happen before the run ends.
- Fault start and end use the **actual** injection time recorded by the runner (fault expiry at the instance is relative to injection), not the scheduled one. The lateness is recorded and a warning is written if it exceeds 100 ms.

Because the rate looks back *w* seconds, `recovery_s` includes up to about *w* of measurement lag. For example, an instantaneous recovery at 10 req/s reads as about 0.9–1.0 s. The parameters are written next to every value.

Recovery depends on traffic (Phase 4 finding 3): a circuit breaker leaves the open state only when a request arrives after its reset timeout. The open-loop generator keeps sending during and after the fault, so recovery is measured under continuous load.

**Resource usage** is not measured inside the runner. The manifest says so. `run_experiment.ps1 -DockerStats` adds host-side `docker stats` samples.

## 5. Across runs and between conditions

- Each run is reduced to run-level statistics: its median latency, its p95 (when n allows), success rate, availability, throughput, retries and so on.
- **Per condition:** for each statistic, the per-run values, the number of runs that have it, and their median, min and max, plus a 95% percentile-bootstrap interval for the median [efron1979bootstrap]. The interval resamples **runs** with replacement (B = 2,000, seeded with the scenario seed, so it is reproducible).
- **Percentiles are never averaged across runs** [promhistograms; papadopoulos2021methodological]. When a single distribution over all runs is useful descriptively, it is computed from the pooled raw rows and labelled `POOLED ... requests are not independent`.
- **Comparisons:** for each condition against the scenario's first condition (the reference), the difference of the medians of run-level statistics, with a 95% bootstrap interval that resamples runs within each condition independently.
- **No p-values.** With the 3–5 runs per condition used here, a bootstrap interval is coarse and tends to be too narrow. It is always shown together with n and the raw per-run values, so the reader can judge it. Hypotheses are evaluated in Phase 8 against the rejection criteria in `docs/research_questions.md` §3, using these intervals and the run-to-run spread of the baseline.

## 6. Isolation, ordering and reproducibility

Before every run, the runner:

1. Resets the controller: placement and link matrix go back to the files, and every fault is cleared (the run fails if a fault cannot be cleared).
2. Applies the condition: a validated `PUT /api/placement` and/or `PUT /api/links`. It checks that the controller's placement SHA-256 equals the one computed from the scenario.
3. Calls `POST /api/resilience/reset {seed}` on the gateway. The gateway fetches the controller placement immediately (rather than waiting for its 2 s poll), resets circuit breakers and routing-policy state, and reseeds its backoff-jitter RNG. The returned placement version and SHA-256 must equal the controller's, or the run is not started.
4. Records the last event sequence numbers, so `events.jsonl` holds exactly that run's events. If an event log wrapped during a run, a warning names the lost range.
5. Generates load. Scheduled faults are injected through the controller's allow-listed fault API at `at_s` seconds into the measurement window. A probabilistic fault without its own seed gets the run seed.
6. Waits for all in-flight requests, clears faults, collects events, appends rows, then cools down for `cooldown_s`.

After each run, every response's `placement_version` is checked against the run's version. A mismatch is a warning in the manifest.

**Order.** The default is `randomized`: the interleaved sequence of (condition, run) pairs, shuffled with the scenario seed. Slow drifts on the host (other programs, thermal state, caches) are then spread across conditions instead of lining up with one of them. `interleaved` and `blocked` exist for debugging.

**Seeds.** Each run has a seed derived from the scenario seed, the condition index and the run index. It drives the Poisson schedule, the gateway's jitter RNG and unseeded probabilistic faults.

What is reproducible is the schedule, the fault timing and the fault decisions, and the configuration of every run (recorded with its SHA-256). Per-request outcomes are reproducible only when requests do not overlap, because concurrent requests interleave nondeterministically at the event loop. Latency values are never reproducible exactly; that is why runs are repeated.

## 7. Safety bounds

A scenario is rejected before any load is sent if any of the following holds:

- the rate exceeds 100 req/s;
- a run lasts longer than 300 s;
- the scenario would run longer than 2 h in total;
- a fault lies outside the measurement window or exceeds the fault limits (duration ≤ 300 s, probability ≤ 1, `slow` ≤ 5 s);
- a fault target is not one of the downstream instances in `configs/placement.yaml`;
- any condition produces a placement or link matrix that the config validators reject.

The runner API only runs files from `configs/scenarios/`, by name, so it cannot be used to send arbitrary load. It runs one experiment at a time, is not published to the host, and is reached through the controller (`127.0.0.1:8090/api/experiments`). A scenario that needs simulated mode is refused on a stack running in local mode, and vice versa; nothing is written in that case.

## 8. Scenarios shipped

| File | Exp. | Mode | Conditions × runs | Load and window |
|---|---|---|---|---|
| `A_baseline.yaml` | A | local | parallel × 5 | 20 req/s Poisson, 5 + 60 s |
| `B_network_delay.yaml` | B (H1) | simulated | {sequential, parallel} × pricing link {0, 25, 50, 100} ms; inventory link fixed at 50 ms; × 3 | 20 req/s, 5 + 30 s |
| `C_service_failure.yaml` | C | local | error vs hang on inventory-a at 10–20 s, fallback off; × 3 | 20 req/s, 5 + 40 s |
| `D_policies_hang.yaml` | D (H2) | local | none / timeout / timeout + retry / timeout + retry + breaker, hang at 5–25 s; × 3 | 10 req/s, 5 + 40 s |
| `D_retries_transient.yaml` | D (H5) | local | retries 0 / 1 / 2 under 50% seeded errors; × 3 | 20 req/s, 5 + 30 s |
| `E_scaling.yaml` | E | local | 5, 10, 20, 40, 80 req/s; × 3 | 5 + 30 s |
| `F_placement.yaml` | F (H4) | simulated | distributed / pricing-local / inventory-local / both-local; × 3 | 20 req/s, 5 + 30 s |
| `G_hybrid_fallback.yaml` | G (H3) | simulated | no fallback / fallback / fallback + breaker, hang at 10–20 s; × 3 | 20 req/s, 5 + 40 s |
| `smoke.yaml`, `smoke_simulated.yaml` | — | local / simulated | acceptance checks only (too short for p95/p99) | 10 req/s |

Two experiments run their variable through a different mechanism than the table in `docs/research_questions.md` might suggest:

- In **F**, "moving" a service relabels the environment of an existing instance. The container does not move; the injected link delay changes.
- In **C**, the "container stopped" variant cannot be driven by the runner, which by design has no Docker access. It is run separately with `scripts/fault_container.ps1` and reported as such.

All simulated link delays are configured values, not measured provider latencies.

## 9. How to run

```powershell
docker compose up -d                                           # local-mode scenarios: A, C, D, E, smoke
pwsh -File scripts/run_experiment.ps1 -Scenario A_baseline     # progress, then results\<id>\
$env:SETU_MODE='simulated'; docker compose up -d               # simulated scenarios: B, F, G, smoke_simulated
pwsh -File scripts/run_experiment.ps1 -Scenario B_network_delay -DockerStats
```

Inside the Compose network without the host script: `docker compose exec runner python -m setu_x.experiments.cli run --scenario configs/scenarios/smoke.yaml`. Re-validate and re-summarise a downloaded directory: `python -m setu_x.experiments.cli analyse results/<id>`.

## 10. Known limitations of the method

- All services share one host, so "environments" share CPU, kernel and network. Results describe this host and these container limits (0.5 CPU per service, 1.0 CPU for the runner), not cloud behaviour.
- The generator and the services share the host's CPUs. At high rates, generator lag (`send_lag_ms`) and service latency can interact. That is why send lag is recorded per run.
- Faults are application-level: health endpoints stay up during `error`, `slow` and `hang`.
- Failure-detection times differ between Docker platforms (Phase 4 finding 1), so absolute timeout and connect-error numbers do not transfer between machines.
- A small number of runs per condition gives coarse intervals. Increase `runs` where a comparison is close to its rejection threshold.
