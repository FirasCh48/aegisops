# Regression introduced by the v2.8 deployment

> Reference incident: `INC-20261003-200924-bad_release`
> Load: 6 req/s · Window: 60 s healthy baseline, 180 s incident

## Summary

`checkout-service` stopped processing orders a few seconds after version
v2.8 was deployed. The symptoms are those of connection pool exhaustion;
the cause is the release itself.

## Timeline

- `T-60s` — normal traffic, 201 orders created, no errors
- `T+0`   — CI pipeline deploys `v2.7` → `v2.8`
- `T+5s`  — first `db_pool_timeout` errors, carrying the field
  `version: v2.8`
- `T+30s` — error rate above 50%
- `T+90s` — full saturation, the order counter freezes at 32
- `T+180s` — rollback from `v2.8` to `v2.7`
- `T+200s` — **errors persist**: 107 additional rejections, no orders
  created

## Observed symptoms

| Signal | Baseline | Incident | After rollback |
| --- | --- | --- | --- |
| `order_created` | 201 | 32 then frozen | 0 |
| `db_pool_timeout` | 0 | 988 | 107 |
| `connection_leaked` | 0 | 10 | — |
| `deployment` | 0 | 1 (`v2.7` → `v2.8`) | 1 (`v2.8` → `v2.7`) |
| `db_pool_usage` | 0 | **1.0** | **1.0** |
| p95 per dependency | 0.048 / 0.091 | unchanged | unchanged |

## Investigation

The symptoms are exactly the same as those of an ordinary connection
leak: the same 503s, the same `db_pool_timeout` logs, the same gauge that
saturates and never comes back down. Looking at the incident window alone
leads to the same conclusion, and therefore to the same remediation — fix
the faulty code.

Per-dependency latency rules out an external cause straight away:
`inventory-service` and `payment-service` stay at their baseline values
for the entire incident. The problem is local to `checkout-service`.

What changes the conclusion sits **before** the window. A `deployment`
event is recorded a few seconds before the first error, showing a change
from `v2.7` to `v2.8`. The temporal proximity between this deployment and
the onset of the symptom is the first piece of evidence.

The second is more direct: every `db_pool_timeout` log itself carries the
field `version: v2.8`. The errors are not merely concurrent with the
deployment — they are emitted by the deployed version. The correlation is
no longer just a coincidence in time.

The rollback gives an ambiguous confirmation that has to be read
carefully: it does not stop the errors — 107 rejections are still
recorded afterwards. That does not invalidate it as a remediation: the
ten connections already held stay held, regardless of the running
version.

## Root cause

The v2.8 deployment introduced a regression: one code path now checks out
a connection from the pool without releasing it.

The cause is therefore not the pool, not the load, and not the database.
It is a release, and the correct remediation is to revert to the previous
version — not a configuration change.

## Remediation

1. **Immediate** — roll back to v2.7, **then redeploy** the service. The
   rollback alone stops the new leak but does not free the ten
   connections already held: the service stays degraded until it is
   restarted.
2. **Long-term** — find, in the diff between v2.7 and v2.8, the code path
   that does not call `close()`.
3. **Prevention** — use progressive rollouts with pool monitoring over
   the window following each release, and add an integration test that
   checks the pool returns to zero after a burst.

## Discriminating signal

**A `deployment` event immediately preceding the incident, and the
`version` field carried by every error log.**

Without this context, this incident is indistinguishable from a
pre-existing connection leak. Both produce exactly the same metrics and
the same error logs. Only reading the events that precede the window
tells them apart — and the remediations differ: fix the code versus roll
back.

Caveat: **the rollback is not enough**. Seeing errors persist after the
rollback must not lead you to rule out the bad-deployment hypothesis.
Stopping a cause does not repair the state it left behind.
