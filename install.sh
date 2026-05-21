#!/usr/bin/env bash

set -euo pipefail

APP_NAME="${APP_NAME:-oci-migrator}"
SERVICE_PREFIX="${SERVICE_PREFIX:-migrator}"
API_HOST="${API_HOST:-0.0.0.0}"
API_PORT="${API_PORT:-8000}"
LOCAL_DATA_ROOT="${OCI_MIGRATOR_LOCAL_DATA_ROOT:-/var/lib/oci-migrator/local}"
JOB_LOG_DIR="${OCI_MIGRATOR_JOB_LOG_DIR:-/var/log/oci-migrator/jobs}"
JOB_LOG_MAX_SIZE="${OCI_MIGRATOR_JOB_LOG_MAX_SIZE:-10M}"
JOB_LOG_RETENTION_DAYS="${OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS:-14}"
JOB_LOG_HELPER="${OCI_MIGRATOR_JOB_LOG_HELPER:-/usr/local/sbin/oci-migrator-job-log}"
LOCAL_SHARE_HELPER="${OCI_MIGRATOR_LOCAL_SHARE_HELPER:-/usr/local/sbin/oci-migrator-local-share}"
LOCAL_SHARE_CONFIG="/etc/oci-migrator/local-share.conf"
UPGRADE_HELPER="${OCI_MIGRATOR_UPGRADE_HELPER:-/usr/local/sbin/oci-migrator-upgrade}"
UPGRADE_CONFIG="${OCI_MIGRATOR_UPGRADE_CONFIG:-/etc/oci-migrator/upgrade.conf}"
UPGRADE_STATE_DIR="${OCI_MIGRATOR_UPGRADE_STATE_DIR:-/var/lib/oci-migrator/upgrade}"
UPGRADE_STATUS_FILE="${OCI_MIGRATOR_UPGRADE_STATUS_FILE:-$UPGRADE_STATE_DIR/status.json}"
UPGRADE_LOG_FILE="${OCI_MIGRATOR_UPGRADE_LOG_FILE:-/var/log/oci-migrator/upgrade.log}"
JOB_LOG_DIR_PROVIDED=0
JOB_LOG_MAX_SIZE_PROVIDED=0
JOB_LOG_RETENTION_DAYS_PROVIDED=0
if [ -n "${OCI_MIGRATOR_JOB_LOG_DIR:-}" ]; then
  JOB_LOG_DIR_PROVIDED=1
fi
if [ -n "${OCI_MIGRATOR_JOB_LOG_MAX_SIZE:-}" ]; then
  JOB_LOG_MAX_SIZE_PROVIDED=1
fi
if [ -n "${OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS:-}" ]; then
  JOB_LOG_RETENTION_DAYS_PROVIDED=1
fi
CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-2}"
OPEN_FIREWALL="${OPEN_FIREWALL:-0}"
STOP_LEGACY_PROCESSES="${STOP_LEGACY_PROCESSES:-0}"
PRINT_TOKEN="${PRINT_TOKEN:-0}"
ADMIN_USERNAME="${OCI_MIGRATOR_ADMIN_USERNAME:-admin}"
ADMIN_USERNAME_PROVIDED=0
if [ -n "${OCI_MIGRATOR_ADMIN_USERNAME:-}" ]; then
  ADMIN_USERNAME_PROVIDED=1
fi
ADMIN_PASSWORD="${OCI_MIGRATOR_ADMIN_PASSWORD:-}"
ADMIN_PASSWORD_FILE="${OCI_MIGRATOR_ADMIN_PASSWORD_FILE:-}"
PROMPT_ADMIN_PASSWORD="${PROMPT_ADMIN_PASSWORD:-0}"
GENERATED_ADMIN_PASSWORD=""
ADMIN_PASSWORD_UPDATED=0
RUN_USER="${RUN_USER:-}"
OCI_MIGRATOR_ENV_FILE="${OCI_MIGRATOR_ENV_FILE:-}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"
VENV_DIR="$PROJECT_DIR/venv"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=()
else
  SUDO=(sudo)
fi

log() {
  printf '\n[%s] %s\n' "$APP_NAME" "$*"
}

fail() {
  printf '\n[%s] ERROR: %s\n' "$APP_NAME" "$*" >&2
  exit 1
}

