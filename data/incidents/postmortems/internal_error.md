# Erreur interne d'inventory-service masquée en rupture de stock

## Résumé

Près de la moitié des commandes ont échoué avec un code 409, suggérant
une rupture de stock. Le stock était complet. La cause réelle était une
erreur interne d'`inventory-service`, traduite en 409 par
`checkout-service`.

## Chronologie

- `T-60s` — trafic nominal, aucune erreur
- `T+0`   — début des erreurs internes dans `inventory-service`
- `T+15s` — premiers 409 côté client, 11 commandes refusées
- `T+120s` — 476 commandes refusées sur 992, soit 48 %
- `T+180s` — levée de la cause, **reprise immédiate**

## Symptômes observés

| Signal | Baseline | Incident | Après levée |
| --- | --- | --- | --- |
| code vu par le client | 200 | **409** | 200 |
| `dependency_error` (checkout) | 0 | `status: 500` | 0 |
| `inventory_internal_error` | 0 | `StockLedgerCorruption` | 0 |
| `inventory_stock_total` | 2 000 | **2 000** | 2 000 |
| taux d'erreur mesuré | 0 % | **0 %** | 0 % |
| p95 `inventory-service` | 0,02 s | 0,02 s | inchangé |

## Investigation

Le symptôme apparent désigne une rupture de stock : les clients reçoivent
des 409 accompagnés du message « stock unavailable ». La remédiation
évidente serait de réapprovisionner.

Cette piste est écartée par la métrique de stock, qui reste à 2 000
unités pendant tout l'incident. Il n'y a pas de rupture — les 409 ne
correspondent à rien de ce qu'ils prétendent signaler.

Les logs de `checkout-service` apportent la première contradiction : le
`dependency_error` enregistré porte `status: 500`, et non 409. Le code
409 est donc produit par `checkout-service` lui-même, qui traduit les
erreurs d'`inventory-service` en « stock indisponible ». Cette traduction
est correcte pour une véritable rupture, mais trompeuse pour toute autre
erreur de la dépendance.

Les logs d'`inventory-service` donnent la cause réelle :
`StockLedgerCorruption`, avec la mention d'un défaut de somme de
contrôle sur le registre de stock.

Une observation complémentaire mérite d'être relevée : la latence
d'`inventory-service` reste nominale à 0,02 s. Le service répond vite —
il répond simplement faux. La latence ne détecte pas ce type de panne.

## Cause racine

Une erreur interne d'`inventory-service` — corruption du registre de
stock — provoque un 500 sur une partie des requêtes de réservation.

`checkout-service` intercepte cette erreur et la traduit en 409 vers le
client, conformément à son traitement des erreurs d'`inventory-service`.
Le code vu par l'appelant final ne reflète donc ni la nature ni la
gravité du problème réel.

## Remédiation

1. **Immédiat** — réparer ou reconstruire le registre de stock
   d'`inventory-service`. Réapprovisionner ne résoudrait rien, le stock
   étant intact.
2. **Fond** — identifier la cause de la corruption du registre.
3. **Prévention** — distinguer, côté `checkout-service`, une véritable
   rupture de stock d'une erreur interne de la dépendance, et ne pas les
   traduire par le même code. Alerter sur les erreurs métier, pas
   seulement sur les codes 5xx.

## Signal discriminant

**Le code d'erreur vu par le client contredit les logs de la
dépendance.**

Client : 409, rupture de stock. `checkout-service` : 500 de la
dépendance. `inventory-service` : corruption du registre. Trois lectures
du même événement, dont seule la dernière est exploitable.

Un agent qui s'arrête au premier niveau produit une recommandation
plausible, cohérente avec ce qu'il observe, et entièrement fausse.

Second point, de portée plus générale : le taux d'erreur affiche **0 %**
pendant tout l'incident, parce qu'un 409 n'est pas un 5xx. Près de la
moitié des commandes échouent et le tableau de bord reste au vert. Une
supervision qui compte les erreurs par classe HTTP ne voit pas les
échecs métier.