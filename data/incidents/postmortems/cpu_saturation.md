# Saturation CPU : blocage de la boucle d'événements

> Incident de référence : `INC-20261003-203913-cpu_saturation`
> Débit : 6 req/s · Fenêtre : 60 s de fond sain, 150 s d'incident

## Résumé

Les deux dépendances de `checkout-service` ont vu leur latence augmenter
simultanément pendant une minute, sans qu'aucune des deux ne soit en
cause. Le service a traité l'intégralité du trafic sans produire la
moindre erreur.

## Chronologie

- `T-60s` — trafic nominal, 176 commandes créées, aucune erreur
- `T+0`   — début de la saturation
- `T+15s` — latences des deux dépendances en hausse
- `T+45s` — `inventory-service` à 0,22 s, `payment-service` à 0,12 s
- `T+60s` — extinction automatique de la cause
- `T+150s` — 852 commandes créées sur la phase, **aucune erreur**
- `T+170s` — retour aux valeurs nominales, **sans intervention**

## Symptômes observés

| Signal | Baseline | Incident | Après |
| --- | --- | --- | --- |
| `order_created` | 176 | 852 | 129 |
| erreurs, tous types | **0** | **0** | 0 |
| p95 `inventory-service` | 0,0481 s | **0,2202 s** | 0,0475 s |
| p95 `payment-service` | 0,0894 s | **0,1204 s** | 0,0865 s |
| `db_pool_usage` | 0 | 0,1 | 0 |
| `process_memory_rss_mb` | 86,9 Mo | 91,8 Mo | 89,9 Mo |

## Investigation

L'absence totale d'erreurs écarte d'emblée les causes habituelles :
aucune saturation de pool, aucune dépendance injoignable, aucun échec
applicatif. Le service répond correctement à toutes les requêtes,
simplement plus lentement. Le compteur `order_created` continue même de
progresser normalement.

La latence par dépendance oriente d'abord vers un problème externe,
puisque les deux appelés se dégradent. Cette piste est écartée par la
**simultanéité** de la dégradation : `inventory-service` est multiplié
par 4,6 et `payment-service` par 1,3, sur la même fenêtre. Or ces deux
services n'ont ni code commun, ni dépendance partagée, ni raison de
ralentir au même moment.

Une dégradation réellement externe produit une asymétrie franche : une
seule courbe monte, l'autre reste strictement stable. C'est ce qui
s'observe lors d'une dépendance lente, où `payment-service` atteint
4,85 s pendant qu'`inventory-service` ne bouge pas d'un millième.

Ici, les deux bougent. Le facteur limitant n'est pas chez les appelés
mais chez l'appelant.

L'hypothèse retenue est donc un blocage local du traitement concurrent.
Elle est confirmée par la stabilité du débit : le service n'est pas
submergé par la charge, il traite le même volume plus lentement.

## Cause racine

Un traitement synchrone et coûteux en CPU s'exécute à l'intérieur de la
boucle d'événements. Tant qu'il s'exécute, aucune autre tâche ne peut
progresser : les appels sortants déjà émis attendent que la boucle leur
rende la main pour traiter leur réponse.

Le service n'est pas surchargé au sens où il manquerait de ressources
pour absorber le trafic. Il est rendu séquentiel : son parallélisme
disparaît, et chaque requête attend son tour.

L'amplitude inégale de la dégradation — plus marquée sur
`inventory-service` — s'explique par l'ordre des appels.
`inventory-service` est sollicité en premier dans le traitement d'une
commande, et absorbe donc une part plus importante du délai d'attente
imposé par la boucle bloquée. `payment-service`, appelé ensuite, hérite
d'une boucle partiellement libérée.

## Remédiation

1. **Immédiat** — identifier le traitement bloquant à partir d'un profil
   CPU. Un redémarrage ne ferait que décaler le problème, qui
   réapparaîtra à la requête suivante.
2. **Fond** — déplacer le calcul hors de la boucle d'événements, via un
   exécuteur de threads ou un processus de travail séparé.
3. **Prévention** — surveiller le temps de blocage de la boucle
   d'événements, et alerter sur une dégradation simultanée de toutes les
   dépendances — motif qui ne peut pas être d'origine externe.

## Signal discriminant

**Toutes les dépendances se dégradent en même temps.**

Deux services sans lien entre eux ne ralentissent pas simultanément par
hasard. Cette concomitance désigne l'appelant.

Le contraste avec une dépendance réellement lente est net : dans ce cas
une seule courbe monte, et elle atteint une valeur bien plus élevée —
4,85 s contre 0,22 s ici. L'ampleur n'est pas le critère de distinction ;
c'est le **nombre de courbes affectées**.

Second signal, de nature différente : **zéro erreur**. Une panne qui ne
casse rien ne se détecte pas par un seuil d'erreur, seulement par
comparaison à la latence de référence.

Contraste avec la fuite mémoire, l'autre panne silencieuse : celle-ci
laisse toutes les latences nominales et ne se voit que dans la mémoire,
alors que la saturation CPU dégrade les latences sans toucher à la
mémoire. Deux pannes sans erreur, deux signaux disjoints.