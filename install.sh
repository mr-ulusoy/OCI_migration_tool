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
TIME_SYNC_HELPER="${OCI_MIGRATOR_TIME_SYNC_HELPER:-/usr/local/sbin/oci-migrator-time-sync}"
NETWORK_HELPER="${OCI_MIGRATOR_NETWORK_HELPER:-/usr/local/sbin/oci-migrator-network}"
TLS_HELPER="${OCI_MIGRATOR_TLS_HELPER:-/usr/local/sbin/oci-migrator-tls}"
TLS_CONFIG="${OCI_MIGRATOR_TLS_CONFIG:-/etc/oci-migrator/tls.conf}"
TLS_STATE_DIR="${OCI_MIGRATOR_TLS_STATE_DIR:-/var/lib/oci-migrator/tls}"
TLS_CADDYFILE="${OCI_MIGRATOR_TLS_CADDYFILE:-/etc/oci-migrator/Caddyfile}"
TLS_SERVICE="${OCI_MIGRATOR_TLS_SERVICE:-}"
LOCAL_SHARE_CONFIG="/etc/oci-migrator/local-share.conf"
UPGRADE_HELPER="${OCI_MIGRATOR_UPGRADE_HELPER:-/usr/local/sbin/oci-migrator-upgrade}"
UPGRADE_CONFIG="${OCI_MIGRATOR_UPGRADE_CONFIG:-/etc/oci-migrator/upgrade.conf}"
UNINSTALL_HELPER="${OCI_MIGRATOR_UNINSTALL_HELPER:-/usr/local/sbin/oci-migrator-uninstall}"
UNINSTALL_CONFIG="${OCI_MIGRATOR_UNINSTALL_CONFIG:-/etc/oci-migrator/uninstall.conf}"
UPGRADE_STATE_DIR="${OCI_MIGRATOR_UPGRADE_STATE_DIR:-/var/lib/oci-migrator/upgrade}"
UPGRADE_STATUS_FILE="${OCI_MIGRATOR_UPGRADE_STATUS_FILE:-$UPGRADE_STATE_DIR/status.json}"
UPGRADE_REQUEST_FILE="${OCI_MIGRATOR_UPGRADE_REQUEST_FILE:-$UPGRADE_STATE_DIR/request.json}"
UPGRADE_LOG_FILE="${OCI_MIGRATOR_UPGRADE_LOG_FILE:-/var/log/oci-migrator/upgrade.log}"
SERVER_TIMEZONE="${OCI_MIGRATOR_TIMEZONE:-Europe/Stockholm}"
NTP_SERVERS="${OCI_MIGRATOR_NTP_SERVERS:-0.se.pool.ntp.org 1.se.pool.ntp.org 2.se.pool.ntp.org 3.se.pool.ntp.org}"
JOB_LOG_DIR_PROVIDED=0
JOB_LOG_MAX_SIZE_PROVIDED=0
JOB_LOG_RETENTION_DAYS_PROVIDED=0
TIMEZONE_PROVIDED=0
NTP_SERVERS_PROVIDED=0
if [ -n "${OCI_MIGRATOR_JOB_LOG_DIR:-}" ]; then
  JOB_LOG_DIR_PROVIDED=1
fi
if [ -n "${OCI_MIGRATOR_JOB_LOG_MAX_SIZE:-}" ]; then
  JOB_LOG_MAX_SIZE_PROVIDED=1
fi
if [ -n "${OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS:-}" ]; then
  JOB_LOG_RETENTION_DAYS_PROVIDED=1
fi
if [ -n "${OCI_MIGRATOR_TIMEZONE:-}" ]; then
  TIMEZONE_PROVIDED=1
fi
if [ -n "${OCI_MIGRATOR_NTP_SERVERS:-}" ]; then
  NTP_SERVERS_PROVIDED=1
