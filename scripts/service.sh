#!/usr/bin/env bash

set -euo pipefail

SERVICE_PREFIX="${SERVICE_PREFIX:-migrator}"
ACTION="${1:-status}"

units=(
  "$SERVICE_PREFIX-api.service"
  "$SERVICE_PREFIX-worker.service"
  "$SERVICE_PREFIX-scheduler.timer"
)

usage() {
  cat <<EOF
Usage:
  scripts/service.sh status|start|stop|restart|logs [unit]

Examples:
  scripts/service.sh status
  scripts/service.sh restart
  scripts/service.sh logs api
  SERVICE_PREFIX=migrator-dev scripts/service.sh status
EOF
}

normalize_unit() {
  case "${1:-}" in
    api) printf '%s-api.service\n' "$SERVICE_PREFIX" ;;
    worker) printf '%s-worker.service\n' "$SERVICE_PREFIX" ;;
    frontend) printf '%s-api.service\n' "$SERVICE_PREFIX" ;;
    scheduler) printf '%s-scheduler.timer\n' "$SERVICE_PREFIX" ;;
    *.service|*.timer) printf '%s\n' "$1" ;;
    "") printf '%s-api.service\n' "$SERVICE_PREFIX" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

case "$ACTION" in
  status)
    sudo systemctl status "${units[@]}"
    ;;
  start|stop|restart)
    sudo systemctl "$ACTION" "${units[@]}"
    ;;
  logs)
    unit="$(normalize_unit "${2:-api}")"
    journalctl -u "$unit" -f
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
