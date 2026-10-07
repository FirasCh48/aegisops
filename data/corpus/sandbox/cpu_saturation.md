# CPU saturation: event loop blocked

> Reference incident: `INC-20261003-203913-cpu_saturation`
> Load: 6 req/s · Window: 60 s healthy baseline, 150 s incident

## Summary

Both dependencies of `checkout-service` saw their latency rise at the
same time for one minute, although neither of them was at fault. The
service handled all of its traffic without producing a single error.

## Timeline

- `T-60s` — normal traffic, 176 orders created, no errors
- `T+0`   — saturation begins
- `T+15s` — latency rising on both dependencies
- `T+45s` — `inventory-service` at 0.22 s, `payment-service` at 0.12 s
- `T+60s` — cause stops on its own
- `T+150s` — 852 orders created over the phase, **no errors**
- `T+170s` — back to baseline values, **with no intervention**

## Observed symptoms

| Signal | Baseline | Incident | After |
| --- | --- | --- | --- |
| `order_created` | 176 | 852 | 129 |
| errors, all types | **0** | **0** | 0 |
| p95 `inventory-service` | 0.0481 s | **0.2202 s** | 0.0475 s |
| p95 `payment-service` | 0.0894 s | **0.1204 s** | 0.0865 s |
| `db_pool_usage` | 0 | 0.1 | 0 |
| `process_memory_rss_mb` | 86.9 MB | 91.8 MB | 89.9 MB |

## Investigation

The complete absence of errors rules out the usual causes straight away:
no pool saturation, no unreachable dependency, no application failure.
The service answers every request correctly, just more slowly. The
`order_created` counter even keeps climbing normally.

Per-dependency latency first points to an external problem, since both
callees degrade. That lead is ruled out by the **simultaneity** of the
degradation: `inventory-service` slows by a factor of 4.6 and
`payment-service` by 1.3, over the same window. Yet these two services
share no code, no common dependency, and have no reason to slow down at
the same moment.

A genuinely external degradation produces a clear asymmetry: only one
curve rises, the other stays perfectly flat. That is what a slow
dependency looks like, where `payment-service` reaches 4.85 s while
`inventory-service` does not move by a millisecond.

Here, both move. The bottleneck is not in the callees but in the caller.

The working hypothesis is therefore that concurrent processing is
blocked locally. Steady throughput confirms it: the service is not
overwhelmed by load, it is processing the same volume more slowly.

## Root cause

A synchronous, CPU-heavy computation runs inside the event loop. While it
runs, no other task can make progress: outbound calls already in flight
wait for the loop to hand control back before their responses can be
processed.

The service is not overloaded in the sense of lacking the resources to
absorb the traffic. It has been made sequential: its concurrency
disappears, and every request waits its turn.

The uneven size of the degradation — more pronounced on
`inventory-service` — is explained by call order. `inventory-service` is
called first when an order is processed, and therefore absorbs a larger
share of the wait imposed by the blocked loop. `payment-service`, called
next, inherits a partially freed loop.

## Remediation

1. **Immediate** — identify the blocking computation from a CPU profile.
   A restart would only postpone the problem, which will come back on the
   next request.
2. **Long-term** — move the computation off the event loop, using a
   thread pool executor or a separate worker process.
3. **Prevention** — monitor event loop blocking time, and alert on a
   simultaneous degradation of all dependencies — a pattern that cannot
   have an external origin.

## Discriminating signal

**All dependencies degrade at the same time.**

Two unrelated services do not slow down simultaneously by coincidence.
This concurrence points to the caller.

The contrast with a genuinely slow dependency is clear: in that case only
one curve rises, and it reaches a much higher value — 4.85 s versus
0.22 s here. Magnitude is not the distinguishing criterion; the
**number of affected curves** is.

A second signal, of a different kind: **zero errors**. An outage that
breaks nothing cannot be detected by an error-rate threshold, only by
comparison against baseline latency.

Contrast with the memory leak, the other silent failure: it leaves all
latencies at baseline and only shows up in memory, whereas CPU saturation
degrades latencies without touching memory. Two error-free failures, two
disjoint signals.
