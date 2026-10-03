# Régression introduite par le déploiement v2.8

> Incident de référence : `INC-20261003-200924-bad_release`
> Débit : 6 req/s · Fenêtre : 60 s de fond sain, 180 s d'incident

## Résumé

`checkout-service` a cessé de traiter les commandes quelques secondes
après le déploiement de la version v2.8. Les symptômes sont ceux d'un
épuisement du pool de connexions ; la cause est la mise en production
elle-même.

## Chronologie

- `T-60s` — trafic nominal, 201 commandes créées, aucune erreur
- `T+0`   — déploiement de `v2.7` vers `v2.8` par la chaîne d'intégration
  continue
- `T+5s`  — premiers `db_pool_timeout`, portant le champ `version: v2.8`
- `T+30s` — taux d'erreur supérieur à 50 %
- `T+90s` — saturation complète, le compteur de commandes se fige à 32
- `T+180s` — rollback de `v2.8` vers `v2.7`
- `T+200s` — **les erreurs persistent** : 107 refus supplémentaires,
  aucune commande créée

## Symptômes observés

| Signal | Baseline | Incident | Après rollback |
| --- | --- | --- | --- |
| `order_created` | 201 | 32 puis figé | 0 |
| `db_pool_timeout` | 0 | 988 | 107 |
| `connection_leaked` | 0 | 10 | — |
| `deployment` | 0 | 1 (`v2.7` → `v2.8`) | 1 (`v2.8` → `v2.7`) |
| `db_pool_usage` | 0 | **1,0** | **1,0** |
| p95 par dépendance | 0,048 / 0,091 | inchangé | inchangé |

## Investigation

Les symptômes sont rigoureusement identiques à ceux d'une fuite de
connexions ordinaire : mêmes 503, mêmes logs `db_pool_timeout`, même
gauge qui sature et ne redescend pas. L'analyse de la seule fenêtre
d'incident conduirait à la même conclusion, et donc à la même
remédiation — corriger le code fautif.

Les latences par dépendance écartent d'emblée une cause externe :
`inventory-service` et `payment-service` restent à leurs valeurs
nominales pendant tout l'incident. Le problème est local à
`checkout-service`.

Ce qui change la conclusion se trouve **avant** la fenêtre. Un événement
`deployment` est enregistré quelques secondes avant la première erreur,
indiquant un passage de `v2.7` à `v2.8`. La proximité temporelle entre ce
déploiement et l'apparition du symptôme est le premier élément.

Le second est plus direct : chaque log `db_pool_timeout` porte lui-même
le champ `version: v2.8`. Les erreurs ne sont pas seulement
contemporaines du déploiement, elles sont émises par la version déployée.
La corrélation n'est plus une simple coïncidence temporelle.

Le rollback apporte une confirmation ambiguë qu'il faut savoir lire : il
n'arrête pas les erreurs — 107 refus sont encore enregistrés après. Cela
ne l'invalide pas comme remédiation : les dix connexions déjà retenues le
restent, indépendamment de la version courante.

## Cause racine

Le déploiement de la version v2.8 a introduit une régression : un chemin
de code emprunte désormais une connexion au pool sans la relâcher.

La cause n'est donc ni le pool, ni la charge, ni la base de données.
C'est une mise en production, et la remédiation correcte est le retour à
la version antérieure — pas un ajustement de configuration.

## Remédiation

1. **Immédiat** — rollback vers v2.7, **puis redéploiement** du service.
   Le rollback seul arrête la nouvelle fuite mais ne libère pas les dix
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
métriques et les mêmes logs d'erreur. Seule la lecture des événements qui
précèdent la fenêtre permet de les séparer — et les remédiations
diffèrent : corriger du code contre revenir en arrière.

Point d'attention : **le rollback ne suffit pas**. Constater que les
erreurs persistent après le retour en arrière ne doit pas conduire à
écarter l'hypothèse du déploiement fautif. Arrêter une cause ne répare
pas l'état qu'elle a laissé derrière elle.