# Fuite mémoire : croissance non bornée d'un cache applicatif

> Incident de référence : `INC-20261003-212619-memory_leak`
> Débit : 4 req/s · Fenêtre : 60 s de fond sain, 420 s d'incident

## Résumé

La mémoire résidente de `checkout-service` est passée de 86 Mo à 381 Mo
en sept minutes, soit un facteur 4,4. Pendant toute cette période, le
service a traité l'intégralité du trafic sans une seule erreur et sans
dégradation de sa latence.

## Chronologie

- `T-60s` — trafic nominal, mémoire stable à 85,8 Mo, 135 commandes créées
- `T+0`   — début de la croissance
- `T+120s` — mémoire en hausse continue, aucune erreur
- `T+300s` — croissance toujours linéaire, p95 inchangé
- `T+420s` — 1 185 commandes créées, **zéro erreur**, mémoire à 381,3 Mo
- `T+440s` — levée de la cause ; **la mémoire reste à 383,0 Mo**

## Symptômes observés

| Signal | Baseline | Incident | Après levée |
| --- | --- | --- | --- |
| `process_memory_rss_mb` | 85,8 Mo | **381,3 Mo** | **383,0 Mo** |
| `app_cache_entries` | 0 | croissance continue | reste élevé |
| `order_created` | 135 | 1 185 | 24 |
| erreurs, tous types | **0** | **0** | 0 |
| p95 `inventory-service` | 0,048 s | inchangé | inchangé |
| p95 `payment-service` | 0,086 s | inchangé | inchangé |
| `db_pool_usage` | 0 | 0,1 | 0 |

## Investigation

Cet incident ne déclenche aucune alerte fondée sur un seuil d'erreur ou
de latence : il n'y a ni erreur, ni ralentissement. Les 1 185 commandes
de la phase d'incident aboutissent toutes. Il n'est détectable que par
l'observation d'une tendance.

Le signal est la mémoire résidente, qui croît de façon linéaire et ne
redescend jamais. Une consommation mémoire normale oscille : elle monte
sous charge et redescend aux creux de trafic, au rythme du
ramasse-miettes. Une croissance strictement monotone, insensible aux
variations d'intensité du trafic, indique que des objets sont retenus et
non libérés.

La valeur après la levée est décisive : 383,0 Mo, soit légèrement
**au-dessus** du pic enregistré pendant l'incident. Arrêter la cause ne
libère rien. Ce comportement sépare une fuite d'une simple montée en
charge, où la mémoire redescend dès que la pression retombe.

Reste à localiser la rétention. La métrique `app_cache_entries` croît en
proportion de la mémoire : le cache applicatif est le conteneur
responsable. Sans cette seconde métrique, la conclusion se limiterait à
« il y a une fuite quelque part », et la seule remédiation possible
serait un redémarrage périodique.

Les latences par dépendance restent strictement nominales, ce qui écarte
toute cause externe et confirme que le problème est local et sans effet
fonctionnel — pour l'instant.

La forme de la croissance apporte une dernière information. Une montée
régulière, proportionnelle au trafic, oriente vers une accumulation
normale non bornée — un cache sans expiration. Une montée brutale
orienterait vers une rétention accidentelle dans un chemin de code
particulier.

## Cause racine

Un cache applicatif grossit sans politique d'éviction. Sa clé contient un
élément unique à chaque requête, ce qui rend toute réutilisation
impossible : aucune entrée n'est jamais relue, donc aucune n'est jamais
remplacée ni expirée.

Le cache ne remplit alors plus aucune fonction de cache. Il n'est plus
qu'une structure qui croît indéfiniment, à raison d'environ 0,25 Mo par
requête traitée.

Ce défaut est pratiquement invisible en développement : sous faible
charge, la mémoire monte trop lentement pour être remarquée. Il ne se
manifeste qu'après plusieurs heures ou plusieurs jours de trafic réel —
et il se termine par un arrêt brutal du processus, sans aucun signe
avant-coureur dans les métriques fonctionnelles.

## Remédiation

1. **Immédiat** — redémarrer le service. L'arrêt de la fuite ne libère
   pas la mémoire déjà retenue, comme le montrent les 383 Mo encore
   occupés après la levée de la cause.
2. **Fond** — retirer l'élément unique de la clé de cache et fixer une
   politique d'éviction : taille maximale, durée de vie, ou les deux.
3. **Prévention** — alerter sur la pente de `process_memory_rss_mb`
   plutôt que sur une valeur absolue, et borner toute structure en
   mémoire susceptible de croître avec le trafic.

## Signal discriminant

**Une croissance mémoire monotone accompagnée d'un taux d'erreur nul.**

C'est la seule panne du système qui ne produit aucun symptôme fonctionnel
pendant sa phase active. Un agent qui cherche des erreurs ou un
dépassement de seuil ne la détecte pas : le service répond parfaitement à
chaque requête, jusqu'au moment où il s'arrête brutalement.

La détection repose entièrement sur l'identification d'une tendance, ce
qui impose une fenêtre d'analyse d'au moins dix minutes. Sur deux
minutes, la croissance se confond avec le bruit normal.

La corrélation entre `process_memory_rss_mb` et `app_cache_entries`
distingue ensuite un cache non borné d'une fuite d'un autre type —
connexions, fichiers, tâches en attente.

Contraste avec la saturation CPU, l'autre panne sans erreur : celle-ci
dégrade la latence de toutes les dépendances sans toucher à la mémoire,
alors que la fuite mémoire laisse toutes les latences nominales. Deux
pannes silencieuses, deux signaux disjoints — et c'est ce qui permet de
les séparer.