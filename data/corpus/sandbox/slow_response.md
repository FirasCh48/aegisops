# Slow dependency: cascading saturation from payment-service

## Summary

`checkout-service` rejected most orders for two minutes with 504s. The
service itself was working normally: the cause was in `payment-service`,
whose response time exceeded the caller's timeout.

## Timeline

- `T-60s`  — normal traffic, 190 orders created, no errors
- `T+0`    — `payment-service` degrades
- `T+10s`  — first `dependency_timeout` errors on `checkout-service`
- `T+30s`  — error rate above 70%, p95 pinned at 2,069 ms
- `T+120s` — fault removed
- `T+130s` — **immediate recovery**: 109 orders created with no
  intervention

## Observed symptoms

| Signal | Baseline | Incident | After fault removal |
| --- | --- | --- | --- |
| `order_created` | 190 | 2 | 109 |
| `dependency_timeout` | 0 | 618 | 10 |
| p95 `payment-service` | 0.05 s | **4.85 s** | back to 0.05 s |
| p95 `inventory-service` | 0.02 s | 0.02 s | unchanged |
| p95 `checkout-service` | 130 ms | ~2,070 ms | back to baseline |

## Investigation

The alert fires on `checkout-service`, which is not actually at fault.
Three steps trace the problem back to its source.

Per-dependency latency immediately separates the two callees:
`payment-service` goes from 0.05 s to 4.85 s while `inventory-service`
stays exactly flat at 0.02 s. This asymmetry clears `inventory-service`
and at the same time eliminates the hypothesis of local saturation in
`checkout-service` — a blocked event loop would degrade both dependencies
simultaneously and in the same proportions.

The failure mode narrows down the nature of the problem. The
`reason="timeout"` label shows that `payment-service` was still
responding, just too slowly; a dead service would have produced
`reason="unreachable"` and an immediate connection refused. The
distinction is decisive for remediation: a service that is alive but
slow is not something you restart, it is something you investigate.

The return to normal without any intervention on `checkout-service`
confirms there is no corrupted state on its side.

## Root cause

The response time of `payment-service` exceeded the two-second
`DEPENDENCY_TIMEOUT` configured in `checkout-service`. Every request
waited out the timeout before failing, and no order could complete.

The p95 measured on the caller side caps at 2,069 ms — the timeout
value — while the dependency's real latency reached 4.85 s. The caller's
metric is bounded by its own patience: it tells you a threshold was
crossed, not by how much.

## Remediation

1. **Immediate** — investigate `payment-service`: load, database,
   third-party provider. No action on `checkout-service` will help.
2. **Long-term** — fix the cause of the slowness in `payment-service`.
3. **Prevention** — add a circuit breaker in `checkout-service` to stop
   calling a degraded dependency instead of waiting out the timeout on
   every request, and alert on per-dependency latency rather than on
   overall latency alone.

## Discriminating signal

**Only one latency curve rises.**

`payment-service` degrades a hundredfold while `inventory-service` does
not move. This asymmetry points to the faulty dependency and rules out a
cause local to the caller: CPU saturation in `checkout-service` would
make both curves rise together, towards a common value.

A second signal, of a different kind: **immediate recovery once the fault
is removed**, with no restart. This separates this outage from resource
leaks, which leave corrupted state behind them.
