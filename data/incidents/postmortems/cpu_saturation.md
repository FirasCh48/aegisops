# Saturation CPU : blocage de la boucle d'événements

## Résumé

`checkout-service` a vu sa latence tripler pendant une minute, sans
produire la moindre erreur. Les deux dépendances ont paru simultanément
dégradées alors qu'aucune des deux n'était en cause : le traitement
concurrent était bloqué côté appelant.

## Chronologie

- `T-45s` — trafic nominal, p95 à 130 ms, aucune erreur
- `T+0`   — début de la saturation
- `T+15s` — p95 à 314 ms, latences des deux dépendances en hausse
- `T+45s` — p95 à 400 ms, les deux dépendances à 0,23 s
- `T+60s` — extinction automatique de la cause
- `T+75s` — retour progressif à 337 ms puis 315 ms, **sans intervention**

## Symptômes observés

| Signal | Baseline | Incident | Après |
| --- | --- | --- | --- |
| p95 `checkout-service` | 130 ms | 400 ms | retour nominal |
| p95 `inventory-service` | 0,05 s | **0,23 s** | 0,05 s |
| p95 `payment-service` | 0,09 s | **0,23 s** | 0,09 s |
| taux d'erreur | 0 % | **0 %** | 0 % |
| débit | 5,8 req/s | 5,8 req/s | stable |

## Investigation

L'absence totale d'erreurs écarte d'emblée les causes habituelles :
aucune saturation de pool, aucune dépendance injoignable, aucun échec
applicatif. Le service répond correctement à toutes les requêtes,
simplement plus lentement.

La latence par dépendance oriente d'abord vers un problème externe,
puisque les deux appelés se dégradent. Cette piste est écartée par la
forme de la dégradation : `inventory-service` et `payment-service`
montent **ensemble** et convergent vers la **même valeur**, 0,23 s. Or
ces deux services n'ont ni code commun, ni dépendance partagée, ni
raison de se dégrader de façon synchrone et identique.

Une dégradation réellement externe produit une asymétrie : une seule
courbe monte, et elle monte jusqu'à une valeur qui lui est propre.
L'égalité des deux latences est le signe que le facteur limitant n'est
pas chez les appelés, mais chez l'appelant.

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

C'est ce qui explique l'égalité des latences observées. Le temps mesuré
pour chaque dépendance n'est plus son temps de réponse, mais le délai
d'attente commun imposé par la boucle bloquée.

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

**Toutes les dépendances se dégradent ensemble et convergent vers la
même valeur.**

Deux services sans lien entre eux ne tombent pas en panne
simultanément et à l'identique. Cette convergence désigne l'appelant.

Le contraste avec une dépendance réellement lente est net : dans ce cas
une seule courbe monte, et elle atteint une valeur qui lui est propre.
Ici, 0,23 s pour les deux — c'est le temps d'attente de la boucle, pas
celui des appelés.

Second signal : **zéro erreur**. Une panne qui ne casse rien ne se
détecte pas par un seuil d'erreur, seulement par la comparaison à la
latence de référence.