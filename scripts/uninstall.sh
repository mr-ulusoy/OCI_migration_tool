#!/usr/bin/env bash

set -euo pipefail

SERVICE_PREFIX="${SERVICE_PREFIX:-migrator}"
PURGE_PROJECT="${PURGE_PROJECT:-0}"
PURGE_DATA="${PURGE_DATA:-0}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${OCI_MIGRATOR_ENV_FILE:-${HOME}/.oci-migrator.env}"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=()
else
  SUDO=(sudo)
fi

usage() {
  cat <<EOF
Uninstall OCI Migrator systemd services.

Usage:
  scripts/uninstall.sh [options]

Options:
  --service-prefix NAME       Service prefix. Default: $SERVICE_PREFIX
  --purge-project             Remove this project directory after removing services.
  --purge-data                Remove env file and OCI/rclone config for the current user.
  -h, --help                  Show this help.

By default this preserves project files, ~/.oci-migrator.env, ~/.oci, and ~/.config/rclone.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --service-prefix)
      SERVICE_PREFIX="$2"
      shift 2
      ;;
    --purge-project)
      PURGE_PROJECT=1
      shift
      ;;
    --purge-data)
      PURGE_DATA=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

units=(
  "$SERVICE_PREFIX-api.service"
  "$SERVICE_PREFIX-worker.service"
  "$SERVICE_PREFIX-frontend.service"
  "$SERVICE_PREFIX-scheduler.timer"
  "$SERVICE_PREFIX-scheduler.service"
)

echo "Stopping and disabling OCI Migrator services with prefix: $SERVICE_PREFIX"
"${SUDO[@]}" systemctl stop "${units[@]}" 2>/dev/null || true
"${SUDO[@]}" systemctl disable "${units[@]}" 2>/dev/null || true

for unit in "${units[@]}"; do
  "${SUDO[@]}" rm -f "/etc/systemd/system/$unit"
done

"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl reset-failed >/dev/null 2>&1 || true

if [ "$PURGE_DATA" = "1" ]; then
  echo "Removing runtime data for current user"
  rm -f "$ENV_FILE"
  rm -rf "$HOME/.oci"
  rm -rf "$HOME/.config/rclone"
else
  echo "Runtime data preserved: $ENV_FILE, ~/.oci, ~/.config/rclone"
fi

if [ "$PURGE_PROJECT" = "1" ]; then
  echo "Removing project directory: $PROJECT_DIR"
  cd /
  "${SUDO[@]}" rm -rf "$PROJECT_DIR"
else
  echo "Project directory preserved: $PROJECT_DIR"
fi

echo "Uninstall complete."
