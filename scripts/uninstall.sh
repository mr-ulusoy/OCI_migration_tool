#!/usr/bin/env bash

set -euo pipefail

SERVICE_PREFIX="${SERVICE_PREFIX:-migrator}"
PURGE_PROJECT="${PURGE_PROJECT:-0}"
PURGE_DATA="${PURGE_DATA:-0}"
PURGE_LOCAL_DATA="${PURGE_LOCAL_DATA:-0}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${OCI_MIGRATOR_ENV_FILE:-${HOME}/.oci-migrator.env}"
LOCAL_DATA_ROOT="${OCI_MIGRATOR_LOCAL_DATA_ROOT:-/var/lib/oci-migrator/local}"

canonical_path() {
  realpath -m -- "$1" 2>/dev/null || python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

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
  --purge-local-data          Remove only server-local backup data under $LOCAL_DATA_ROOT.
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
    --purge-local-data)
      PURGE_LOCAL_DATA=1
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

if [ "$PURGE_PROJECT" = "1" ] && [ "$PURGE_LOCAL_DATA" != "1" ] && [[ "$LOCAL_DATA_ROOT" = /* ]]; then
  resolved_local_data_root="$(canonical_path "$LOCAL_DATA_ROOT")"
  if [[ "$resolved_local_data_root" == "$PROJECT_DIR"/* ]]; then
    echo "Refusing to remove the project because preserved local backup data is stored inside it: $resolved_local_data_root" >&2
    exit 1
  fi
fi

validate_local_data_root() {
  case "$LOCAL_DATA_ROOT" in
    /*)
      ;;
    *)
      echo "Local data root must be an absolute path: $LOCAL_DATA_ROOT" >&2
      exit 1
      ;;
  esac

  LOCAL_DATA_ROOT="$(canonical_path "$LOCAL_DATA_ROOT")"
  case "$LOCAL_DATA_ROOT" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
      echo "Refusing unsafe local data root: $LOCAL_DATA_ROOT" >&2
      exit 1
      ;;
  esac

  while IFS= read -r mount_target; do
    if [[ "$mount_target" == "$LOCAL_DATA_ROOT"/* ]]; then
      echo "Refusing to delete local data while a nested filesystem is mounted at: $mount_target" >&2
      exit 1
    fi
  done < <(findmnt -rn -o TARGET 2>/dev/null || true)

  local filesystem_type
  filesystem_type="$(findmnt -T "$LOCAL_DATA_ROOT" -n -o FSTYPE 2>/dev/null || true)"
  case "$filesystem_type" in
    nfs|nfs4|cifs|smb3|fuse.sshfs)
      echo "Refusing to delete data from an external mounted filesystem: $filesystem_type" >&2
      exit 1
      ;;
  esac
}

if [ "$PURGE_LOCAL_DATA" = "1" ]; then
  validate_local_data_root
fi

units=(
  "$SERVICE_PREFIX-api.service"
  "$SERVICE_PREFIX-worker.service"
  "$SERVICE_PREFIX-frontend.service"
  "$SERVICE_PREFIX-scheduler.timer"
  "$SERVICE_PREFIX-scheduler.service"
  "$SERVICE_PREFIX-tls.service"
)

echo "Stopping and disabling OCI Migrator services with prefix: $SERVICE_PREFIX"
"${SUDO[@]}" systemctl stop "${units[@]}" 2>/dev/null || true
"${SUDO[@]}" systemctl disable "${units[@]}" 2>/dev/null || true

for unit in "${units[@]}"; do
  "${SUDO[@]}" rm -f "/etc/systemd/system/$unit"
done

"${SUDO[@]}" rm -f \
  "/etc/sudoers.d/$SERVICE_PREFIX-tls" \
  /usr/local/sbin/oci-migrator-tls \
  /etc/oci-migrator/tls.conf \
  /etc/oci-migrator/Caddyfile
"${SUDO[@]}" rm -rf /var/lib/oci-migrator/tls

"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl reset-failed >/dev/null 2>&1 || true

if [ "$PURGE_LOCAL_DATA" = "1" ]; then
  if [ -d "$LOCAL_DATA_ROOT" ]; then
    echo "Removing server-local backup data only: $LOCAL_DATA_ROOT"
    "${SUDO[@]}" find "$LOCAL_DATA_ROOT" -xdev -mindepth 1 -delete
    "${SUDO[@]}" rmdir "$LOCAL_DATA_ROOT" 2>/dev/null || true
  else
    echo "Server-local backup data directory does not exist: $LOCAL_DATA_ROOT"
  fi
else
  echo "Server-local backup data preserved: $LOCAL_DATA_ROOT"
fi

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
