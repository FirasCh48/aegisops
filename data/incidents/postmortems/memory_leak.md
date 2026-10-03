# Fuite mémoire : croissance non bornée d'un cache applicatif

## Résumé

La mémoire résidente de `checkout-service` est passée de 77 Mo à 390 Mo
en quatre minutes, soit un facteur cinq. Pendant toute cette période, le
service a traité l'intégralité du trafic sans une seule erreur et sans
dégradation notable de sa latence.

## Chronologie

- `T-90s` — trafic nominal, mémoire stable à 77 Mo
- `T+0`   — début de la croissance
- `T+60s` — mémoire à ~190 Mo, aucune erreur, p95 inchangé
- `T+120s` — mémoire à ~290 Mo, 787 requêtes traitées, zéro erreur
- `T+240s` — plafond atteint à 390 Mo
- `T+250s` — levée de la cause ; **la mémoire ne redescend pas**

## Symptômes observés

| Signal | Baseline | Incident | Après levée |
| --- | --- | --- | --- |
| `process_memory_rss_mb` | 77 Mo | **390 Mo** | reste à 390 Mo |
| `app_cache_entries` | 0 | croissance continue | reste élevé |
| taux d'erreur | 0 % | **0 %** | 0 % |
| p95 checkout | 136 ms | 144 ms | 144 ms |
| `order_created` | nominal | nominal | nominal |

## Investigation

Cet incident ne déclenche aucune alerte fondée sur un seuil d'erreur ou
de latence : il n'y a ni erreur, ni ralentissement significatif. Il n'est
détectable que par l'observation d'une tendance.

Le signal est la mémoire résidente, qui croît de façon linéaire et ne
redescend jamais. Une consommation mémoire normale oscille : elle monte
sous charge et redescend aux creux de trafic, au rythme du ramasse-miettes.
Une croissance strictement monotone, insensible aux variations
d'intensité, indique que des objets sont retenus et non libérés.

Reste à localiser la rétention. La métrique `app_cache_entries` croît en
proportion de la mémoire : le cache applicatif est le conteneur
responsable. Sans cette seconde métrique, la conclusion se limiterait à
« il y a une fuite quelque part », et la seule remédiation possible
serait un redémarrage périodique.

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
qu'une structure qui croît indéfiniment.

Ce défaut est pratiquement invisible en développement : sous faible
charge, la mémoire monte trop lentement pour être remarquée. Il ne se
manifeste qu'après plusieurs heures ou plusieurs jours de trafic réel.

## Remédiation

1. **Immédiat** — redémarrer le service. L'arrêt de la fuite ne libère
   pas la mémoire déjà retenue.
2. **Fond** — retirer l'élément unique de la clé de cache et fixer une
   politique d'éviction : taille maximale, durée de vie, ou les deux.
3. **Prévention** — alerter sur la pente de `process_memory_rss_mb`
   plutôt que sur une valeur absolue, et borner toute structure en
   mémoire susceptible de croître avec le trafic.

## Signal discriminant

**Une croissance mémoire monotone accompagnée d'un taux d'erreur nul.**

C'est la seule panne du système qui ne produit aucun symptôme
fonctionnel pendant sa phase active. Un agent qui cherche des erreurs ou
un dépassement de seuil ne la détecte pas : le service répond
parfaitement à chaque requête, jusqu'au moment où il s'arrête
brutalement.

La détection repose entièrement sur l'identification d'une tendance, ce
qui impose une fenêtre d'analyse d'au moins dix minutes. Sur deux
minutes, la croissance se confond avec le bruit normal.

La corrélation entre `process_memory_rss_mb` et `app_cache_entries`
distingue ensuite un cache non borné d'une fuite d'un autre type —
connexions, fichiers, tâches en attente.