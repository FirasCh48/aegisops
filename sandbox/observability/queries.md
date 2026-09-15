# Requêtes PromQL de référence

Ces requêtes constituent la boîte à outils du Metrics Agent (semaine 4).
Chacune répond à une question précise de l'investigation.

## Quelle est l'ampleur de l'incident ?

Latence p95 d'un service :
histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{service="checkout-service"}[1m])) by (le))

Taux d'erreur :
sum(rate(http_requests_total{service="checkout-service",status=~"5.."}[1m]))
/ sum(rate(http_requests_total{service="checkout-service"}[1m]))

## Quelle dépendance est en cause ?

Latence p95 par dépendance — désigne le coupable :
histogram_quantile(0.95, sum(rate(dependency_call_duration_seconds_bucket[5m])) by (dependency, le))

Échecs ventilés par dépendance et par mode :
sum(rate(dependency_failures_total[5m])) by (dependency, reason)

## La base de données est-elle saturée ?

    db_pool_connections_in_use / db_pool_size

## Le stock est-il épuisé ?

    inventory_stock_total

## Valeurs de référence (état sain)

| Métrique      | Valeur nominale |
| ------------- | --------------- |
| p95 checkout  | ~0,47 s         |
| p95 inventory | ~0,02 s         |
| p95 payment   | ~0,05 s         |
| taux d'erreur | 0               |

Mesurées sur i5-10210U, 100 requêtes séquentielles.
La valeur absolue importe peu ; c'est l'écart à cette base qui signale l'incident.
