# SETU-X voiceover

Script for `presentation/setu_x.pptx` (five slides). It targets 5–7 minutes at a normal speaking pace of about 140–150 words per minute; the word counts are checked by `presentation/check_voiceover.py`. The same text is in each slide's speaker notes. Every number spoken here appears on the slide it belongs to, and comes from `results/analysis/`.

## Slide 1 — SETU-X

Hello, I'm Parv Agarwal, a Junior Research Associate in the AI NLP ML Research Laboratory at IIT Patna. This is SETU-X, a mini-project I built independently. It is not an official IIT Patna project.

When an application's services run in different clouds, every call between them adds delay and one more way to fail. Three findings from the literature show why that matters. With a hundred-way fan-out, if each server is slow just one percent of the time, sixty-three percent of requests end up slow. In a microservice benchmark, mismanaging a single dependency raised tail latency more than ten times. And a single run of an experiment could not reliably detect a ten percent regression, while ten runs could.

So my question is this. When services sit in different, simulated, cloud environments, how do delay, failures and resilience policies such as timeouts, retries, circuit breakers and fallback change latency and success rate? And which of those effects can a small local lab actually establish? I turned this into five hypotheses, H1 to H5.

## Slide 2 — Known pieces, joined in one runnable lab

The building blocks already exist, but separately. Placement has mostly been studied with models and simulators. Fault-injection tools such as Gremlin and Filibuster are built to find bugs, not to measure trade-offs. Real cloud networks vary, and even network emulators drift from their own settings. And methodology papers show that cloud studies often skip basic rules, such as repeated runs.

SETU-X joins these pieces in one runnable lab. On the right is the real dashboard. A gateway calls an inventory service and a pricing service, each labelled as if it lived on AWS, Google Cloud or Azure, with a second inventory replica for fallback. These are labels on one Docker host, and the gateway injects the configured link delays. There are three modes: local, simulated, and cloud, which stays switched off. The cloud adapters only check configuration, so nothing is created and nothing costs money. Faults are allow-listed, time-limited and expire on their own.

## Slide 3 — Measured per run, checked by automated gates

Numbers are only useful if the measurement is honest, so the harness follows published methodology rules. The load is open-loop: requests go out on schedule whether or not earlier ones have finished, so a slow system cannot hide its own slowness. Every run starts from a reset, with a fresh seed, in random order. The run, not the single request, is the unit of replication. Confidence intervals are bootstrapped over runs, and percentiles are never averaged.

I ran the full campaign on my own Windows PC: eight experiments in about sixty-five minutes, with every record validated. The screenshot shows one of those runs in the dashboard. The primary inventory service hangs for ten seconds, yet every request succeeds, because the gateway falls back to the replica. Latency spikes only until the circuit breaker opens.

Behind this sit 337 automated tests, plus end-to-end and browser tests, and an eighteen-check clean-clone verification that passed on Linux and on Windows. The pilot run even caught a timeout bug before the real campaign.

## Slide 4 — All five hypotheses held here, each with a cost

Here are the results. All of them are medians over runs, on that one machine.

H1: on a sequential call path, adding d milliseconds of delay added almost exactly d: about 27, 51 and 103 milliseconds for 25, 50 and 100. With parallel calls, most of it was hidden, until the delayed call became the slowest one.

H2: without a timeout, failed requests waited ten seconds. With an 800-millisecond timeout, they failed in about 0.8 seconds. The circuit breaker cut calls to the hung service by 78 percent and failed in 17 milliseconds. But it stayed open after the service recovered, so recovery took about five seconds instead of one.

H3: falling back to the replica in another environment raised success from 74 percent to 100 percent, at the cost of a slower p95.

H4: moving the service on the critical path saved 31 milliseconds. Moving the other one saved nothing measurable.

H5: retries matched the textbook prediction closely, and multiplied the calls as expected.

Finally, at 80 requests per second the gateway ran out of CPU, and throughput collapsed instead of levelling off.

## Slide 5 — What transfers, what does not, what is next

What should you take away? First, the critical path decides: delay and placement matter only where they sit on it. Second, every resilience policy trades something. Timeouts trade errors for bounded latency, breakers trade recovery time for less load, and fallback trades latency for success. Third, without admission control, overload causes a collapse, not a plateau.

Just as important is what this lab cannot show. These are not AWS, Azure or Google numbers. Everything ran on one host, with constant injected delay and application-level faults, and both failure domains share one kernel. With three runs per condition, I make no claims about the extreme tail.

The next step is to run the same scenarios on real providers, add jitter and packet-loss models, and try admission control. All the code, data and CI are open on GitHub: Apache 2.0 for the code, and CC BY for the documentation and results. Thank you.
