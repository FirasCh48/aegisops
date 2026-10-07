# Connection pool exhaustion caused by a leak

> Reference incident: `INC-20261003-193936-connection_leak`
> Load: 6 req/s · Window: 60 s healthy baseline, 180 s incident

## Summary

`checkout-service` stopped processing orders for the entire duration of
the incident. The PostgreSQL connection pool drained progressively until
it was fully saturated, and the service did not recover after the fault
was removed.

## Timeline

- `T-60s` — normal traffic, p95 at 130 ms, no errors, 176 orders
  created
- `T+0`    — leak begins
- `T+30s`  — first `db_pool_timeout` errors, error rate at 10%
- `T+90s`  — pool gauge at 1, the successful-order counter freezes
  at 16
- `T+180s` — fault removed
- `T+200s` — **no recovery**: 80 additional errors, zero orders

## Observed symptoms

| Signal | Baseline | Incident | After fault removal |
| --- | --- | --- | --- |
| `order_created` | 176 | 16 then frozen | 0 |
| `db_pool_timeout` | 0 | 978 | 80 |
| `connection_leaked` | 0 | 10 | — |
| checkout p95 | 130 ms | ~1,100 ms | ~1,100 ms |
| pool gauge | 0 | climbs to 1 | **stays at 1** |

## Investigation

The initial symptom — 503s accompanied by `db_pool_timeout` — points to
the database. Two causes produce exactly this signature, and they have to
be told apart.

The first hypothesis is an undersized pool facing a rise in load. The
request rate rules it out: it stays steady at around 6 requests per
second throughout the incident, with no earlier spike.

The second hypothesis is a leak. It is confirmed by how the gauge behaves
after the fault is removed: pool utilization stays at 100% even though no
request is checking out a connection any more. A pool that is simply too
small drains as soon as the pressure drops; a pool whose connections are
never returned never drains.

The `connection_leaked` logs confirm the mechanism and quantify the
retention: ten connections held, i.e. the entire pool.

The contrast between phases is stark. During the healthy baseline, 176
orders are created without a single error. During the incident, only 16
orders go through — the counter freezes as soon as the pool is
exhausted — against 978 rejections. After the fault is removed, 80
further errors are emitted and no more orders are created.

## Root cause

A code path checks out a connection from the pool without releasing it.
The connection remains referenced on the application side, which
prevents both its return to the pool and its collection by the garbage
collector.

The leak is gradual and proportional to traffic: the delay between
deploying the faulty code and saturation depends on load, which explains
why this kind of bug often slips through pre-production testing.

## Remediation

1. **Immediate** — restart the service to free the pool. Stopping the
   leak alone is not enough: connections already held stay held, as the
   80 errors emitted after the fault was removed show.
2. **Long-term** — find the code path that does not call `close()`, and
   switch to a context manager that guarantees the connection is
   returned even when an exception is raised.
3. **Prevention** — alert on `db_pool_connections_in_use / db_pool_size`
   above 0.8 for more than two minutes, and add an integration test that
   checks the pool returns to zero after a burst.

## Discriminating signal

Pool utilization that **stays at 1 after traffic stops**.

This is the only signal that separates a leak from an undersized pool.
Logs, HTTP status codes and latency are identical in both cases, yet the
remediations are opposite: increasing the pool size in the face of a leak
only delays saturation by a few minutes.

A second signal, of a different kind: **no recovery after the fault is
removed**. The 80 post-incident errors and zero orders created establish
that a restart is required — which distinguishes this outage from a slow
dependency, which recovers on its own.
