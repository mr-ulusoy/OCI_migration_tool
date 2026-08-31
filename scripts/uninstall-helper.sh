#!/usr/bin/env bash

set -euo pipefail

CONFIG_FILE="${OCI_MIGRATOR_UNINSTALL_CONFIG:-/etc/oci-migrator/uninstall.conf}"
HELPER_PATH="$(realpath "${BASH_SOURCE[0]}")"
SCHEDULE_LOCK="/run/oci-migrator-uninstall-scheduled"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

load_config() {
  [ "$(id -u)" -eq 0 ] || fail "This helper must run as root."
  [ -f "$CONFIG_FILE" ] || fail "Missing uninstall configuration: $CONFIG_FILE"
  # The installer creates this root-owned file with shell-escaped values.
  # shellcheck source=/dev/null
  source "$CONFIG_FILE"
  : "${PROJECT_DIR:?Missing PROJECT_DIR}"
  : "${SERVICE_PREFIX:?Missing SERVICE_PREFIX}"
  : "${ENV_FILE:?Missing ENV_FILE}"
  : "${LOCAL_DATA_ROOT:?Missing LOCAL_DATA_ROOT}"
  [[ "$PROJECT_DIR" = /* ]] || fail "PROJECT_DIR must be absolute."
  [[ "$ENV_FILE" = /* ]] || fail "ENV_FILE must be absolute."
  [[ "$LOCAL_DATA_ROOT" = /* ]] || fail "LOCAL_DATA_ROOT must be absolute."
  [[ "$SERVICE_PREFIX" =~ ^[A-Za-z0-9_-]+$ ]] || fail "Invalid SERVICE_PREFIX."
}

execute_uninstall() {
  local purge_local_data="${1:-0}"
  local uninstall_script="$PROJECT_DIR/scripts/uninstall.sh"
  [ -x "$uninstall_script" ] || fail "Missing uninstall script: $uninstall_script"

  local args=(--service-prefix "$SERVICE_PREFIX" --purge-project)
  if [ "$purge_local_data" = "1" ]; then
    args+=(--purge-local-data)
  fi

  OCI_MIGRATOR_ENV_FILE="$ENV_FILE" \
  OCI_MIGRATOR_LOCAL_DATA_ROOT="$LOCAL_DATA_ROOT" \
    "$uninstall_script" "${args[@]}"

  rm -f "/etc/sudoers.d/${SERVICE_PREFIX}-uninstall" "$CONFIG_FILE" "$HELPER_PATH"
}

schedule_uninstall() {
  local purge_local_data="${1:-0}"
  mkdir "$SCHEDULE_LOCK" 2>/dev/null || fail "An uninstall is already scheduled."

  local unit="oci-migrator-uninstall-$(date +%s)"
  if ! systemd-run \
    --quiet \
    --collect \
    --unit="$unit" \
    --on-active=2s \
    "$HELPER_PATH" execute "$purge_local_data"; then
    rmdir "$SCHEDULE_LOCK" 2>/dev/null || true
    fail "Could not schedule uninstall."
  fi

  printf '{"status":"scheduled","purge_local_backups":%s}\n' "$([ "$purge_local_data" = "1" ] && printf true || printf false)"
}

case "${1:-}" in
  schedule)
    load_config
    case "${2:-}" in
      "") schedule_uninstall 0 ;;
      --purge-local-data) schedule_uninstall 1 ;;
      *) fail "Unsupported schedule option." ;;
    esac
    ;;
  execute)
    load_config
    trap 'rmdir "$SCHEDULE_LOCK" 2>/dev/null || true' EXIT
    case "${2:-0}" in
      0|1) execute_uninstall "${2:-0}" ;;
      *) fail "Invalid purge flag." ;;
    esac
    ;;
  *)
    fail "Usage: $0 schedule [--purge-local-data]"
    ;;
esac
