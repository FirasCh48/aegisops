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

## Valeurs de référence (état sain)

| Métrique | Valeur nominale |
| --- | --- |
| p95 checkout | ~0,13 s |
| p95 inventory | ~0,02 s |
| p95 payment | ~0,05 s |
| taux d'erreur | 0 |
| débit | ~5 req/s |

Mesurées avec `sandbox/loadgen.py --rps 5 --pattern flat` : client HTTP
persistant, concurrence bornée à 20. Les valeurs antérieures (p95 à
0,47 s) provenaient d'une boucle `curl` séquentielle et mesuraient le
coût de création du processus client, pas le service. Écart : 72 %.

## Signature d'une fuite de connexions

| Phase | p95 | Taux d'erreur | Gauge du pool |
| --- | --- | --- | --- |
| Sain | ~0,13 s | 0 % | 0 |
| Dégradation | ~1,1 s | 10 → 30 % | montée progressive |
| Saturé | ~1,1 s | > 80 % | **1, et y reste** |

Mesurée avec `POOL_TIMEOUT=1`. Le plateau de la gauge après l'arrêt du
trafic est la preuve qui distingue une fuite d'un pool sous-dimensionné.










| Métrique      | Valeur nominale |
| ------------- | --------------- |
| p95 checkout  | ~0,47 s         |
| p95 inventory | ~0,02 s         |
| p95 payment   | ~0,05 s         |
| taux d'erreur | 0               |

Mesurées sur i5-10210U, 100 requêtes séquentielles.
La valeur absolue importe peu ; c'est l'écart à cette base qui signale l'incident.


Mesurées avec `sandbox/loadgen.py --rps 5 --pattern flat`, client HTTP
persistant et concurrence bornée. Les valeurs antérieures (p95 checkout
0,47 s) étaient mesurées avec une boucle `curl` séquentielle : elles
mesuraient le coût de création du processus client, pas le service.