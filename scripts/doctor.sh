#!/usr/bin/env bash

set -euo pipefail

SERVICE_PREFIX="${SERVICE_PREFIX:-migrator}"
API_PORT="${API_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
API_HOST="${API_HOST:-127.0.0.1}"
ENV_FILE="${OCI_MIGRATOR_ENV_FILE:-${HOME}/.oci-migrator.env}"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=()
else
  SUDO=(sudo)
fi

ok() {
  printf '[OK] %s\n' "$*"
}

warn() {
  printf '[WARN] %s\n' "$*"
}

fail_check() {
  printf '[FAIL] %s\n' "$*"
  FAILED=1
}

have_command() {
  command -v "$1" >/dev/null 2>&1
}

check_command() {
  local name="$1"
  local version_cmd="${2:-}"
  if have_command "$name"; then
    if [ -n "$version_cmd" ]; then
      ok "$name: $($version_cmd 2>/dev/null | head -1)"
    else
      ok "$name: $(command -v "$name")"
    fi
  else
    fail_check "$name is not installed"
  fi
}

check_service() {
  local unit="$1"
  if ! have_command systemctl; then
    fail_check "systemctl is not available"
    return
  fi

  if systemctl list-unit-files --no-legend "$unit" 2>/dev/null | awk '{print $1}' | grep -Fxq "$unit"; then
    if systemctl is-active --quiet "$unit"; then
      ok "$unit is active"
    else
      fail_check "$unit is installed but not active"
    fi
  else
    fail_check "$unit is not installed"
  fi
}

check_port() {
  local port="$1"
  local label="$2"
  if have_command ss && "${SUDO[@]}" ss -ltn | awk '{print $4}' | grep -Eq "[:.]$port$"; then
    ok "$label is listening on port $port"
  else
    fail_check "$label is not listening on port $port"
  fi
}

read_env_value() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 1
  grep "^${key}=" "$ENV_FILE" | tail -1 | cut -d= -f2-
}

FAILED=0

echo "OCI Migrator doctor"
echo

if [ -f /etc/os-release ]; then
  . /etc/os-release
  ok "OS: ${PRETTY_NAME:-unknown}"
else
  warn "Could not read /etc/os-release"
fi

check_command python3 "python3 --version"
check_command node "node --version"
check_command npm "npm --version"
check_command rclone "rclone version"
check_command redis-server "redis-server --version"
check_command curl "curl --version"

if [ -f "$ENV_FILE" ]; then
  ok "Env file exists: $ENV_FILE"
  mode="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE" 2>/dev/null || true)"
  if [ "$mode" = "600" ]; then
    ok "Env file permissions are 600"
  else
    warn "Env file permissions are $mode, expected 600"
  fi

  if [ -n "$(read_env_value OCI_MIGRATOR_API_TOKEN || true)" ]; then
    ok "OCI_MIGRATOR_API_TOKEN is configured"
  else
    fail_check "OCI_MIGRATOR_API_TOKEN is missing"
  fi

  if [ -n "$(read_env_value OCI_MIGRATOR_ADMIN_PASSWORD_HASH || true)" ]; then
    ok "OCI_MIGRATOR_ADMIN_PASSWORD_HASH is configured"
  else
    fail_check "OCI_MIGRATOR_ADMIN_PASSWORD_HASH is missing"
  fi
else
  fail_check "Env file missing: $ENV_FILE"
fi

check_service redis-server.service
check_service "$SERVICE_PREFIX-api.service"
check_service "$SERVICE_PREFIX-worker.service"
check_service "$SERVICE_PREFIX-scheduler.timer"
if systemctl list-unit-files --no-legend "$SERVICE_PREFIX-frontend.service" 2>/dev/null | awk '{print $1}' | grep -Fxq "$SERVICE_PREFIX-frontend.service"; then
  check_service "$SERVICE_PREFIX-frontend.service"
fi

check_port "$API_PORT" "Backend"
if systemctl list-unit-files --no-legend "$SERVICE_PREFIX-frontend.service" 2>/dev/null | awk '{print $1}' | grep -Fxq "$SERVICE_PREFIX-frontend.service"; then
  check_port "$FRONTEND_PORT" "Frontend"
fi

if have_command curl && [ -f "$ENV_FILE" ]; then
  token="$(read_env_value OCI_MIGRATOR_API_TOKEN || true)"
  if [ -n "$token" ]; then
    if curl -fsS -H "X-API-Token: $token" "http://$API_HOST:$API_PORT/list-profiles" >/dev/null; then
      ok "Backend API responds to authenticated request"
    else
      fail_check "Backend API did not respond to authenticated request"
    fi
  fi
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "Doctor passed."
else
  echo "Doctor found issues. Check systemctl status and journalctl logs."
fi

exit "$FAILED"
