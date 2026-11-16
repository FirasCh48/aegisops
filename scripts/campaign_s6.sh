#!/usr/bin/env bash
# Campagne S6 : jeu de test réservé.
#
# --warmup 180 n'est pas un réglage de confort. Le détecteur prend huit
# points de référence à 15 s de pas, soit 120 s minimum ; avec le défaut
# de 60 s la « baseline » contient déjà l'incident. La première campagne
# a produit sept captures inutilisables pour cette seule raison : sur
# l'une d'elles le détecteur ne trouve aucun changement, retombe sur sa
# fenêtre par défaut, et le contexte sort vide — zéro métrique.
#
# timeout : la capture bad_release du 26/10 est restée bloquée 23 heures,
# services morts et zéro trafic. flock empêche une seconde campagne, pas
# un blocage.
set -uo pipefail

exec 9> /tmp/aegis-campaign.lock
flock -n 9 || { echo "une campagne tourne deja - abandon"; exit 1; }

cd "$(dirname "$0")/.."

capture() {
  local scenario=$1; shift
  echo "=== $scenario $* - $(date -Is)"
  if ! timeout 1200 uv run python scripts/capture_incident.py \
        "$scenario" --warmup 180 "$@"; then
    echo "!!!ECHEC $scenario - depasse 20 min ou code non nul"
  fi
  sleep 20
}

SCENARIOS=${*:-"connection_leak slow_response bad_release internal_error cpu_saturation memory_leak"}
for s in $SCENARIOS; do
  capture "$s" --holdout
done
capture slow_response

echo "=== termine - $(date -Is)"