fi
CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-2}"
OPEN_FIREWALL="${OPEN_FIREWALL:-1}"
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
ADMIN_PASSWORD_OUTPUT_FILE=""
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
Cloud Migration Console installer

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
  --local-share-helper PATH   Root helper used by the UI for optional SMB/NFS shares. Default: $LOCAL_SHARE_HELPER
  --time-sync-helper PATH     Root helper used by the UI for timezone/NTP. Default: $TIME_SYNC_HELPER
  --network-helper PATH       Root helper used by the UI for DHCP/static IPv4. Default: $NETWORK_HELPER
  --tls-helper PATH           Root helper used by the UI for HTTPS configuration. Default: $TLS_HELPER
  --upgrade-helper PATH       Root helper used by the UI for controlled upgrades. Default: $UPGRADE_HELPER
  --uninstall-helper PATH     Root helper used by the UI for controlled uninstall. Default: $UNINSTALL_HELPER
  --timezone ZONE             Server timezone for schedules/logs. Default: $SERVER_TIMEZONE
  --ntp-servers "LIST"        Space/comma separated NTP servers. Default: $NTP_SERVERS
  --celery-concurrency N      Celery worker concurrency. Default: $CELERY_CONCURRENCY
  --open-firewall             Open local firewall ports with ufw/iptables. Default: enabled.
  --no-open-firewall          Do not open local firewall ports during install.
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
  OCI_MIGRATOR_LOCAL_SHARE_HELPER, OCI_MIGRATOR_TIME_SYNC_HELPER,
  OCI_MIGRATOR_NETWORK_HELPER,
  OCI_MIGRATOR_TLS_HELPER,
  OCI_MIGRATOR_UPGRADE_HELPER, OCI_MIGRATOR_UNINSTALL_HELPER,
  OCI_MIGRATOR_TIMEZONE, OCI_MIGRATOR_NTP_SERVERS,
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
      --time-sync-helper)
        TIME_SYNC_HELPER="$2"
        shift 2
        ;;
      --network-helper)
        NETWORK_HELPER="$2"
        shift 2
        ;;
      --tls-helper)
        TLS_HELPER="$2"
        shift 2
        ;;
      --upgrade-helper)
        UPGRADE_HELPER="$2"
        shift 2
        ;;
      --uninstall-helper)
        UNINSTALL_HELPER="$2"
        shift 2
        ;;
      --timezone)
        SERVER_TIMEZONE="$2"
        TIMEZONE_PROVIDED=1
        shift 2
        ;;
      --ntp-servers)
        NTP_SERVERS="$2"
        NTP_SERVERS_PROVIDED=1
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
      --no-open-firewall)
        OPEN_FIREWALL=0
        shift
        ;;
      --share-allow-cidr)
        # Deprecated compatibility for the short-lived installer option. The value is intentionally ignored.
        shift 2
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
  TLS_SERVICE="${TLS_SERVICE:-$SERVICE_PREFIX-tls.service}"
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
  ADMIN_PASSWORD_OUTPUT_FILE="$USER_HOME/oci-migrator-admin-password.txt"
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

  case "$TIME_SYNC_HELPER" in
    /*)
      ;;
    *)
      fail "--time-sync-helper must be an absolute path."
      ;;
  esac

  case "$NETWORK_HELPER" in
    /*)
      ;;
    *)
      fail "--network-helper must be an absolute path."
      ;;
  esac

  case "$TLS_HELPER" in
    /*)
      ;;
    *)
      fail "--tls-helper must be an absolute path."
      ;;
  esac
}

normalize_ntp_servers() {
  NTP_SERVERS="$(printf '%s' "$NTP_SERVERS" | tr ',' ' ' | awk '{$1=$1; print}')"
}

validate_time_settings() {
  if ! [[ "$SERVER_TIMEZONE" =~ ^[A-Za-z0-9_+.-]+(/[A-Za-z0-9_+.-]+)*$ ]]; then
    fail "--timezone must be a valid IANA timezone, for example Europe/Stockholm."
  fi

  if [ ! -f "/usr/share/zoneinfo/$SERVER_TIMEZONE" ]; then
    fail "Timezone data not found for $SERVER_TIMEZONE."
  fi

  normalize_ntp_servers
  [ -n "$NTP_SERVERS" ] || fail "--ntp-servers must contain at least one server."

  local server
  for server in $NTP_SERVERS; do
    if ! [[ "$server" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]*$ ]]; then
      fail "Invalid NTP server: $server"
    fi
  done
}

run_as_user() {
  if [ "$(id -un)" = "$RUN_USER" ]; then
    "$@"
  else
    sudo -H -u "$RUN_USER" "$@"
  fi
}

run_as_user_in_dir() {
  local dir="$1"
  shift

  if [ "$(id -un)" = "$RUN_USER" ]; then
    (cd "$dir" && "$@")
  else
    sudo -H -u "$RUN_USER" bash -c 'cd "$1" && shift && "$@"' bash "$dir" "$@"
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

persist_generated_admin_password() {
  if [ -n "$GENERATED_ADMIN_PASSWORD" ]; then
    local temp_file
    temp_file="$(mktemp)"
    printf '%s\n' "$GENERATED_ADMIN_PASSWORD" > "$temp_file"
    if ! "${SUDO[@]}" install -o "$RUN_USER" -g "$RUN_USER" -m 600 "$temp_file" "$ADMIN_PASSWORD_OUTPUT_FILE"; then
      rm -f "$temp_file"
      fail "Unable to store the generated admin password in $ADMIN_PASSWORD_OUTPUT_FILE"
    fi
    rm -f "$temp_file"
  elif [ "$ADMIN_PASSWORD_UPDATED" = "1" ]; then
    if [ -n "$ADMIN_PASSWORD_FILE" ] \
      && [ "$(readlink -f "$ADMIN_PASSWORD_FILE")" = "$(readlink -f "$ADMIN_PASSWORD_OUTPUT_FILE")" ]; then
      "${SUDO[@]}" chown "$RUN_USER:$RUN_USER" "$ADMIN_PASSWORD_OUTPUT_FILE"
      "${SUDO[@]}" chmod 600 "$ADMIN_PASSWORD_OUTPUT_FILE"
    else
      "${SUDO[@]}" rm -f "$ADMIN_PASSWORD_OUTPUT_FILE"
    fi
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
  if command -v debconf-set-selections >/dev/null 2>&1; then
    printf '%s\n' \
      'iptables-persistent iptables-persistent/autosave_v4 boolean true' \
      'iptables-persistent iptables-persistent/autosave_v6 boolean true' \
      | "${SUDO[@]}" debconf-set-selections
  fi
  "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates \
    cifs-utils \
    curl \
    apt-transport-https \
    debian-archive-keyring \
    debian-keyring \
    gnupg \
    iproute2 \
    iptables \
    iptables-persistent \
    logrotate \
    netplan.io \
    nfs-common \
    openssl \
    python3 \
    python3-pip \
    python3-venv \
    redis-server \
    sudo \
    systemd-timesyncd \
    tzdata \
    unzip
}

install_caddy_support() {
  if command -v caddy >/dev/null 2>&1; then
    log "Caddy $(caddy version 2>/dev/null || printf 'is installed')"
    return
  fi

  log "Installing Caddy for managed HTTPS"
  if ! "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y caddy; then
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key -o /tmp/caddy-stable.gpg.key
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o /tmp/caddy-stable.list
    "${SUDO[@]}" gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg /tmp/caddy-stable.gpg.key
    "${SUDO[@]}" install -o root -g root -m 644 /tmp/caddy-stable.list /etc/apt/sources.list.d/caddy-stable.list
    "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get update
    "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y caddy
  fi

  # Cloud Migration Console uses its own isolated Caddy unit and never edits /etc/caddy/Caddyfile.
  "${SUDO[@]}" systemctl disable --now caddy.service caddy-api.service >/dev/null 2>&1 || true
}

configure_time_sync() {
  log "Configuring server time sync"

  local timesyncd_dir="/etc/systemd/timesyncd.conf.d"
  local timesyncd_conf="$timesyncd_dir/oci-migrator.conf"
  local temp_file
  temp_file="$(mktemp)"
  {
    printf '[Time]\n'
    printf 'NTP=%s\n' "$NTP_SERVERS"
    printf 'FallbackNTP=0.pool.ntp.org 1.pool.ntp.org 2.pool.ntp.org 3.pool.ntp.org\n'
  } > "$temp_file"

  "${SUDO[@]}" install -d -o root -g root -m 755 "$timesyncd_dir"
  "${SUDO[@]}" install -o root -g root -m 644 "$temp_file" "$timesyncd_conf"
  rm -f "$temp_file"

  if command -v timedatectl >/dev/null 2>&1; then
    "${SUDO[@]}" timedatectl set-timezone "$SERVER_TIMEZONE" || log "Warning: unable to set timezone with timedatectl."
    "${SUDO[@]}" timedatectl set-ntp true || log "Warning: unable to enable NTP with timedatectl."
  fi

  "${SUDO[@]}" systemctl enable --now systemd-timesyncd.service >/dev/null 2>&1 || log "Warning: unable to enable systemd-timesyncd."
  "${SUDO[@]}" systemctl restart systemd-timesyncd.service >/dev/null 2>&1 || log "Warning: unable to restart systemd-timesyncd."
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
      printf 'OCI_MIGRATOR_SERVICE_PREFIX=%s\n' "$SERVICE_PREFIX"
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
      printf 'OCI_MIGRATOR_TIME_SYNC_HELPER=%s\n' "$TIME_SYNC_HELPER"
      printf 'OCI_MIGRATOR_NETWORK_HELPER=%s\n' "$NETWORK_HELPER"
      printf 'OCI_MIGRATOR_TLS_HELPER=%s\n' "$TLS_HELPER"
      printf 'OCI_MIGRATOR_TLS_MODE=http\n'
      printf 'OCI_MIGRATOR_TLS_HOSTNAME=\n'
      printf 'OCI_MIGRATOR_TLS_EMAIL=\n'
      printf 'OCI_MIGRATOR_TLS_CERT_SOURCE=\n'
      printf 'OCI_MIGRATOR_TLS_HTTP_ACKNOWLEDGED=false\n'
      printf 'OCI_MIGRATOR_UPGRADE_HELPER=%s\n' "$UPGRADE_HELPER"
      printf 'OCI_MIGRATOR_UNINSTALL_HELPER=%s\n' "$UNINSTALL_HELPER"
      printf 'OCI_MIGRATOR_UPGRADE_STATUS_FILE=%s\n' "$UPGRADE_STATUS_FILE"
      printf 'OCI_MIGRATOR_UPGRADE_REQUEST_FILE=%s\n' "$UPGRADE_REQUEST_FILE"
      printf 'OCI_MIGRATOR_UPGRADE_LOG_FILE=%s\n' "$UPGRADE_LOG_FILE"
      printf 'OCI_MIGRATOR_TIMEZONE=%s\n' "$SERVER_TIMEZONE"
      printf 'OCI_MIGRATOR_NTP_SERVERS=%s\n' "${NTP_SERVERS// /,}"
      printf 'OCI_MIGRATOR_SYSLOG_ENABLED=false\n'
      printf 'OCI_MIGRATOR_SYSLOG_HOST=\n'
      printf 'OCI_MIGRATOR_SYSLOG_PORT=514\n'
      printf 'OCI_MIGRATOR_SYSLOG_PROTOCOL=udp\n'
      printf 'OCI_MIGRATOR_SYSLOG_FACILITY=local0\n'
      printf 'OCI_MIGRATOR_SYSLOG_EVENTS=failures_recovery\n'
    } > "$temp_file"

    "${SUDO[@]}" install -o "$RUN_USER" -g "$RUN_USER" -m 600 "$temp_file" "$ENV_FILE"
    rm -f "$temp_file"
  else
    log "Keeping existing $ENV_FILE"
    "${SUDO[@]}" chmod 600 "$ENV_FILE" || true
    "${SUDO[@]}" chown "$RUN_USER:$RUN_USER" "$ENV_FILE" || true

    grep -q '^OCI_MIGRATOR_ALLOWED_ORIGINS=' "$ENV_FILE" || printf 'OCI_MIGRATOR_ALLOWED_ORIGINS=%s\n' "$allowed_origins" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_SERVICE_PREFIX=' "$ENV_FILE" || printf 'OCI_MIGRATOR_SERVICE_PREFIX=%s\n' "$SERVICE_PREFIX" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
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
    grep -q '^OCI_MIGRATOR_TIME_SYNC_HELPER=' "$ENV_FILE" || printf 'OCI_MIGRATOR_TIME_SYNC_HELPER=%s\n' "$TIME_SYNC_HELPER" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_NETWORK_HELPER=' "$ENV_FILE" || printf 'OCI_MIGRATOR_NETWORK_HELPER=%s\n' "$NETWORK_HELPER" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_TLS_HELPER=' "$ENV_FILE" || printf 'OCI_MIGRATOR_TLS_HELPER=%s\n' "$TLS_HELPER" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_TLS_MODE=' "$ENV_FILE" || printf 'OCI_MIGRATOR_TLS_MODE=http\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_TLS_HOSTNAME=' "$ENV_FILE" || printf 'OCI_MIGRATOR_TLS_HOSTNAME=\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_TLS_EMAIL=' "$ENV_FILE" || printf 'OCI_MIGRATOR_TLS_EMAIL=\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_TLS_CERT_SOURCE=' "$ENV_FILE" || printf 'OCI_MIGRATOR_TLS_CERT_SOURCE=\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_TLS_HTTP_ACKNOWLEDGED=' "$ENV_FILE" || printf 'OCI_MIGRATOR_TLS_HTTP_ACKNOWLEDGED=false\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_UPGRADE_HELPER=' "$ENV_FILE" || printf 'OCI_MIGRATOR_UPGRADE_HELPER=%s\n' "$UPGRADE_HELPER" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_UNINSTALL_HELPER=' "$ENV_FILE" || printf 'OCI_MIGRATOR_UNINSTALL_HELPER=%s\n' "$UNINSTALL_HELPER" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_UPGRADE_STATUS_FILE=' "$ENV_FILE" || printf 'OCI_MIGRATOR_UPGRADE_STATUS_FILE=%s\n' "$UPGRADE_STATUS_FILE" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_UPGRADE_REQUEST_FILE=' "$ENV_FILE" || printf 'OCI_MIGRATOR_UPGRADE_REQUEST_FILE=%s\n' "$UPGRADE_REQUEST_FILE" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_UPGRADE_LOG_FILE=' "$ENV_FILE" || printf 'OCI_MIGRATOR_UPGRADE_LOG_FILE=%s\n' "$UPGRADE_LOG_FILE" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_TIMEZONE=' "$ENV_FILE" || printf 'OCI_MIGRATOR_TIMEZONE=%s\n' "$SERVER_TIMEZONE" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_NTP_SERVERS=' "$ENV_FILE" || printf 'OCI_MIGRATOR_NTP_SERVERS=%s\n' "${NTP_SERVERS// /,}" | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_SYSLOG_ENABLED=' "$ENV_FILE" || printf 'OCI_MIGRATOR_SYSLOG_ENABLED=false\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_SYSLOG_HOST=' "$ENV_FILE" || printf 'OCI_MIGRATOR_SYSLOG_HOST=\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_SYSLOG_PORT=' "$ENV_FILE" || printf 'OCI_MIGRATOR_SYSLOG_PORT=514\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_SYSLOG_PROTOCOL=' "$ENV_FILE" || printf 'OCI_MIGRATOR_SYSLOG_PROTOCOL=udp\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_SYSLOG_FACILITY=' "$ENV_FILE" || printf 'OCI_MIGRATOR_SYSLOG_FACILITY=local0\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null
    grep -q '^OCI_MIGRATOR_SYSLOG_EVENTS=' "$ENV_FILE" || printf 'OCI_MIGRATOR_SYSLOG_EVENTS=failures_recovery\n' | "${SUDO[@]}" tee -a "$ENV_FILE" >/dev/null

    if [ "$ADMIN_USERNAME_PROVIDED" = "1" ] || ! grep -q '^OCI_MIGRATOR_ADMIN_USERNAME=' "$ENV_FILE"; then
      set_env_value "OCI_MIGRATOR_ADMIN_USERNAME" "$ADMIN_USERNAME"
    fi
    if [ "$TIMEZONE_PROVIDED" = "1" ]; then
      set_env_value "OCI_MIGRATOR_TIMEZONE" "$SERVER_TIMEZONE"
    fi
    if [ "$NTP_SERVERS_PROVIDED" = "1" ]; then
      set_env_value "OCI_MIGRATOR_NTP_SERVERS" "${NTP_SERVERS// /,}"
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

load_time_settings_from_env() {
  [ -f "$ENV_FILE" ] || return 0

  local configured_timezone configured_ntp_servers
  configured_timezone="$(grep '^OCI_MIGRATOR_TIMEZONE=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
  configured_ntp_servers="$(grep '^OCI_MIGRATOR_NTP_SERVERS=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"

  if [ "$TIMEZONE_PROVIDED" = "0" ] && [ -n "$configured_timezone" ]; then
    SERVER_TIMEZONE="$configured_timezone"
  fi
  if [ "$NTP_SERVERS_PROVIDED" = "0" ] && [ -n "$configured_ntp_servers" ]; then
    NTP_SERVERS="$configured_ntp_servers"
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

load_time_sync_helper_from_env() {
  [ -f "$ENV_FILE" ] || return 0

  local configured_time_sync_helper
  configured_time_sync_helper="$(grep '^OCI_MIGRATOR_TIME_SYNC_HELPER=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
  if [ -n "$configured_time_sync_helper" ]; then
    TIME_SYNC_HELPER="$configured_time_sync_helper"
  fi
}

load_network_helper_from_env() {
  [ -f "$ENV_FILE" ] || return 0

  local configured_network_helper
  configured_network_helper="$(grep '^OCI_MIGRATOR_NETWORK_HELPER=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
  if [ -n "$configured_network_helper" ]; then
    NETWORK_HELPER="$configured_network_helper"
  fi
}

load_tls_helper_from_env() {
  [ -f "$ENV_FILE" ] || return 0

  local configured_tls_helper
  configured_tls_helper="$(grep '^OCI_MIGRATOR_TLS_HELPER=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
  if [ -n "$configured_tls_helper" ]; then
    TLS_HELPER="$configured_tls_helper"
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
    printf '# Allow Cloud Migration Console to update its managed job logrotate settings only.\n'
    printf '%s ALL=(root) NOPASSWD: %s\n' "$RUN_USER" "$JOB_LOG_HELPER"
  } > "$sudoers_temp"

  "${SUDO[@]}" visudo -cf "$sudoers_temp" >/dev/null
  "${SUDO[@]}" install -o root -g root -m 440 "$sudoers_temp" "$sudoers_file"
  rm -f "$sudoers_temp"
}

install_time_sync_helper() {
  log "Installing time sync settings helper"

  case "$TIME_SYNC_HELPER" in
    /*)
      ;;
    *)
      fail "--time-sync-helper must be an absolute path."
      ;;
  esac

  local helper_source
  helper_source="$PROJECT_DIR/scripts/time-sync-helper.sh"
  [ -f "$helper_source" ] || fail "Missing helper source: $helper_source"

  "${SUDO[@]}" install -d -o root -g root -m 755 "$(dirname "$TIME_SYNC_HELPER")"
  "${SUDO[@]}" install -o root -g root -m 755 "$helper_source" "$TIME_SYNC_HELPER"

  local sudoers_file sudoers_temp
  sudoers_file="/etc/sudoers.d/$SERVICE_PREFIX-time-sync"
  sudoers_temp="$(mktemp)"
  {
    printf '# Allow Cloud Migration Console to update its managed timezone/NTP settings only.\n'
    printf '%s ALL=(root) NOPASSWD: %s\n' "$RUN_USER" "$TIME_SYNC_HELPER"
  } > "$sudoers_temp"

  "${SUDO[@]}" visudo -cf "$sudoers_temp" >/dev/null
  "${SUDO[@]}" install -o root -g root -m 440 "$sudoers_temp" "$sudoers_file"
  rm -f "$sudoers_temp"
}

install_network_helper() {
  log "Installing network settings helper"

  local helper_source
  helper_source="$PROJECT_DIR/scripts/network-helper.sh"
  [ -f "$helper_source" ] || fail "Missing helper source: $helper_source"

  "${SUDO[@]}" install -d -o root -g root -m 755 "$(dirname "$NETWORK_HELPER")"
  "${SUDO[@]}" install -o root -g root -m 755 "$helper_source" "$NETWORK_HELPER"
  "${SUDO[@]}" install -d -o root -g root -m 700 /var/lib/oci-migrator/network

  local service_file timer_file service_temp timer_temp
  service_file="/etc/systemd/system/oci-migrator-network-rollback.service"
  timer_file="/etc/systemd/system/oci-migrator-network-rollback.timer"
  service_temp="$(mktemp)"
  timer_temp="$(mktemp)"
  {
    printf '[Unit]\n'
    printf 'Description=Rollback unconfirmed Cloud Migration Console network configuration\n'
    printf 'After=network.target\n\n'
    printf '[Service]\n'
    printf 'Type=oneshot\n'
    printf 'ExecStart=%s rollback\n' "$NETWORK_HELPER"
  } > "$service_temp"
  {
    printf '[Unit]\n'
    printf 'Description=Rollback timer for Cloud Migration Console network configuration\n\n'
    printf '[Timer]\n'
    printf 'OnActiveSec=3min\n'
    printf 'AccuracySec=1s\n'
    printf 'Unit=oci-migrator-network-rollback.service\n\n'
    printf '[Install]\n'
    printf 'WantedBy=timers.target\n'
  } > "$timer_temp"
  "${SUDO[@]}" install -o root -g root -m 644 "$service_temp" "$service_file"
  "${SUDO[@]}" install -o root -g root -m 644 "$timer_temp" "$timer_file"
  rm -f "$service_temp" "$timer_temp"
  "${SUDO[@]}" systemctl daemon-reload

  local sudoers_file sudoers_temp
  sudoers_file="/etc/sudoers.d/$SERVICE_PREFIX-network"
  sudoers_temp="$(mktemp)"
  {
    printf '# Allow Cloud Migration Console to manage its validated Netplan configuration only.\n'
    printf '%s ALL=(root) NOPASSWD: %s\n' "$RUN_USER" "$NETWORK_HELPER"
  } > "$sudoers_temp"

  "${SUDO[@]}" visudo -cf "$sudoers_temp" >/dev/null
  "${SUDO[@]}" install -o root -g root -m 440 "$sudoers_temp" "$sudoers_file"
  rm -f "$sudoers_temp"
}

install_tls_helper() {
  log "Installing managed HTTPS helper"

  local helper_source
  helper_source="$PROJECT_DIR/scripts/tls-helper.sh"
  [ -f "$helper_source" ] || fail "Missing helper source: $helper_source"

  "${SUDO[@]}" install -d -o root -g root -m 755 "$(dirname "$TLS_HELPER")" "$(dirname "$TLS_CONFIG")"
  "${SUDO[@]}" install -o root -g root -m 755 "$helper_source" "$TLS_HELPER"
  "${SUDO[@]}" install -d -o root -g root -m 700 "$TLS_STATE_DIR"
  "${SUDO[@]}" install -d -o caddy -g caddy -m 750 "$TLS_STATE_DIR/data" "$TLS_STATE_DIR/config" "$TLS_STATE_DIR/logs"

  local helper_config
  helper_config="$(mktemp)"
  {
    printf 'ENV_FILE=%q\n' "$ENV_FILE"
    printf 'API_PORT=%q\n' "$API_PORT"
    printf 'SERVICE_PREFIX=%q\n' "$SERVICE_PREFIX"
    printf 'CADDYFILE=%q\n' "$TLS_CADDYFILE"
    printf 'TLS_STATE_DIR=%q\n' "$TLS_STATE_DIR"
    printf 'TLS_SERVICE=%q\n' "$TLS_SERVICE"
  } > "$helper_config"
  "${SUDO[@]}" install -o root -g root -m 600 "$helper_config" "$TLS_CONFIG"
  rm -f "$helper_config"

  "${SUDO[@]}" tee "/etc/systemd/system/$TLS_SERVICE" >/dev/null <<EOF
[Unit]
Description=Cloud Migration Console managed HTTPS endpoint
After=network-online.target $SERVICE_PREFIX-api.service
Wants=network-online.target $SERVICE_PREFIX-api.service

[Service]
Type=notify
User=caddy
Group=caddy
Environment=XDG_DATA_HOME=$TLS_STATE_DIR/data
Environment=XDG_CONFIG_HOME=$TLS_STATE_DIR/config
ExecStart=/usr/bin/caddy run --environ --config $TLS_CADDYFILE --adapter caddyfile
ExecReload=/usr/bin/caddy reload --config $TLS_CADDYFILE --adapter caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$TLS_STATE_DIR
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

  local sudoers_file sudoers_temp
  sudoers_file="/etc/sudoers.d/$SERVICE_PREFIX-tls"
  sudoers_temp="$(mktemp)"
  {
    printf '# Allow Cloud Migration Console to manage only its validated HTTPS configuration.\n'
    printf '%s ALL=(root) NOPASSWD: %s\n' "$RUN_USER" "$TLS_HELPER"
  } > "$sudoers_temp"
  "${SUDO[@]}" visudo -cf "$sudoers_temp" >/dev/null
  "${SUDO[@]}" install -o root -g root -m 440 "$sudoers_temp" "$sudoers_file"
  rm -f "$sudoers_temp"
  "${SUDO[@]}" systemctl daemon-reload
}

install_local_share_helper() {
  log "Installing optional local SMB/NFS share helper"

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
    printf '# Allow Cloud Migration Console to enable/disable managed local SMB/NFS shares only.\n'
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
    printf 'TIME_SYNC_HELPER=%q\n' "$TIME_SYNC_HELPER"
    printf 'NETWORK_HELPER=%q\n' "$NETWORK_HELPER"
    printf 'TLS_HELPER=%q\n' "$TLS_HELPER"
    printf 'UPGRADE_HELPER=%q\n' "$UPGRADE_HELPER"
    printf 'UNINSTALL_HELPER=%q\n' "$UNINSTALL_HELPER"
    printf 'UPGRADE_STATE_DIR=%q\n' "$UPGRADE_STATE_DIR"
    printf 'UPGRADE_STATUS_FILE=%q\n' "$UPGRADE_STATUS_FILE"
    printf 'UPGRADE_REQUEST_FILE=%q\n' "$UPGRADE_REQUEST_FILE"
    printf 'UPGRADE_LOG_FILE=%q\n' "$UPGRADE_LOG_FILE"
    printf 'SERVER_TIMEZONE=%q\n' "$SERVER_TIMEZONE"
    printf 'NTP_SERVERS=%q\n' "$NTP_SERVERS"
    printf 'CELERY_CONCURRENCY=%q\n' "$CELERY_CONCURRENCY"
    printf 'OPEN_FIREWALL=%q\n' "$OPEN_FIREWALL"
  } > "$helper_config"
  "${SUDO[@]}" install -o root -g root -m 644 "$helper_config" "$UPGRADE_CONFIG"
  rm -f "$helper_config"

  local sudoers_file sudoers_temp
  sudoers_file="/etc/sudoers.d/$SERVICE_PREFIX-upgrade"
  sudoers_temp="$(mktemp)"
  {
    printf '# Allow Cloud Migration Console to run its controlled self-upgrade only.\n'
    printf '%s ALL=(root) NOPASSWD: %s start\n' "$RUN_USER" "$UPGRADE_HELPER"
  } > "$sudoers_temp"

  "${SUDO[@]}" visudo -cf "$sudoers_temp" >/dev/null
  "${SUDO[@]}" install -o root -g root -m 440 "$sudoers_temp" "$sudoers_file"
  rm -f "$sudoers_temp"
}

install_uninstall_helper() {
  log "Installing controlled uninstall helper"

  case "$UNINSTALL_HELPER" in
    /*)
      ;;
    *)
      fail "--uninstall-helper must be an absolute path."
      ;;
  esac

  local helper_source
  helper_source="$PROJECT_DIR/scripts/uninstall-helper.sh"
  [ -f "$helper_source" ] || fail "Missing helper source: $helper_source"

  "${SUDO[@]}" install -d -o root -g root -m 755 "$(dirname "$UNINSTALL_HELPER")"
  "${SUDO[@]}" install -o root -g root -m 755 "$helper_source" "$UNINSTALL_HELPER"
  "${SUDO[@]}" install -d -o root -g root -m 755 "$(dirname "$UNINSTALL_CONFIG")"

  local helper_config
  helper_config="$(mktemp)"
  {
    printf 'PROJECT_DIR=%q\n' "$PROJECT_DIR"
    printf 'SERVICE_PREFIX=%q\n' "$SERVICE_PREFIX"
    printf 'ENV_FILE=%q\n' "$ENV_FILE"
    printf 'LOCAL_DATA_ROOT=%q\n' "$LOCAL_DATA_ROOT"
  } > "$helper_config"
  "${SUDO[@]}" install -o root -g root -m 600 "$helper_config" "$UNINSTALL_CONFIG"
  rm -f "$helper_config"

  local sudoers_file sudoers_temp
  sudoers_file="/etc/sudoers.d/$SERVICE_PREFIX-uninstall"
  sudoers_temp="$(mktemp)"
  {
    printf '# Allow Cloud Migration Console to schedule its controlled self-uninstall only.\n'
    printf '%s ALL=(root) NOPASSWD: %s schedule\n' "$RUN_USER" "$UNINSTALL_HELPER"
    printf '%s ALL=(root) NOPASSWD: %s schedule --purge-local-data\n' "$RUN_USER" "$UNINSTALL_HELPER"
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
Description=Cloud Migration Console FastAPI backend
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
Description=Cloud Migration Console Celery worker
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
Description=Cloud Migration Console scheduled job runner
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
Description=Run Cloud Migration Console scheduler every minute

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

restore_tls_service() {
  local tls_mode
  tls_mode="$(grep '^OCI_MIGRATOR_TLS_MODE=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  case "$tls_mode" in
    letsencrypt|custom)
      [ -f "$TLS_CADDYFILE" ] || fail "HTTPS mode is $tls_mode but the managed Caddyfile is missing: $TLS_CADDYFILE"
      log "Starting managed HTTPS service"
      "${SUDO[@]}" systemctl enable "$TLS_SERVICE" >/dev/null
      "${SUDO[@]}" systemctl restart "$TLS_SERVICE"
      ;;
    *)
      ;;
  esac
}

open_firewall_ports() {
  [ "$OPEN_FIREWALL" = "1" ] || return 0

  local ports=(22 80 443 "$API_PORT" 445 2049)
  local port
  local seen=" "

  for port in "${ports[@]}"; do
    case "$port" in
      ""|*[!0-9]*)
        fail "Invalid firewall port: $port"
        ;;
    esac
  done

  log "Opening local firewall TCP ports: 22 80 443 $API_PORT 445 2049"
  if command -v ufw >/dev/null 2>&1 && "${SUDO[@]}" ufw status | grep -q 'Status: active'; then
    for port in "${ports[@]}"; do
      case "$seen" in
        *" $port "*) continue ;;
      esac
      seen="${seen}${port} "
      "${SUDO[@]}" ufw allow "$port/tcp"
    done
    return
  fi

  if command -v iptables >/dev/null 2>&1; then
    for port in "${ports[@]}"; do
      case "$seen" in
        *" $port "*) continue ;;
      esac
      seen="${seen}${port} "
      "${SUDO[@]}" iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || "${SUDO[@]}" iptables -I INPUT 1 -p tcp --dport "$port" -j ACCEPT
    done
    command -v netfilter-persistent >/dev/null 2>&1 && "${SUDO[@]}" netfilter-persistent save || true
  fi
}

print_summary() {
  local tls_mode tls_hostname
  tls_mode="$(grep '^OCI_MIGRATOR_TLS_MODE=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || printf 'http')"
  tls_hostname="$(grep '^OCI_MIGRATOR_TLS_HOSTNAME=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  printf '\n'
  printf 'Installation complete.\n'
  if [ "$tls_mode" != "http" ] && [ -n "$tls_hostname" ]; then
    printf 'App:      https://%s\n' "$tls_hostname"
  else
    printf 'App:      http://%s:%s (setup only; configure HTTPS in Settings)\n' "${PUBLIC_HOST:-localhost}" "$API_PORT"
  fi
  printf 'API:      http://%s:%s\n' "${PUBLIC_HOST:-localhost}" "$API_PORT"
  printf 'Env file: %s\n' "$ENV_FILE"
  printf 'Time:     timezone %s, NTP %s\n' "$SERVER_TIMEZONE" "$NTP_SERVERS"
  printf 'Job logs: %s (logrotate maxsize %s, retention %s days)\n' "$JOB_LOG_DIR" "$JOB_LOG_MAX_SIZE" "$JOB_LOG_RETENTION_DAYS"
  printf 'Admin username: %s\n' "$ADMIN_USERNAME"
  if [ -n "$GENERATED_ADMIN_PASSWORD" ]; then
    printf 'Generated admin password file: %s (mode 600)\n' "$ADMIN_PASSWORD_OUTPUT_FILE"
    printf 'Read it only from a trusted terminal and store it securely.\n'
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
  install_caddy_support
  load_admin_password_input
  install_node
  install_rclone
  validate_time_settings
  validate_job_log_settings
  ensure_env_file
  persist_generated_admin_password
  load_time_settings_from_env
  load_time_sync_helper_from_env
  load_network_helper_from_env
  load_tls_helper_from_env
  load_job_log_settings_from_env
  validate_time_settings
  validate_job_log_settings
  configure_time_sync
  ensure_local_data_root
  ensure_job_log_dir
  install_job_logrotate_config
  install_job_log_helper
  install_time_sync_helper
  install_network_helper
  install_tls_helper
  install_local_share_helper
  install_upgrade_helper
  install_uninstall_helper
  install_backend
  install_frontend
  stop_services
  check_ports
  write_systemd_units
  start_services
  restore_tls_service
  open_firewall_ports
  print_summary
}

main "$@"
