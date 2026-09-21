#!/usr/bin/env bash
# Lance / arrête les trois services du bac à sable en arrière-plan.
# Les logs JSON vont dans sandbox/logs/<service>.log, lus par Alloy.
set -uo pipefail

SANDBOX="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SANDBOX")"
UVICORN="$ROOT/.venv/bin/uvicorn"
LOGS="$SANDBOX/logs"
PIDS="$SANDBOX/.pids"
mkdir -p "$LOGS" "$PIDS"

declare -A PORT=(
  [inventory-service]=8003
  [payment-service]=8002
  [checkout-service]=8001
)
ALL=(inventory-service payment-service checkout-service)

is_running() {
  local pidfile="$PIDS/$1.pid"
  [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

start_one() {
  local svc="$1"
  if is_running "$svc"; then
    echo "  $svc déjà actif"
    return
  fi
  (
    cd "$SANDBOX/$svc" || exit 1
    PYTHONUNBUFFERED=1 nohup "$UVICORN" main:app \
      --host 0.0.0.0 --port "${PORT[$svc]}" \
      --log-level warning --no-access-log \
      >> "$LOGS/$svc.log" 2>&1 &
    echo $! > "$PIDS/$svc.pid"
  )
  echo "  $svc démarré sur :${PORT[$svc]}"
}

stop_one() {
  local svc="$1"
  if is_running "$svc"; then
    kill "$(cat "$PIDS/$svc.pid")"
    echo "  $svc arrêté"
  else
    echo "  $svc n'était pas actif"
  fi
  rm -f "$PIDS/$svc.pid"
}

status_one() {
  if is_running "$1"; then
    echo "  $1  UP    :${PORT[$1]}  pid $(cat "$PIDS/$1.pid")"
  else
    echo "  $1  DOWN"
  fi
}

cmd="${1:-status}"
shift || true
if [[ $# -gt 0 ]]; then targets=("$@"); else targets=("${ALL[@]}"); fi

for svc in "${targets[@]}"; do
  if [[ -z "${PORT[$svc]:-}" ]]; then
    echo "Service inconnu : $svc"
    exit 1
  fi
done

case "$cmd" in
  start)   for s in "${targets[@]}"; do start_one "$s"; done ;;
  stop)    for s in "${targets[@]}"; do stop_one "$s"; done ;;
  restart) for s in "${targets[@]}"; do stop_one "$s"; done
           sleep 1
           for s in "${targets[@]}"; do start_one "$s"; done ;;
  status)  for s in "${targets[@]}"; do status_one "$s"; done ;;
  *) echo "Usage: $0 {start|stop|restart|status} [service...]"; exit 1 ;;
esac