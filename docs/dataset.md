# Jeu de données d'incidents

## Production

Incidents enregistrés dans un bac à sable à trois services, par injection
de pannes à chaud via l'endpoint `/admin/fault`. La cause racine est
connue avec certitude : c'est l'opérateur qui a cassé le système.

C'est la propriété qui rend l'évaluation possible. Sur des incidents
synthétiques produits par un LLM, aucune vérification n'est possible : on
ne peut pas savoir si l'agent a raison.

Chaque capture est orchestrée par `scripts/capture_incident.py`, qui
exécute la séquence complète sans intervention : redémarrage des
services, réinitialisation du stock, levée des pannes résiduelles,
vérification de santé, trafic de fond, injection, observation, levée,
extraction.

## Structure d'un fichier

Chaque capture découpe la fenêtre en trois phases :

| Phase | Contenu |
| --- | --- |
| `baseline` | trafic sain, cold start exclu |
| `incident` | de l'injection à la levée |
| `recovery` | après la levée |

Le découpage porte la comparaison dans les données elles-mêmes. L'agent
lit « p95 payment : 0,089 s puis 4,85 s », pas une valeur isolée dont il
devrait deviner si elle est anormale.

Le fichier contient, pour chaque phase : les métriques Prometheus
résumées (pic, moyenne, dernière valeur, horodatage du pic) et les logs
Loki agrégés par type d'événement, avec quelques échantillons bruts.

## Scénarios

| Scénario | Service injecté | Captures | Signal discriminant |
| --- | --- | --- | --- |
| `connection_leak` | checkout | 3 | `db_pool_usage` à 1, ne redescend pas |
| `memory_leak` | checkout | 1 | tendance mémoire, zéro erreur |
| `slow_response` | payment | 3 | p95 d'une seule dépendance |
| `bad_release` | checkout | 3 | log `deployment` antérieur |
| `cpu_saturation` | checkout | 2 | toutes les dépendances ensemble |
| `internal_error` | inventory | 3 | logs dépendance ≠ code client |

Chaque scénario est capturé à plusieurs débits (4, 6 et 9 req/s) et sur
des durées variables, afin que le modèle apprenne une forme plutôt qu'une
valeur.

## Séparation train / test

Cinq incidents portent `holdout: true`.

**Règle absolue :** ils ne servent jamais de graine à la génération
synthétique en S6. Générer cinquante variantes d'un incident puis évaluer
le modèle sur cet incident produirait une métrique excellente et
mensongère — le modèle aurait été entraîné sur des quasi-copies de son
propre jeu de test.

Les dix autres sont les graines. Les cinq `holdout` constituent le golden
set d'évaluation de S7.

## Postmortems

Un postmortem rédigé par scénario, dans `data/incidents/postmortems/`.

Le JSON porte les données, le postmortem porte le raisonnement. C'est ce
dernier qui alimente le RAG en S3 : un embedding sur `"root_cause":
"Fuite de connexions"` n'a pas assez de surface pour se rattacher à une
alerte nouvelle.

Chaque postmortem suit la même structure : résumé, chronologie, symptômes
chiffrés, investigation, cause racine, remédiation, signal discriminant.

La section « Investigation » inclut délibérément les **hypothèses
écartées** et ce qui les écarte. L'agent doit apprendre ce qui élimine
une piste, pas seulement ce qui confirme la bonne. Un postmortem qui
donne la réponse sans le chemin enseigne la mémorisation, pas le
raisonnement.

Les chiffres des postmortems correspondent à une capture de référence,
nommée en tête de fichier. Un écart entre le postmortem et le JSON
placerait deux versions contradictoires du même incident dans le même
contexte, ce que la métrique `faithfulness` sanctionnerait sans qu'on en
comprenne la cause.

---

# Limites connues

Les limites ci-dessous sont assumées et documentées plutôt que masquées.
Elles sont nécessaires à l'interprétation correcte des résultats
d'évaluation.

## 1. Fenêtre de `rate()` et contamination de la baseline

Les requêtes PromQL utilisent `rate(...[1m])`, qui agrège la minute
écoulée. Les premières mesures de la phase `baseline` recouvrent donc
encore la fin de la capture précédente.

Effet observé : `error_rate` atteint 0,13 à 0,27 en phase `baseline` sur
plusieurs captures, alors que les logs de la même phase ne contiennent
aucune erreur — uniquement des `order_created`.

**Les logs font foi pour la baseline, pas les métriques dérivées.** La
marge `COLD_START_SKIP_S` était fixée à 20 secondes, insuffisante face à
une fenêtre d'agrégation de 60 secondes.

Correction identifiée : porter la marge à 70 secondes et le temps de
chauffe à 120 secondes.

## 2. `p95_checkout` inexploitable

La métrique `p95_checkout` renvoie 1,0 dans presque toutes les phases,
quelle que soit la latence réelle.

Cause : elle s'appuie sur les buckets par défaut de
`prometheus-fastapi-instrumentator`, dont le dernier seuil borné est à
1,0 seconde. Dès que la majorité des observations dépasse ce seuil,
l'interpolation du quantile sature.

