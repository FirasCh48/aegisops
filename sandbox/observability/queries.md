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

## Signature d'une fuite mémoire

| Signal | Comportement |
| --- | --- |
| `process_memory_rss_mb` | montée linéaire continue, jamais de redescente |
| `app_cache_entries` | croît avec la mémoire — désigne le cache comme cause |
| taux d'erreur | **reste à 0** |
| p95 | stable (~144 ms) |

Mesurée : 77 Mo → 390 Mo en ~4 minutes à 8 req/s, 787 requêtes, zéro erreur.

Une fuite mémoire ne casse rien avant de tout casser. L'agent doit
détecter une tendance, pas un seuil d'erreur. Fenêtre d'analyse
minimale : 10 minutes. Une fenêtre de2 minutes ne montre qu'une
variation de quelques pourcents, indiscernable du bruit.

## Signature d'une dépendance lente

| Signal | Comportement |
| --- | --- |
| p95 de la dépendance | saut immédiat au timeout de l'appelant (2069 ms) |
| `dependency_failures{reason="timeout"}` | apparaît en ~10 s |
| p95 des autres dépendances | inchangé — les innocente |
| retour à la normale | **immédiat à la levée, sans redémarrage** |

## Récapitulatif des trois signatures

| Panne | Taux d'erreur | Signal discriminant | Récupération |
| --- | --- | --- | --- |
| `connection_leak` | > 80 % | gauge du pool à 1, plateau | redémarrage requis |
| `slow_response` | > 90 % | p95 dépendance au timeout | levée suffit |
| `memory_leak` | **0 %** | tendance mémoire croissante | redémarrage requis |

La colonne « récupération » est celle qui compte pour le Remediation
Planner : deux de ces pannes exigent un redéploiement, une seule se
résout en arrêtant la cause.


## Signature d'une mauvaise release

| Signal | Comportement |
| --- | --- |
| log `deployment` | `from_version` → `to_version`, quelques secondes avant |
| symptôme | identique à `connection_leak` |
| remédiation | **rollback**, pas augmentation du pool |

Le symptôme seul ne permet pas de conclure. C'est la corrélation
temporelle avec le déploiement qui désigne la cause.

## Signature d'une saturation CPU

| Signal | Comportement |
| --- | --- |
| p95 | montée sur **toutes** les dépendances simultanément |
| taux d'erreur | faible ou nul |
| logs | rien d'anormal |

Aucune dépendance n'est en cause : la boucle d'événements est bloquée.
Une panne qui monte partout à la fois est locale, pas propagée.

## Signature d'une cascade de 500

| Signal | Comportement |
| --- | --- |
| côté client | `409` — trompeur |
| logs inventory | `inventory_internal_error`, 500 |
| p95 inventory | inchangé — le service répond vite, mais faux |

Le code vu par le client ne reflète pas l'erreur réelle de la
dépendance. L'agent doit lire les logs du service en amont.