usage() {
  cat <<EOF
OCI Migrator installer

Usage:
  ./install.sh [options]

Options:
  --public-host HOST          Public IP/DNS used for CORS allowlist and printed URLs.
  --api-port PORT             Backend port. Default: $API_PORT
  --service-prefix NAME       systemd service prefix. Default: $SERVICE_PREFIX
  --run-user USER             Linux user that runs services. Default: current sudo user.
  --env-file PATH             Runtime env file. Default: ~/.oci-migrator.env for the run user.
  --local-data-root PATH      Managed server-local source folder root. Default: $LOCAL_DATA_ROOT
  --job-log-dir PATH          Persistent rclone job log directory. Default: $JOB_LOG_DIR
  --job-log-max-size SIZE     logrotate maxsize for job logs. Default: $JOB_LOG_MAX_SIZE
  --job-log-retention-days N  Number of daily rotated logs to keep. Default: $JOB_LOG_RETENTION_DAYS
  --job-log-helper PATH       Root helper used by the UI for job log rotation. Default: $JOB_LOG_HELPER
  --local-share-helper PATH   Root helper used by the UI for optional SMB shares. Default: $LOCAL_SHARE_HELPER
  --upgrade-helper PATH       Root helper used by the UI for controlled upgrades. Default: $UPGRADE_HELPER
  --celery-concurrency N      Celery worker concurrency. Default: $CELERY_CONCURRENCY
  --open-firewall             Open local firewall ports with ufw/iptables when possible.
  --stop-legacy-processes     Stop old manual uvicorn/vite processes from this project path.
  --admin-username USERNAME   Admin login username. Default: $ADMIN_USERNAME
  --admin-password PASSWORD   Set or reset the admin password.
  --admin-password-file PATH  Read admin password from a file.
  --prompt-admin-password     Prompt for admin password without storing it in shell history.
  --print-token               Print API token in the final summary.
  -h, --help                  Show this help.

Environment variables with the same names are also supported:
  PUBLIC_HOST, API_PORT, SERVICE_PREFIX, RUN_USER, OCI_MIGRATOR_LOCAL_DATA_ROOT,
  OCI_MIGRATOR_JOB_LOG_DIR, OCI_MIGRATOR_JOB_LOG_MAX_SIZE,
  OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS, OCI_MIGRATOR_JOB_LOG_HELPER,
  OCI_MIGRATOR_LOCAL_SHARE_HELPER, OCI_MIGRATOR_UPGRADE_HELPER,
  OCI_MIGRATOR_ENV_FILE, OPEN_FIREWALL, STOP_LEGACY_PROCESSES,
  CELERY_CONCURRENCY, PRINT_TOKEN,
  OCI_MIGRATOR_ADMIN_USERNAME, OCI_MIGRATOR_ADMIN_PASSWORD,
  OCI_MIGRATOR_ADMIN_PASSWORD_FILE, PROMPT_ADMIN_PASSWORD.
EOF
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --public-host)
        PUBLIC_HOST="$2"
        shift 2
        ;;
      --api-port)
        API_PORT="$2"
        shift 2
        ;;
      --frontend-port)
        # Deprecated no-op kept for backwards-compatible install commands.
        shift 2
        ;;
      --service-prefix)
        SERVICE_PREFIX="$2"
        shift 2
        ;;
      --run-user)
        RUN_USER="$2"
        shift 2
        ;;
      --env-file)
        OCI_MIGRATOR_ENV_FILE="$2"
        shift 2
        ;;
      --local-data-root)
        LOCAL_DATA_ROOT="$2"
        shift 2
        ;;
      --job-log-dir)
        JOB_LOG_DIR="$2"
        JOB_LOG_DIR_PROVIDED=1
        shift 2
        ;;
      --job-log-max-size)
        JOB_LOG_MAX_SIZE="$2"
        JOB_LOG_MAX_SIZE_PROVIDED=1
        shift 2
        ;;
      --job-log-retention-days)
        JOB_LOG_RETENTION_DAYS="$2"
        JOB_LOG_RETENTION_DAYS_PROVIDED=1
        shift 2
        ;;
      --job-log-helper)
        JOB_LOG_HELPER="$2"
        shift 2
        ;;
      --local-share-helper)
        LOCAL_SHARE_HELPER="$2"
        shift 2
        ;;
      --upgrade-helper)
        UPGRADE_HELPER="$2"
        shift 2
        ;;
      --celery-concurrency)
        CELERY_CONCURRENCY="$2"
        shift 2
        ;;
      --open-firewall)
        OPEN_FIREWALL=1
        shift
        ;;
      --stop-legacy-processes)
        STOP_LEGACY_PROCESSES=1
        shift
        ;;
      --no-frontend-service)
        # Kept for backwards-compatible install commands.
        shift
        ;;
      --admin-username)
        ADMIN_USERNAME="$2"
        ADMIN_USERNAME_PROVIDED=1
        shift 2
        ;;
      --admin-password)
        ADMIN_PASSWORD="$2"
        shift 2
        ;;
      --admin-password-file)
        ADMIN_PASSWORD_FILE="$2"
        shift 2
        ;;
      --prompt-admin-password)
        PROMPT_ADMIN_PASSWORD=1
        shift
        ;;
      --print-token)
        PRINT_TOKEN=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        fail "Unknown option: $1"
        ;;
    esac
  done
}

initialize_runtime_paths() {
  if [ -z "$RUN_USER" ]; then
    if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER:-}" != "root" ]; then
      RUN_USER="$SUDO_USER"
    else
      RUN_USER="$(id -un)"
    fi
  fi

  USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
  [ -n "$USER_HOME" ] || fail "Unable to find home directory for run user: $RUN_USER"

  ENV_FILE="${OCI_MIGRATOR_ENV_FILE:-$USER_HOME/.oci-migrator.env}"
}

