#!/usr/bin/env bash

set -euo pipefail

SERVICE_PREFIX="${SERVICE_PREFIX:-migrator}"
API_PORT="${API_PORT:-8000}"
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

echo "Cloud Migration Console doctor"
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
check_command logrotate "logrotate --version"

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

  job_log_dir="$(read_env_value OCI_MIGRATOR_JOB_LOG_DIR || true)"
  if [ -n "$job_log_dir" ]; then
    if [ -d "$job_log_dir" ]; then
      ok "Job log directory exists: $job_log_dir"
    else
      fail_check "Job log directory is missing: $job_log_dir"
    fi
  else
    warn "OCI_MIGRATOR_JOB_LOG_DIR is not configured"
  fi

  if [ -n "$(read_env_value OCI_MIGRATOR_JOB_LOG_MAX_SIZE || true)" ]; then
    ok "OCI_MIGRATOR_JOB_LOG_MAX_SIZE is configured"
  else
    warn "OCI_MIGRATOR_JOB_LOG_MAX_SIZE is not configured"
  fi

  if [ -n "$(read_env_value OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS || true)" ]; then
    ok "OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS is configured"
  else
    warn "OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS is not configured"
  fi
else
  fail_check "Env file missing: $ENV_FILE"
fi

check_service redis-server.service
check_service "$SERVICE_PREFIX-api.service"
check_service "$SERVICE_PREFIX-worker.service"
check_service "$SERVICE_PREFIX-scheduler.timer"

check_port "$API_PORT" "App/backend"

if have_command curl; then
  if curl -fsS "http://$API_HOST:$API_PORT/health" >/dev/null; then
    ok "Health endpoint responds"
  else
    fail_check "Health endpoint did not respond"
  fi
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
