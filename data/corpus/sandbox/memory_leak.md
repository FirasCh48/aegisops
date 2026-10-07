# Memory leak: unbounded growth of an application cache

> Reference incident: `INC-20261003-212619-memory_leak`
> Load: 4 req/s · Window: 60 s healthy baseline, 420 s incident

## Summary

Resident memory of `checkout-service` grew from 86 MB to 381 MB in seven
minutes, a factor of 4.4. Throughout that period, the service handled
all of its traffic without a single error and without any latency
degradation.

## Timeline

- `T-60s` — normal traffic, memory steady at 85.8 MB, 135 orders created
- `T+0`   — growth begins
- `T+120s` — memory rising continuously, no errors
- `T+300s` — growth still linear, p95 unchanged
- `T+420s` — 1,185 orders created, **zero errors**, memory at 381.3 MB
- `T+440s` — fault removed; **memory stays at 383.0 MB**

## Observed symptoms

| Signal | Baseline | Incident | After fault removal |
| --- | --- | --- | --- |
| `process_memory_rss_mb` | 85.8 MB | **381.3 MB** | **383.0 MB** |
| `app_cache_entries` | 0 | continuous growth | stays high |
| `order_created` | 135 | 1,185 | 24 |
| errors, all types | **0** | **0** | 0 |
| p95 `inventory-service` | 0.048 s | unchanged | unchanged |
| p95 `payment-service` | 0.086 s | unchanged | unchanged |
| `db_pool_usage` | 0 | 0.1 | 0 |

## Investigation

This incident triggers no alert based on an error or latency threshold:
there are no errors and no slowdown. All 1,185 orders in the incident
phase complete. It can only be detected by observing a trend.

The signal is resident memory, which grows linearly and never comes back
down. Normal memory usage oscillates: it rises under load and falls back
in traffic troughs, in step with the garbage collector. Strictly
monotonic growth, insensitive to changes in traffic intensity, means
objects are being retained and not freed.

The value after the fault is removed is decisive: 383.0 MB, slightly
**above** the peak recorded during the incident. Stopping the cause frees
nothing. This behaviour separates a leak from a simple load increase,
where memory comes back down as soon as the pressure drops.

That leaves locating the retention. The `app_cache_entries` metric grows
in proportion to memory: the application cache is the container
responsible. Without this second metric, the conclusion would stop at
"there is a leak somewhere", and the only possible remediation would be
periodic restarts.

Per-dependency latencies stay strictly at baseline, which rules out any
external cause and confirms the problem is local and has no functional
impact — for now.

The shape of the growth provides one last clue. A steady climb,
proportional to traffic, points to normal but unbounded accumulation — a
cache with no expiry. A sudden jump would point to accidental retention
in a specific code path.

## Root cause

An application cache grows with no eviction policy. Its key contains a
value unique to each request, which makes any reuse impossible: no entry
is ever read again, so none is ever replaced or expired.

The cache then no longer serves any caching purpose. It is just a
structure that grows indefinitely, at roughly 0.25 MB per request
processed.

This defect is practically invisible in development: under light load,
memory rises too slowly to be noticed. It only shows up after hours or
days of real traffic — and it ends with the process being killed
abruptly, with no warning sign in the functional metrics.

## Remediation

1. **Immediate** — restart the service. Stopping the leak does not free
   memory already retained, as the 383 MB still in use after the fault
   was removed shows.
2. **Long-term** — remove the unique value from the cache key and set an
   eviction policy: maximum size, TTL, or both.
3. **Prevention** — alert on the slope of `process_memory_rss_mb` rather
   than on an absolute value, and bound every in-memory structure that
   can grow with traffic.

## Discriminating signal

**Monotonic memory growth combined with a zero error rate.**

This is the only failure in the system that produces no functional
symptom during its active phase. An agent looking for errors or a
threshold breach will not detect it: the service answers every request
perfectly, right up to the moment it dies abruptly.

Detection relies entirely on identifying a trend, which requires an
analysis window of at least ten minutes. Over two minutes, the growth is
indistinguishable from normal noise.

The correlation between `process_memory_rss_mb` and `app_cache_entries`
then distinguishes an unbounded cache from other kinds of leak —
connections, file handles, pending tasks.

Contrast with CPU saturation, the other error-free failure: it degrades
the latency of every dependency without touching memory, whereas the
memory leak leaves all latencies at baseline. Two silent failures, two
disjoint signals — and that is what makes it possible to tell them
apart.