validate_job_log_settings() {
  case "$JOB_LOG_DIR" in
    /*)
      ;;
    *)
      fail "--job-log-dir must be an absolute path."
      ;;
  esac

  if ! [[ "$JOB_LOG_MAX_SIZE" =~ ^[1-9][0-9]*[KkMmGg]?$ ]]; then
    fail "--job-log-max-size must look like 10M, 512K, or 1G."
  fi

  if ! [[ "$JOB_LOG_RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
    fail "--job-log-retention-days must be a number."
  fi
  if [ "$JOB_LOG_RETENTION_DAYS" -lt 1 ] || [ "$JOB_LOG_RETENTION_DAYS" -gt 365 ]; then
    fail "--job-log-retention-days must be between 1 and 365."
  fi

  case "$JOB_LOG_HELPER" in
    /*)
      ;;
    *)
      fail "--job-log-helper must be an absolute path."
      ;;
  esac
}

run_as_user() {
  if [ "$(id -un)" = "$RUN_USER" ]; then
    "$@"
  else
    "${SUDO[@]}" -H -u "$RUN_USER" "$@"
  fi
}

run_as_user_in_dir() {
  local dir="$1"
  shift

  if [ "$(id -un)" = "$RUN_USER" ]; then
    (cd "$dir" && "$@")
  else
    "${SUDO[@]}" -H -u "$RUN_USER" bash -c 'cd "$1" && shift && "$@"' bash "$dir" "$@"
  fi
}

detect_public_host() {
  if [ -n "${PUBLIC_HOST:-}" ]; then
    printf '%s\n' "$PUBLIC_HOST"
    return
  fi

  if command -v hostname >/dev/null 2>&1; then
    hostname -I 2>/dev/null | awk '{print $1}' | grep -E '.+' || true
  fi
}

generate_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    python3 -c 'import secrets; print(secrets.token_hex(32))'
  fi
}

generate_admin_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 24 | tr -d '\n'
  else
    python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
  fi
}

hash_admin_password() {
  ADMIN_PASSWORD_TO_HASH="$1" python3 - <<'PY'
import base64
import hashlib
import os

password = os.environ["ADMIN_PASSWORD_TO_HASH"].encode("utf-8")
salt = os.urandom(16)
iterations = 390000
digest = hashlib.pbkdf2_hmac("sha256", password, salt, iterations)
print(
    "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )
)
PY
}

validate_admin_password() {
  local password="$1"
  if [ "${#password}" -lt 12 ]; then
    fail "Admin password must be at least 12 characters."
  fi
}

load_admin_password_input() {
  if [ -n "$ADMIN_PASSWORD_FILE" ]; then
    [ -f "$ADMIN_PASSWORD_FILE" ] || fail "Admin password file does not exist: $ADMIN_PASSWORD_FILE"
    ADMIN_PASSWORD="$(tr -d '\r\n' < "$ADMIN_PASSWORD_FILE")"
  fi

  if [ "$PROMPT_ADMIN_PASSWORD" = "1" ]; then
    local first second
    read -r -s -p "Admin password: " first
    printf '\n'
    read -r -s -p "Confirm admin password: " second
    printf '\n'
    [ "$first" = "$second" ] || fail "Admin passwords did not match."
    ADMIN_PASSWORD="$first"
  fi

  if [ -n "$ADMIN_PASSWORD" ]; then
    validate_admin_password "$ADMIN_PASSWORD"
  fi
}

set_env_value() {
  local key="$1"
  local value="$2"
  local temp_file
  temp_file="$(mktemp)"

  if [ -f "$ENV_FILE" ]; then
    awk -v key="$key" -v value="$value" '
      BEGIN { updated = 0 }
      $0 ~ "^" key "=" {
        print key "=" value
        updated = 1
        next
      }
      { print }
      END {
        if (!updated) {
          print key "=" value
        }
      }
    ' "$ENV_FILE" > "$temp_file"
  else
    printf '%s=%s\n' "$key" "$value" > "$temp_file"
  fi

  "${SUDO[@]}" install -o "$RUN_USER" -g "$RUN_USER" -m 600 "$temp_file" "$ENV_FILE"
  rm -f "$temp_file"
}

ensure_supported_os() {
  command -v apt-get >/dev/null 2>&1 || fail "This installer currently targets Ubuntu/Debian hosts with apt-get."
  command -v systemctl >/dev/null 2>&1 || fail "systemd is required for service installation."
}

install_system_dependencies() {
  log "Installing system dependencies"
  "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get update
  "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates \
    curl \
    gnupg \
    iptables \
    logrotate \
    openssl \
    python3 \
    python3-pip \
    python3-venv \
    redis-server \
    sudo \
    unzip
}

install_node() {
  if command -v node >/dev/null 2>&1; then
    local major
    major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || printf '0')"
    if [ "$major" -ge 20 ]; then
      log "Node.js $(node -v) is already installed"
      return
    fi
  fi

  log "Installing Node.js 20.x"
  curl -fsSL https://deb.nodesource.com/setup_20.x -o /tmp/nodesource_setup_20.x.sh
  "${SUDO[@]}" -E bash /tmp/nodesource_setup_20.x.sh
  "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs
}

install_rclone() {
  if command -v rclone >/dev/null 2>&1; then
    log "$(rclone version | head -1) is already installed"
    return
  fi

  log "Installing rclone"
  curl -fsSL https://rclone.org/install.sh -o /tmp/rclone-install.sh
  "${SUDO[@]}" bash /tmp/rclone-install.sh
}

ensure_env_file() {
  local detected_host
  detected_host="$(detect_public_host)"
  PUBLIC_HOST="${PUBLIC_HOST:-${detected_host:-localhost}}"

  local allowed_origins
  allowed_origins="${OCI_MIGRATOR_ALLOWED_ORIGINS:-http://localhost:$API_PORT,http://127.0.0.1:$API_PORT,http://$PUBLIC_HOST:$API_PORT}"

  if [ ! -f "$ENV_FILE" ]; then
    log "Creating $ENV_FILE"
    local token
    token="${OCI_MIGRATOR_API_TOKEN:-$(generate_token)}"
    local admin_password_hash
    if [ -z "$ADMIN_PASSWORD" ]; then
      ADMIN_PASSWORD="$(generate_admin_password)"
      GENERATED_ADMIN_PASSWORD="$ADMIN_PASSWORD"
    fi
    admin_password_hash="$(hash_admin_password "$ADMIN_PASSWORD")"
    if [ -z "$GENERATED_ADMIN_PASSWORD" ]; then
      ADMIN_PASSWORD_UPDATED=1
    fi

    local temp_file
    temp_file="$(mktemp)"
    {
      printf 'OCI_MIGRATOR_API_TOKEN=%s\n' "$token"
      printf 'OCI_MIGRATOR_ADMIN_USERNAME=%s\n' "$ADMIN_USERNAME"
      printf 'OCI_MIGRATOR_ADMIN_PASSWORD_HASH=%s\n' "$admin_password_hash"
      printf 'OCI_MIGRATOR_SESSION_TTL_SECONDS=43200\n'
      printf 'OCI_MIGRATOR_ALLOWED_ORIGINS=%s\n' "$allowed_origins"
      printf 'OCI_MIGRATOR_REDIS_URL=redis://localhost:6379/0\n'
      printf 'OCI_MIGRATOR_LOG_LEVEL=INFO\n'
      printf 'OCI_MIGRATOR_RCLONE_TIMEOUT_SECONDS=7200\n'
      printf 'OCI_MIGRATOR_LOCAL_DATA_ROOT=%s\n' "$LOCAL_DATA_ROOT"
      printf 'OCI_MIGRATOR_JOB_LOG_DIR=%s\n' "$JOB_LOG_DIR"
      printf 'OCI_MIGRATOR_JOB_LOG_MAX_SIZE=%s\n' "$JOB_LOG_MAX_SIZE"
      printf 'OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS=%s\n' "$JOB_LOG_RETENTION_DAYS"
      printf 'OCI_MIGRATOR_JOB_LOG_HELPER=%s\n' "$JOB_LOG_HELPER"
      printf 'OCI_MIGRATOR_JOB_LOGROTATE_FILE=/etc/logrotate.d/%s-job-logs\n' "$SERVICE_PREFIX"
      printf 'OCI_MIGRATOR_LOCAL_SHARE_HELPER=%s\n' "$LOCAL_SHARE_HELPER"
      printf 'OCI_MIGRATOR_LOCAL_SHARE_TIMEOUT_SECONDS=300\n'
      printf 'OCI_MIGRATOR_UPGRADE_HELPER=%s\n' "$UPGRADE_HELPER"
      printf 'OCI_MIGRATOR_UPGRADE_STATUS_FILE=%s\n' "$UPGRADE_STATUS_FILE"
      printf 'OCI_MIGRATOR_UPGRADE_LOG_FILE=%s\n' "$UPGRADE_LOG_FILE"
    } > "$temp_file"

    "${SUDO[@]}" install -o "$RUN_USER" -g "$RUN_USER" -m 600 "$temp_file" "$ENV_FILE"
    rm -f "$temp_file"
  else
    log "Keeping existing $ENV_FILE"
    "${SUDO[@]}" chmod 600 "$ENV_FILE" || true
    "${SUDO[@]}" chown "$RUN_USER:$RUN_USER" "$ENV_FILE" || true

    grep -q '^OCI_MIGRATOR_ALLOWED_ORIGINS=' "$ENV_FILE" || printf 'OCI_MIGRATOR_ALLOWED_ORIGINS=%s\n' "$allowed_origins" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_REDIS_URL=' "$ENV_FILE" || printf 'OCI_MIGRATOR_REDIS_URL=redis://localhost:6379/0\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_LOG_LEVEL=' "$ENV_FILE" || printf 'OCI_MIGRATOR_LOG_LEVEL=INFO\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_RCLONE_TIMEOUT_SECONDS=' "$ENV_FILE" || printf 'OCI_MIGRATOR_RCLONE_TIMEOUT_SECONDS=7200\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_LOCAL_DATA_ROOT=' "$ENV_FILE" || printf 'OCI_MIGRATOR_LOCAL_DATA_ROOT=%s\n' "$LOCAL_DATA_ROOT" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_JOB_LOG_DIR=' "$ENV_FILE" || printf 'OCI_MIGRATOR_JOB_LOG_DIR=%s\n' "$JOB_LOG_DIR" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_JOB_LOG_MAX_SIZE=' "$ENV_FILE" || printf 'OCI_MIGRATOR_JOB_LOG_MAX_SIZE=%s\n' "$JOB_LOG_MAX_SIZE" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS=' "$ENV_FILE" || printf 'OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS=%s\n' "$JOB_LOG_RETENTION_DAYS" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_JOB_LOG_HELPER=' "$ENV_FILE" || printf 'OCI_MIGRATOR_JOB_LOG_HELPER=%s\n' "$JOB_LOG_HELPER" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_JOB_LOGROTATE_FILE=' "$ENV_FILE" || printf 'OCI_MIGRATOR_JOB_LOGROTATE_FILE=/etc/logrotate.d/%s-job-logs\n' "$SERVICE_PREFIX" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_LOCAL_SHARE_HELPER=' "$ENV_FILE" || printf 'OCI_MIGRATOR_LOCAL_SHARE_HELPER=%s\n' "$LOCAL_SHARE_HELPER" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_LOCAL_SHARE_TIMEOUT_SECONDS=' "$ENV_FILE" || printf 'OCI_MIGRATOR_LOCAL_SHARE_TIMEOUT_SECONDS=300\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_UPGRADE_HELPER=' "$ENV_FILE" || printf 'OCI_MIGRATOR_UPGRADE_HELPER=%s\n' "$UPGRADE_HELPER" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_UPGRADE_STATUS_FILE=' "$ENV_FILE" || printf 'OCI_MIGRATOR_UPGRADE_STATUS_FILE=%s\n' "$UPGRADE_STATUS_FILE" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_UPGRADE_LOG_FILE=' "$ENV_FILE" || printf 'OCI_MIGRATOR_UPGRADE_LOG_FILE=%s\n' "$UPGRADE_LOG_FILE" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null

    if [ "$ADMIN_USERNAME_PROVIDED" = "1" ] || ! grep -q '^OCI_MIGRATOR_ADMIN_USERNAME=' "$ENV_FILE"; then
      set_env_value "OCI_MIGRATOR_ADMIN_USERNAME" "$ADMIN_USERNAME"
    fi
    if [ "$JOB_LOG_DIR_PROVIDED" = "1" ]; then
      set_env_value "OCI_MIGRATOR_JOB_LOG_DIR" "$JOB_LOG_DIR"
    fi
    if [ "$JOB_LOG_MAX_SIZE_PROVIDED" = "1" ]; then
      set_env_value "OCI_MIGRATOR_JOB_LOG_MAX_SIZE" "$JOB_LOG_MAX_SIZE"
    fi
    if [ "$JOB_LOG_RETENTION_DAYS_PROVIDED" = "1" ]; then
      set_env_value "OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS" "$JOB_LOG_RETENTION_DAYS"
    fi
    grep -q '^OCI_MIGRATOR_SESSION_TTL_SECONDS=' "$ENV_FILE" || set_env_value "OCI_MIGRATOR_SESSION_TTL_SECONDS" "43200"

    if [ -n "$ADMIN_PASSWORD" ]; then
      set_env_value "OCI_MIGRATOR_ADMIN_PASSWORD_HASH" "$(hash_admin_password "$ADMIN_PASSWORD")"
      ADMIN_PASSWORD_UPDATED=1
    elif ! grep -q '^OCI_MIGRATOR_ADMIN_PASSWORD_HASH=' "$ENV_FILE"; then
      ADMIN_PASSWORD="$(generate_admin_password)"
      GENERATED_ADMIN_PASSWORD="$ADMIN_PASSWORD"
      set_env_value "OCI_MIGRATOR_ADMIN_PASSWORD_HASH" "$(hash_admin_password "$ADMIN_PASSWORD")"
    fi
  fi

  local configured_admin_username
  configured_admin_username="$(grep '^OCI_MIGRATOR_ADMIN_USERNAME=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
  if [ -n "$configured_admin_username" ]; then
    ADMIN_USERNAME="$configured_admin_username"
  fi
}

load_job_log_settings_from_env() {
  [ -f "$ENV_FILE" ] || return 0

  local configured_job_log_dir configured_job_log_max_size
  configured_job_log_dir="$(grep '^OCI_MIGRATOR_JOB_LOG_DIR=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
  configured_job_log_max_size="$(grep '^OCI_MIGRATOR_JOB_LOG_MAX_SIZE=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
  local configured_job_log_retention_days configured_job_log_helper
  configured_job_log_retention_days="$(grep '^OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
  configured_job_log_helper="$(grep '^OCI_MIGRATOR_JOB_LOG_HELPER=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"

  if [ "$JOB_LOG_DIR_PROVIDED" = "0" ] && [ -n "$configured_job_log_dir" ]; then
    JOB_LOG_DIR="$configured_job_log_dir"
  fi
  if [ "$JOB_LOG_MAX_SIZE_PROVIDED" = "0" ] && [ -n "$configured_job_log_max_size" ]; then
    JOB_LOG_MAX_SIZE="$configured_job_log_max_size"
  fi
  if [ "$JOB_LOG_RETENTION_DAYS_PROVIDED" = "0" ] && [ -n "$configured_job_log_retention_days" ]; then
    JOB_LOG_RETENTION_DAYS="$configured_job_log_retention_days"
  fi
  if [ -n "$configured_job_log_helper" ]; then
    JOB_LOG_HELPER="$configured_job_log_helper"
  fi
}

ensure_local_data_root() {
  log "Preparing managed local data root"
  "${SUDO[@]}" install -d -o "$RUN_USER" -g "$RUN_USER" -m 775 "$LOCAL_DATA_ROOT"
}

ensure_job_log_dir() {
  log "Preparing persistent job log directory"
  "${SUDO[@]}" install -d -o "$RUN_USER" -g "$RUN_USER" -m 750 "$JOB_LOG_DIR"
}

install_job_logrotate_config() {
  log "Installing job log rotation"

  local logrotate_file candidate_file temp_file
  logrotate_file="/etc/logrotate.d/$SERVICE_PREFIX-job-logs"
  candidate_file="/etc/logrotate.d/.$SERVICE_PREFIX-job-logs.tmp"
  temp_file="$(mktemp)"
  {
    printf '%s/*.log {\n' "$JOB_LOG_DIR"
    printf '    daily\n'
    printf '    rotate %s\n' "$JOB_LOG_RETENTION_DAYS"
    printf '    maxsize %s\n' "$JOB_LOG_MAX_SIZE"
    printf '    compress\n'
    printf '    delaycompress\n'
    printf '    missingok\n'
    printf '    notifempty\n'
    printf '    copytruncate\n'
    printf '    su %s %s\n' "$RUN_USER" "$RUN_USER"
    printf '    create 0640 %s %s\n' "$RUN_USER" "$RUN_USER"
    printf '}\n'
  } > "$temp_file"

  "${SUDO[@]}" install -o root -g root -m 644 "$temp_file" "$candidate_file"
  rm -f "$temp_file"
  if ! "${SUDO[@]}" logrotate -d "$candidate_file" >/dev/null; then
    "${SUDO[@]}" rm -f "$candidate_file"
    fail "Generated logrotate configuration did not validate."
  fi
  "${SUDO[@]}" mv "$candidate_file" "$logrotate_file"
}

install_job_log_helper() {
  log "Installing job log settings helper"

  local helper_source
  helper_source="$PROJECT_DIR/scripts/job-log-helper.sh"
  [ -f "$helper_source" ] || fail "Missing helper source: $helper_source"

  "${SUDO[@]}" install -d -o root -g root -m 755 "$(dirname "$JOB_LOG_HELPER")"
  "${SUDO[@]}" install -o root -g root -m 755 "$helper_source" "$JOB_LOG_HELPER"

  local sudoers_file sudoers_temp
  sudoers_file="/etc/sudoers.d/$SERVICE_PREFIX-job-log"
  sudoers_temp="$(mktemp)"
  {
    printf '# Allow OCI Migrator to update its managed job logrotate settings only.\n'
    printf '%s ALL=(root) NOPASSWD: %s\n' "$RUN_USER" "$JOB_LOG_HELPER"
  } > "$sudoers_temp"

  "${SUDO[@]}" visudo -cf "$sudoers_temp" >/dev/null
  "${SUDO[@]}" install -o root -g root -m 440 "$sudoers_temp" "$sudoers_file"
  rm -f "$sudoers_temp"
}

install_local_share_helper() {
  log "Installing optional local SMB share helper"

  case "$LOCAL_SHARE_HELPER" in
    /*)
      ;;
    *)
      fail "--local-share-helper must be an absolute path."
      ;;
  esac

  local helper_source
  helper_source="$PROJECT_DIR/scripts/local-share-helper.sh"
  [ -f "$helper_source" ] || fail "Missing helper source: $helper_source"

  "${SUDO[@]}" install -d -o root -g root -m 755 "$(dirname "$LOCAL_SHARE_HELPER")"
  "${SUDO[@]}" install -o root -g root -m 755 "$helper_source" "$LOCAL_SHARE_HELPER"

  "${SUDO[@]}" install -d -o root -g root -m 755 "$(dirname "$LOCAL_SHARE_CONFIG")"
  local helper_config
  helper_config="$(mktemp)"
  {
    printf 'LOCAL_DATA_ROOT=%q\n' "$LOCAL_DATA_ROOT"
    printf 'RUN_USER=%q\n' "$RUN_USER"
  } > "$helper_config"
  "${SUDO[@]}" install -o root -g root -m 644 "$helper_config" "$LOCAL_SHARE_CONFIG"
  rm -f "$helper_config"

  local sudoers_file sudoers_temp
  sudoers_file="/etc/sudoers.d/$SERVICE_PREFIX-local-share"
  sudoers_temp="$(mktemp)"
  {
    printf '# Allow OCI Migrator to enable/disable managed local SMB shares only.\n'
    printf '%s ALL=(root) NOPASSWD: %s\n' "$RUN_USER" "$LOCAL_SHARE_HELPER"
  } > "$sudoers_temp"

  "${SUDO[@]}" visudo -cf "$sudoers_temp" >/dev/null
  "${SUDO[@]}" install -o root -g root -m 440 "$sudoers_temp" "$sudoers_file"
  rm -f "$sudoers_temp"
}

install_upgrade_helper() {
  log "Installing controlled upgrade helper"

  case "$UPGRADE_HELPER" in
    /*)
      ;;
    *)
      fail "--upgrade-helper must be an absolute path."
      ;;
  esac

  local helper_source
  helper_source="$PROJECT_DIR/scripts/upgrade-helper.sh"
  [ -f "$helper_source" ] || fail "Missing helper source: $helper_source"

  "${SUDO[@]}" install -d -o root -g root -m 755 "$(dirname "$UPGRADE_HELPER")"
  "${SUDO[@]}" install -o root -g root -m 755 "$helper_source" "$UPGRADE_HELPER"
  "${SUDO[@]}" install -d -o root -g root -m 755 "$(dirname "$UPGRADE_CONFIG")"
  "${SUDO[@]}" install -d -o "$RUN_USER" -g "$RUN_USER" -m 750 "$UPGRADE_STATE_DIR"
  "${SUDO[@]}" install -d -o "$RUN_USER" -g "$RUN_USER" -m 750 "$(dirname "$UPGRADE_LOG_FILE")"

  local repo_url branch
  repo_url="$(git -C "$PROJECT_DIR" config --get remote.origin.url 2>/dev/null || printf 'https://github.com/mr-ulusoy/OCI_migration_tool.git')"
  branch="$(git -C "$PROJECT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || printf 'main')"
  [ "$branch" != "HEAD" ] || branch="main"

  local helper_config
  helper_config="$(mktemp)"
  {
    printf 'INSTALL_DIR=%q\n' "$PROJECT_DIR"
    printf 'RUN_USER=%q\n' "$RUN_USER"
    printf 'REPO_URL=%q\n' "$repo_url"
    printf 'BRANCH=%q\n' "$branch"
    printf 'PUBLIC_HOST=%q\n' "${PUBLIC_HOST:-}"
    printf 'API_PORT=%q\n' "$API_PORT"
    printf 'SERVICE_PREFIX=%q\n' "$SERVICE_PREFIX"
    printf 'ENV_FILE=%q\n' "$ENV_FILE"
    printf 'LOCAL_DATA_ROOT=%q\n' "$LOCAL_DATA_ROOT"
    printf 'JOB_LOG_DIR=%q\n' "$JOB_LOG_DIR"
    printf 'JOB_LOG_MAX_SIZE=%q\n' "$JOB_LOG_MAX_SIZE"
    printf 'JOB_LOG_RETENTION_DAYS=%q\n' "$JOB_LOG_RETENTION_DAYS"
    printf 'JOB_LOG_HELPER=%q\n' "$JOB_LOG_HELPER"
    printf 'LOCAL_SHARE_HELPER=%q\n' "$LOCAL_SHARE_HELPER"
    printf 'UPGRADE_HELPER=%q\n' "$UPGRADE_HELPER"
    printf 'UPGRADE_STATE_DIR=%q\n' "$UPGRADE_STATE_DIR"
    printf 'UPGRADE_STATUS_FILE=%q\n' "$UPGRADE_STATUS_FILE"
    printf 'UPGRADE_LOG_FILE=%q\n' "$UPGRADE_LOG_FILE"
    printf 'CELERY_CONCURRENCY=%q\n' "$CELERY_CONCURRENCY"
    printf 'OPEN_FIREWALL=%q\n' "$OPEN_FIREWALL"
  } > "$helper_config"
  "${SUDO[@]}" install -o root -g root -m 644 "$helper_config" "$UPGRADE_CONFIG"
  rm -f "$helper_config"

  local sudoers_file sudoers_temp
  sudoers_file="/etc/sudoers.d/$SERVICE_PREFIX-upgrade"
  sudoers_temp="$(mktemp)"
  {
    printf '# Allow OCI Migrator to run its controlled self-upgrade only.\n'
    printf '%s ALL=(root) NOPASSWD: %s start\n' "$RUN_USER" "$UPGRADE_HELPER"
  } > "$sudoers_temp"

  "${SUDO[@]}" visudo -cf "$sudoers_temp" >/dev/null
  "${SUDO[@]}" install -o root -g root -m 440 "$sudoers_temp" "$sudoers_file"
  rm -f "$sudoers_temp"
}

install_backend() {
  log "Installing backend Python environment"
  local requirements_file="$BACKEND_DIR/requirements.txt"
  if [ -f "$BACKEND_DIR/requirements.lock" ]; then
    requirements_file="$BACKEND_DIR/requirements.lock"
  fi

  run_as_user python3 -m venv "$VENV_DIR"
  run_as_user "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
  run_as_user "$VENV_DIR/bin/python" -m pip install -r "$requirements_file"
}

write_frontend_env() {
  local temp_file
  temp_file="$(mktemp)"
  {
    if [ -n "${VITE_API_BASE:-}" ]; then
      printf 'VITE_API_BASE=%s\n' "$VITE_API_BASE"
    else
      printf '# VITE_API_BASE intentionally unset; production uses the same backend origin.\n'
    fi
    printf '# Keep VITE_API_TOKEN unset for normal installs. Admin login is used instead.\n'
  } > "$temp_file"

  "${SUDO[@]}" install -o "$RUN_USER" -g "$RUN_USER" -m 644 "$temp_file" "$FRONTEND_DIR/.env.production"
  rm -f "$temp_file"
}

install_frontend() {
  log "Installing frontend dependencies and building static assets"
  write_frontend_env

  if [ -f "$FRONTEND_DIR/package-lock.json" ]; then
    run_as_user_in_dir "$FRONTEND_DIR" npm ci
  else
    run_as_user_in_dir "$FRONTEND_DIR" npm install
  fi

  run_as_user_in_dir "$FRONTEND_DIR" npm run build
}

stop_services() {
  log "Stopping existing services if present"
  "${SUDO[@]}" systemctl stop "$SERVICE_PREFIX-scheduler.timer" "$SERVICE_PREFIX-scheduler.service" "$SERVICE_PREFIX-api.service" "$SERVICE_PREFIX-worker.service" "$SERVICE_PREFIX-frontend.service" 2>/dev/null || true
}

port_is_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    "${SUDO[@]}" ss -ltn | awk '{print $4}' | grep -Eq "[:.]$port$"
  else
    return 1
  fi
}

stop_legacy_processes() {
  [ "$STOP_LEGACY_PROCESSES" = "1" ] || return 0

  log "Stopping legacy uvicorn/vite processes for this project"
  pkill -f "$VENV_DIR/bin/uvicorn main:app" 2>/dev/null || true
  pkill -f "$VENV_DIR/bin/python.*uvicorn main:app" 2>/dev/null || true
  pkill -f "$FRONTEND_DIR.*vite" 2>/dev/null || true
}

check_ports() {
  stop_legacy_processes

  if port_is_in_use "$API_PORT"; then
    "${SUDO[@]}" ss -ltnp | grep ":$API_PORT" || true
    fail "Port $API_PORT is already in use. Stop that process, set API_PORT=another_port, or rerun with STOP_LEGACY_PROCESSES=1 if it is an old $APP_NAME process."
  fi
}

write_systemd_units() {
  log "Writing systemd services"

  "${SUDO[@]}" systemctl disable --now "$SERVICE_PREFIX-frontend.service" 2>/dev/null || true
  "${SUDO[@]}" rm -f "/etc/systemd/system/$SERVICE_PREFIX-frontend.service"

  "${SUDO[@]}" tee "/etc/systemd/system/$SERVICE_PREFIX-api.service" >/dev/null <<EOF
[Unit]
Description=OCI Migrator FastAPI backend
After=network-online.target redis-server.service
Wants=network-online.target redis-server.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$BACKEND_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
Environment=OCI_MIGRATOR_ENV_FILE=$ENV_FILE
Environment=OCI_MIGRATOR_FRONTEND_DIST_DIR=$FRONTEND_DIR/dist
ExecStart=$VENV_DIR/bin/python -m uvicorn main:app --host $API_HOST --port $API_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

  "${SUDO[@]}" tee "/etc/systemd/system/$SERVICE_PREFIX-worker.service" >/dev/null <<EOF
[Unit]
Description=OCI Migrator Celery worker
After=network-online.target redis-server.service
Wants=network-online.target redis-server.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$BACKEND_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
Environment=OCI_MIGRATOR_ENV_FILE=$ENV_FILE
ExecStart=$VENV_DIR/bin/python -m celery -A worker.celery_app worker --loglevel=info --concurrency=$CELERY_CONCURRENCY
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

  "${SUDO[@]}" tee "/etc/systemd/system/$SERVICE_PREFIX-scheduler.service" >/dev/null <<EOF
[Unit]
Description=OCI Migrator scheduled job runner
After=network-online.target redis-server.service
Wants=network-online.target redis-server.service

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$BACKEND_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
Environment=OCI_MIGRATOR_ENV_FILE=$ENV_FILE
ExecStart=$VENV_DIR/bin/python $BACKEND_DIR/run_backups.py
EOF

  "${SUDO[@]}" tee "/etc/systemd/system/$SERVICE_PREFIX-scheduler.timer" >/dev/null <<EOF
[Unit]
Description=Run OCI Migrator scheduler every minute

[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
AccuracySec=1s
Unit=$SERVICE_PREFIX-scheduler.service

[Install]
WantedBy=timers.target
EOF
}

start_services() {
  log "Starting services"
  "${SUDO[@]}" systemctl daemon-reload
  "${SUDO[@]}" systemctl enable --now redis-server
  "${SUDO[@]}" systemctl enable --now "$SERVICE_PREFIX-api.service"
  "${SUDO[@]}" systemctl enable --now "$SERVICE_PREFIX-worker.service"
  "${SUDO[@]}" systemctl enable --now "$SERVICE_PREFIX-scheduler.timer"
}

open_firewall_ports() {
  [ "$OPEN_FIREWALL" = "1" ] || return 0

  log "Opening local firewall ports"
  if command -v ufw >/dev/null 2>&1 && "${SUDO[@]}" ufw status | grep -q 'Status: active'; then
    "${SUDO[@]}" ufw allow "$API_PORT/tcp"
    return
  fi

  if command -v iptables >/dev/null 2>&1; then
    "${SUDO[@]}" iptables -C INPUT -p tcp --dport "$API_PORT" -j ACCEPT 2>/dev/null || "${SUDO[@]}" iptables -I INPUT 1 -p tcp --dport "$API_PORT" -j ACCEPT
    command -v netfilter-persistent >/dev/null 2>&1 && "${SUDO[@]}" netfilter-persistent save || true
  fi
}

print_summary() {
  printf '\n'
  printf 'Installation complete.\n'
  printf 'App:      http://%s:%s\n' "${PUBLIC_HOST:-localhost}" "$API_PORT"
  printf 'API:      http://%s:%s\n' "${PUBLIC_HOST:-localhost}" "$API_PORT"
  printf 'Env file: %s\n' "$ENV_FILE"
  printf 'Job logs: %s (logrotate maxsize %s, retention %s days)\n' "$JOB_LOG_DIR" "$JOB_LOG_MAX_SIZE" "$JOB_LOG_RETENTION_DAYS"
  printf 'Admin username: %s\n' "$ADMIN_USERNAME"
  if [ -n "$GENERATED_ADMIN_PASSWORD" ]; then
    printf 'Generated admin password: %s\n' "$GENERATED_ADMIN_PASSWORD"
    printf 'Store this password now. It will not be shown again.\n'
  elif [ "$ADMIN_PASSWORD_UPDATED" = "1" ]; then
    printf 'Admin password: updated\n'
  else
    printf 'Admin password: already configured\n'
  fi
  if [ "$PRINT_TOKEN" = "1" ] && [ -f "$ENV_FILE" ]; then
    printf 'API token: %s\n' "$(grep '^OCI_MIGRATOR_API_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2- || printf '<not found>')"
  else
    printf 'API token: stored in %s (use --print-token only on trusted terminals)\n' "$ENV_FILE"
  fi
  printf '\n'
  printf 'Useful commands:\n'
  printf '  sudo systemctl status %s-api %s-worker %s-scheduler.timer\n' "$SERVICE_PREFIX" "$SERVICE_PREFIX" "$SERVICE_PREFIX"
  printf '  journalctl -u %s-api -f\n' "$SERVICE_PREFIX"
}

main() {
  parse_args "$@"
  initialize_runtime_paths
  ensure_supported_os
  install_system_dependencies
  load_admin_password_input
  install_node
  install_rclone
  validate_job_log_settings
  ensure_env_file
  load_job_log_settings_from_env
  validate_job_log_settings
  ensure_local_data_root
  ensure_job_log_dir
  install_job_logrotate_config
  install_job_log_helper
  install_local_share_helper
  install_upgrade_helper
  install_backend
  install_frontend
  stop_services
  check_ports
  write_systemd_units
  start_services
  open_firewall_ports
  print_summary
}

main "$@"
