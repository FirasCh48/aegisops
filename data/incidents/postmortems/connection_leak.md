# Épuisement du pool de connexions par fuite

> Incident de référence : `INC-20261003-193936-connection_leak`
> Débit : 6 req/s · Fenêtre : 60 s de fond sain, 180 s d'incident

## Résumé

`checkout-service` a cessé de traiter les commandes pendant toute la durée
de l'incident. Le pool de connexions à PostgreSQL s'est vidé
progressivement jusqu'à saturation complète, et le service n'a pas
récupéré après la levée de la cause.

## Chronologie

- `T-60s` — trafic nominal, p95 à 130 ms, aucune erreur, 176 commandes
  créées
- `T+0`    — début de la fuite
- `T+30s`  — premiers `db_pool_timeout`, taux d'erreur à 10 %
- `T+90s`  — gauge du pool à 1, le compteur de commandes réussies se fige
  à 16
- `T+180s` — levée de la cause
- `T+200s` — **aucune reprise** : 80 erreurs supplémentaires, zéro commande

## Symptômes observés

| Signal | Baseline | Incident | Après levée |
| --- | --- | --- | --- |
| `order_created` | 176 | 16 puis figé | 0 |
| `db_pool_timeout` | 0 | 978 | 80 |
| `connection_leaked` | 0 | 10 | — |
| p95 checkout | 130 ms | ~1 100 ms | ~1 100 ms |
| gauge du pool | 0 | monte à 1 | **reste à 1** |

## Investigation

Le symptôme initial — des 503 accompagnés de `db_pool_timeout` — oriente
vers la base de données. Deux causes produisent exactement cette
signature, et il faut les départager.

La première hypothèse est un pool sous-dimensionné face à une hausse de
charge. Elle est écartée par le débit : celui-ci reste stable autour de
6 requêtes par seconde pendant tout l'incident, sans pic antérieur.

La seconde hypothèse est une fuite. Elle est confirmée par le
comportement de la gauge après la levée de la cause : l'occupation du
pool reste à 100 % alors que plus aucune requête n'emprunte de
connexion. Un pool simplement trop petit se vide dès que la pression
retombe ; un pool dont les connexions ne sont jamais rendues ne se vide
jamais.

Les logs `connection_leaked` confirment le mécanisme et quantifient la
rétention : dix connexions retenues, soit la totalité du pool.

Le contraste entre les phases est net. En fond sain, 176 commandes sont
créées sans une seule erreur. Pendant l'incident, 16 commandes
seulement aboutissent — le compteur se fige dès que le pool est épuisé —
pour 978 refus. Après la levée, 80 erreurs supplémentaires sont émises et
plus aucune commande n'est créée.

## Cause racine

Un chemin de code emprunte une connexion au pool sans la relâcher. La
connexion reste référencée côté application, ce qui empêche à la fois sa
restitution au pool et sa libération par le ramasse-miettes.

La fuite est progressive et proportionnelle au trafic : le délai entre le
déploiement du code fautif et la saturation dépend de la charge, ce qui
explique qu'un tel bug franchisse souvent les tests de pré-production.

## Remédiation

1. **Immédiat** — redémarrer le service pour libérer le pool. L'arrêt de
   la fuite seul ne suffit pas : les connexions déjà retenues le restent,
   comme le montrent les 80 erreurs émises après la levée.
2. **Fond** — identifier le chemin de code qui n'appelle pas `close()`,
   et basculer sur un gestionnaire de contexte garantissant la
   restitution même en cas d'exception.
3. **Prévention** — alerter sur `db_pool_connections_in_use / db_pool_size`
   au-delà de 0,8 pendant plus de deux minutes, et ajouter un test
   d'intégration vérifiant que le pool revient à zéro après une rafale.

## Signal discriminant

L'occupation du pool qui **reste à 1 après l'arrêt du trafic**.

C'est le seul signal qui sépare une fuite d'un sous-dimensionnement. Les
logs, les codes HTTP et la latence sont identiques dans les deux cas,
alors que les remédiations sont opposées : augmenter la taille du pool
face à une fuite ne fait que repousser la saturation de quelques minutes.

Second signal, de nature différente : **l'absence de reprise après la
levée**. Les 80 erreurs post-incident et les zéro commande créée
établissent qu'un redémarrage est nécessaire — ce qui distingue cette
panne d'une dépendance lente, qui récupère seule.