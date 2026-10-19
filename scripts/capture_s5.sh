#!/bin/bash
# Campagne de captures pour S5 : deux par scénario, warmup=180.
#
# Le warmup long est nécessaire depuis S4-J2 : avec 60 s, la baseline ne
# compte que cinq points et le détecteur de fenêtre manque le bon signal.
#
# Les captures de S2 ne contiennent pas metrics_raw et ne sont pas
# rejouables.
set -e
cd "$(dirname "$0")/.."

for scenario in connection_leak slow_response bad_release internal_error cpu_saturation; do
  for i in 1 2; do
    uv run python scripts/capture_incident.py "$scenario" \
      --duration 180 --warmup 180 --rps 6
    sleep 15
  done
done

# memory_leak demande une fenêtre longue (mesuré en S2-J3 : la tendance
# n'est lisible qu'au-delà de dix minutes) et un débit faible, le stock
# s'épuisant au-delà d'environ 1 700 requêtes.
for i in 1 2; do
  uv run python scripts/capture_incident.py memory_leak \
    --duration 420 --warmup 180 --rps 4
  sleep 15
done