# inventory-service internal error masked as out-of-stock

> Reference incident: `INC-20261003-202505-internal_error`
> Load: 6 req/s · Window: 60 s healthy baseline, 180 s incident

## Summary

Nearly half of all orders failed with a 409, suggesting an out-of-stock
condition. Stock was full. The real cause was an internal error in
`inventory-service`, translated into a 409 by `checkout-service`.

## Timeline

- `T-60s` — normal traffic, 199 orders created, no errors
- `T+0`   — internal errors begin in `inventory-service`
- `T+15s` — first client-side 409s
- `T+180s` — 514 orders rejected against 531 completed, a 49% failure
  rate
- `T+200s` — fault removed, **immediate recovery**: 97 orders

## Observed symptoms

| Signal | Baseline | Incident | After fault removal |
| --- | --- | --- | --- |
| `order_created` | 199 | 531 | 97 |
| `dependency_error` | 0 | **514** | 0 |
| status code seen by client | 200 | **409** | 200 |
| `inventory_internal_error` | 0 | `StockLedgerCorruption` | 0 |
| `inventory_stock_total` | 2,000 | **2,000** | 2,000 |
| p95 `inventory-service` | 0.0512 s | 0.0486 s | 0.0478 s |
| `db_pool_usage` | 0 | 0.1 | 0 |

## Investigation

The apparent symptom points to a stockout: clients receive 409s with the
message "stock unavailable". The obvious remediation would be to
restock.

The stock metric rules that out: it stays at 2,000 units throughout the
incident. There is no stockout — the 409s do not correspond to anything
they claim to report.

The `checkout-service` logs provide the first contradiction: the recorded
`dependency_error` carries `status: 500`, not 409. The 409 is therefore
produced by `checkout-service` itself, which translates errors from
`inventory-service` into "stock unavailable". That mapping is correct
for a genuine stockout, and misleading for any other dependency error.

The `inventory-service` logs give the real cause:
`StockLedgerCorruption`, reporting a checksum failure on the stock
ledger.

Two further observations are worth noting.

`inventory-service` latency stays at baseline, at 0.049 s. The service
answers fast — it just answers wrong. No latency metric detects this
kind of failure.

And the completed-order counter keeps climbing: 531 orders go through
during the incident, with a failure rate of about 50%. The system is not
down, it is half broken — a state that is harder to detect than a hard
outage.

## Root cause

An internal error in `inventory-service` — stock ledger corruption —
causes a 500 on a share of reservation requests.

`checkout-service` catches this error and translates it into a 409 for
the client, as its error handling for `inventory-service` dictates. The
status code seen by the end caller therefore reflects neither the nature
nor the severity of the real problem.

## Remediation

1. **Immediate** — repair or rebuild the `inventory-service` stock
   ledger. Restocking would fix nothing, since stock is intact.
2. **Long-term** — find the cause of the ledger corruption.
3. **Prevention** — in `checkout-service`, distinguish a genuine stockout
   from an internal dependency error, and do not map them to the same
   status code. Alert on business errors, not only on 5xx codes.

## Discriminating signal

**The error code seen by the client contradicts the dependency's logs.**

Client: 409, out of stock. `checkout-service`: 500 from the dependency.
`inventory-service`: ledger corruption. Three readings of the same event,
of which only the last is actionable.

An agent that stops at the first layer produces a recommendation that is
plausible, consistent with what it observes, and entirely wrong.

A second, more general point: the error rate measured on 5xx codes
shows **zero** for the whole incident, because a 409 is not a 5xx.
Nearly half of all orders fail and the dashboard stays green. Monitoring
that counts errors by HTTP class does not see business failures.