**Utiliser `p95_by_dependency`**, dont les buckets ont été choisis
explicitement autour du timeout applicatif de 2 secondes. Cette métrique
reste fidèle sur toute la plage utile.

C'est une illustration directe du rôle des buckets : la précision d'un
histogramme ne dépend pas de la quantité de données, mais du placement
des seuils.

## 3. Phase `recovery` contaminée

Pour la même raison que la baseline, la phase `recovery` — vingt
secondes — est entièrement recouverte par la fenêtre d'agrégation de
l'incident.

Effet observé : `p95 payment-service` affiche encore 4,85 s en
`recovery` alors que la latence est revenue à la normale.

**Les logs de la phase `recovery` restent fiables** et suffisent à
établir le comportement de récupération :

| Scénario | `recovery` logs | Lecture |
| --- | --- | --- |
| `connection_leak` | 80 `db_pool_timeout`, 0 commande | pas de reprise |
| `slow_response` | 107 commandes, 13 timeouts | reprise immédiate |

Cette distinction — récupération spontanée ou redémarrage nécessaire —
est précisément ce qui sépare les deux familles de pannes.

## 4. Épuisement du stock sur les captures à débit élevé

À 9 req/s, le stock d'`inventory-service` (2 000 unités) s'épuise avant
la fin de la fenêtre et produit des `dependency_error` sans rapport avec
la panne injectée.

Exemple, capture `connection_leak` à 9 req/s :

| Phase | Signal principal | Bruit |
| --- | --- | --- |
| incident | 1 468 `db_pool_timeout` | 45 `dependency_error` |
| recovery | 89 `db_pool_timeout` | 78 `dependency_error` |

Le signal dominant reste très net, mais ces captures contiennent bien
deux causes.

Ce n'est pas uniquement un défaut : les incidents réels présentent
souvent plusieurs symptômes d'origines distinctes. Un agent qui conclut
« deux problèmes simultanés » sur ces captures a raison, et sa réponse ne
doit pas être comptée comme une erreur lors de l'évaluation.

## Conséquence pour l'évaluation

Les métriques fiables, par ordre de confiance :

1. **Logs par phase** — fidèles sans réserve
2. **`p95_by_dependency`** — fidèle en phase `incident`
3. **`db_pool_usage`, `memory_mb`, `cache_entries`** — gauges, non
   dérivées, donc non affectées par la fenêtre de `rate()`
4. **`error_rate`** — fiable en phase `incident` uniquement
5. **`p95_checkout`** — à ignorer

Les trois premiers suffisent à distinguer les six scénarios :

| Scénario | `db_pool_usage` | `p95_by_dependency` | `recovery` logs |
| --- | --- | --- | --- |
| `connection_leak` | **1,0** | inchangé | aucune reprise |
| `slow_response` | ~0,1 | **une seule à 4,85** | reprise immédiate |
| `cpu_saturation` | ~0 | **les deux, même valeur** | reprise spontanée |
| `memory_leak` | ~0 | inchangé | aucune reprise |
| `bad_release` | **1,0** | inchangé | aucune reprise |
| `internal_error` | ~0 | inchangé | reprise immédiate |

`connection_leak` et `bad_release` restent indiscernables sur ces trois
signaux — c'est voulu. Seul le log `deployment`, antérieur à la fenêtre
d'incident, les sépare.

## 5. `cpu_saturation` : convergence incomplète

Avec `burn_ms=40` à 6 req/s, les deux dépendances montent ensemble
(×4,5 pour inventory, ×1,3 pour payment) mais ne convergent pas vers une
valeur commune comme observé lors des essais manuels à J4.

Le signal reste valide — deux dépendances sans lien ne se dégradent pas
simultanément par hasard — mais il est moins net. Un `burn_ms` plus élevé
le renforcerait.

## 6. `memory_leak` : capture unique et stock épuisé

Ce scénario n'a qu'une seule capture, de dix minutes à 8 req/s, soit
environ 4 800 requêtes. Le stock s'épuise en cours de route et produit
1 532 `dependency_error`.

Le signal principal reste sans ambiguïté — la mémoire passe de 86 Mo à
394 Mo — mais cette capture contient deux causes, et c'est la seule du
scénario. Un stock plus grand ou une réinitialisation périodique serait
nécessaire pour une capture propre.
## 6. Limite de débit pour les captures longues

Le stock d'`inventory-service` (2 000 unités) impose un plafond au
produit débit × durée. Trois captures de `memory_leak` ont permis de le
cerner :

| Débit | Durée | `dependency_error` parasites |
| --- | --- | --- |
| 8 req/s | 600 s | 1 532 |
| 6 req/s | 600 s | 606 |
| **4 req/s** | **420 s** | **0** |

La capture retenue comme référence est celle à 4 req/s : c'est la seule
où la fuite mémoire apparaît seule, sans cause parasite.

Règle empirique : au-delà d'environ 1 700 requêtes par capture, le stock
s'épuise. Les captures courtes (180 s) ne sont pas concernées, même à
9 req/s.