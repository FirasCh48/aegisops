# Dépendance lente : saturation en cascade depuis payment-service

## Résumé

`checkout-service` a rejeté la majorité des commandes pendant deux
minutes avec des codes 504. Le service lui-même fonctionnait
normalement : la cause se situait dans `payment-service`, dont le temps
de réponse a dépassé le délai d'attente de l'appelant.

## Chronologie

- `T-60s`  — trafic nominal, 190 commandes créées, aucune erreur
- `T+0`    — dégradation de `payment-service`
- `T+10s`  — premiers `dependency_timeout` sur `checkout-service`
- `T+30s`  — taux d'erreur supérieur à 70 %, p95 figé à 2 069 ms
- `T+120s` — levée de la cause
- `T+130s` — **reprise immédiate** : 109 commandes créées sans
  intervention

## Symptômes observés

| Signal | Baseline | Incident | Après levée |
| --- | --- | --- | --- |
| `order_created` | 190 | 2 | 109 |
| `dependency_timeout` | 0 | 618 | 10 |
| p95 `payment-service` | 0,05 s | **4,85 s** | retour à 0,05 s |
| p95 `inventory-service` | 0,02 s | 0,02 s | inchangé |
| p95 `checkout-service` | 130 ms | ~2 070 ms | retour nominal |

## Investigation

L'alerte se déclenche sur `checkout-service`, qui est pourtant hors de
cause. Trois étapes permettent de remonter à la source.

La latence par dépendance départage immédiatement les deux appelés :
`payment-service` passe de 0,05 s à 4,85 s tandis qu'`inventory-service`
reste strictement inchangé à 0,02 s. Cette asymétrie innocente
`inventory-service` et élimine du même coup l'hypothèse d'une saturation
locale de `checkout-service` — une boucle d'événements bloquée
dégraderait les deux dépendances simultanément et dans les mêmes
proportions.

Le mode de défaillance précise la nature du problème. Le label
`reason="timeout"` indique que `payment-service` répondait encore, mais
trop lentement ; un service arrêté aurait produit `reason="unreachable"`
et un refus de connexion immédiat. La distinction est décisive pour la
remédiation : un service vivant mais lent ne se redémarre pas, il
s'investigue.

Le retour à la normale sans aucune intervention sur `checkout-service`
confirme l'absence d'état corrompu de son côté.

## Cause racine

Le temps de réponse de `payment-service` a dépassé le
`DEPENDENCY_TIMEOUT` de deux secondes configuré côté
`checkout-service`. Chaque requête a attendu ce délai avant d'échouer,
sans qu'aucune commande ne puisse aboutir.

Le p95 mesuré côté appelant plafonne à 2 069 ms, soit la valeur du
timeout, alors que la latence réelle de la dépendance atteignait 4,85 s.
La métrique de l'appelant est bornée par sa propre patience : elle
indique qu'un seuil a été franchi, pas de combien.

## Remédiation

1. **Immédiat** — investiguer `payment-service` : charge, base de
   données, fournisseur externe. Aucune action sur `checkout-service`
   n'est utile.
2. **Fond** — corriger la cause de la lenteur dans `payment-service`.
3. **Prévention** — ajouter un disjoncteur côté `checkout-service` pour
   cesser d'appeler une dépendance dégradée plutôt que d'attendre le
   timeout à chaque requête, et alerter sur la latence par dépendance
   plutôt que sur la seule latence globale.

## Signal discriminant

**Une seule courbe de latence monte.**

`payment-service` se dégrade d'un facteur cent pendant
qu'`inventory-service` ne bouge pas. Cette asymétrie désigne la
dépendance fautive et exclut une cause locale à l'appelant : une
saturation CPU de `checkout-service` ferait monter les deux courbes
ensemble, vers une valeur commune.

Second signal, de nature différente : la **récupération immédiate à la
levée**, sans redémarrage. Elle sépare cette panne des fuites de
ressources, qui laissent un état corrompu derrière elles.