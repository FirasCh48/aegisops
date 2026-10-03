# Régression introduite par le déploiement v2.8

## Résumé

`checkout-service` a cessé de traiter les commandes quelques secondes
après le déploiement de la version v2.8. Les symptômes sont ceux d'un
épuisement du pool de connexions ; la cause est la mise en production
elle-même.

## Chronologie

- `19:37:45` — déploiement de `v2.7` vers `v2.8` par la chaîne
  d'intégration continue
- `19:37:50` — premiers `db_pool_timeout`, portant le champ
  `version: v2.8`
- `19:38:30` — taux d'erreur supérieur à 50 %
- `19:40:10` — saturation complète, plus aucune commande créée
- `19:40:34` — rollback de `v2.8` vers `v2.7`
- `19:40:40` — **les erreurs persistent** malgré le rollback

## Symptômes observés

| Signal | Baseline | Incident | Après rollback |
| --- | --- | --- | --- |
| `order_created` | 70 | figé à 70 | 0 |
| `db_pool_timeout` | 0 | 1 217 | continue |
| champ `version` des erreurs | — | `v2.8` | `v2.7` |
| p95 checkout | 253 ms | 1 109 ms | 1 109 ms |
| gauge du pool | 0 | monte à 1 | reste à 1 |

## Investigation

Les symptômes sont rigoureusement identiques à ceux d'une fuite de
connexions ordinaire : mêmes codes 503, mêmes logs `db_pool_timeout`,
même gauge qui sature et ne redescend pas. L'analyse de la seule fenêtre
d'incident conduirait à la même conclusion, et donc à la même
remédiation — corriger le code fautif.

Ce qui change la conclusion se trouve **avant** la fenêtre. Un événement
`deployment` est enregistré quelques secondes avant la première erreur,
indiquant un passage de `v2.7` à `v2.8`. La proximité temporelle entre ce
déploiement et l'apparition du symptôme est le premier élément.

Le second est plus direct : chaque log `db_pool_timeout` porte lui-même
le champ `version: v2.8`. Les erreurs ne sont pas seulement
contemporaines du déploiement, elles sont émises par la version déployée.
La corrélation n'est plus une simple coïncidence temporelle.

Le rollback apporte une confirmation ambiguë qu'il faut savoir lire : il
n'arrête pas les erreurs. Cela ne l'invalide pas comme remédiation — les
connexions déjà retenues le restent, indépendamment de la version
courante.

## Cause racine

Le déploiement de la version v2.8 a introduit une régression : un chemin
de code emprunte désormais une connexion au pool sans la relâcher.

La cause n'est donc pas le pool, ni la charge, ni la base de données.
C'est une mise en production, et la remédiation correcte est le retour à
la version antérieure — pas un ajustement de configuration.

## Remédiation

1. **Immédiat** — rollback vers v2.7, **puis redéploiement** du service.
   Le rollback seul arrête la nouvelle fuite mais ne libère pas les
   connexions déjà retenues : le service reste dégradé jusqu'au
   redémarrage.
2. **Fond** — identifier dans le différentiel entre v2.7 et v2.8 le
   chemin de code qui n'appelle pas `close()`.
3. **Prévention** — déployer progressivement avec surveillance du pool
   sur la fenêtre suivant chaque mise en production, et ajouter un test
   d'intégration vérifiant que le pool revient à zéro après une rafale.

## Signal discriminant

**Un événement `deployment` immédiatement antérieur à l'incident, et le
champ `version` porté par chaque log d'erreur.**

Sans ce contexte, cet incident est indiscernable d'une fuite de
connexions préexistante. Les deux produisent exactement les mêmes
métriques et les mêmes logs d'erreur. Seule la lecture des événements
qui précèdent la fenêtre permet de les séparer — et les remédiations
diffèrent : corriger du code contre revenir en arrière.

Point d'attention : **le rollback ne suffit pas**. Constater que les
erreurs persistent après le retour en arrière ne doit pas conduire à
écarter l'hypothèse du déploiement fautif. Arrêter une cause ne répare
pas l'état qu'elle a laissé derrière elle.
