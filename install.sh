#!/usr/bin/env bash

set -euo pipefail

APP_NAME="${APP_NAME:-oci-migrator}"
SERVICE_PREFIX="${SERVICE_PREFIX:-migrator}"
API_HOST="${API_HOST:-0.0.0.0}"
API_PORT="${API_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-2}"
INSTALL_FRONTEND_SERVICE="${INSTALL_FRONTEND_SERVICE:-1}"
OPEN_FIREWALL="${OPEN_FIREWALL:-0}"
STOP_LEGACY_PROCESSES="${STOP_LEGACY_PROCESSES:-0}"
PRINT_TOKEN="${PRINT_TOKEN:-0}"
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
  --public-host HOST          Public IP/DNS used by the frontend build and CORS allowlist.
  --api-port PORT             Backend port. Default: $API_PORT
  --frontend-port PORT        Frontend port. Default: $FRONTEND_PORT
  --service-prefix NAME       systemd service prefix. Default: $SERVICE_PREFIX
  --run-user USER             Linux user that runs services. Default: current sudo user.
  --env-file PATH             Runtime env file. Default: ~/.oci-migrator.env for the run user.
  --celery-concurrency N      Celery worker concurrency. Default: $CELERY_CONCURRENCY
  --open-firewall             Open local firewall ports with ufw/iptables when possible.
  --stop-legacy-processes     Stop old manual uvicorn/vite processes from this project path.
  --no-frontend-service       Build frontend, but do not create/start migrator-frontend.service.
  --print-token               Print API token in the final summary.
  -h, --help                  Show this help.

Environment variables with the same names are also supported:
  PUBLIC_HOST, API_PORT, FRONTEND_PORT, SERVICE_PREFIX, RUN_USER,
  OCI_MIGRATOR_ENV_FILE, OPEN_FIREWALL, STOP_LEGACY_PROCESSES,
  INSTALL_FRONTEND_SERVICE, CELERY_CONCURRENCY, PRINT_TOKEN.
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
        FRONTEND_PORT="$2"
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
        INSTALL_FRONTEND_SERVICE=0
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
    openssl \
    python3 \
    python3-pip \
    python3-venv \
    redis-server \
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
  allowed_origins="${OCI_MIGRATOR_ALLOWED_ORIGINS:-http://localhost:$FRONTEND_PORT,http://127.0.0.1:$FRONTEND_PORT,http://$PUBLIC_HOST:$FRONTEND_PORT}"

  if [ ! -f "$ENV_FILE" ]; then
    log "Creating $ENV_FILE"
    local token
    token="${OCI_MIGRATOR_API_TOKEN:-$(generate_token)}"

    local temp_file
    temp_file="$(mktemp)"
    {
      printf 'OCI_MIGRATOR_API_TOKEN=%s\n' "$token"
      printf 'OCI_MIGRATOR_ALLOWED_ORIGINS=%s\n' "$allowed_origins"
      printf 'OCI_MIGRATOR_REDIS_URL=redis://localhost:6379/0\n'
      printf 'OCI_MIGRATOR_LOG_LEVEL=INFO\n'
      printf 'OCI_MIGRATOR_RCLONE_TIMEOUT_SECONDS=7200\n'
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
  fi
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
  local api_base="http://${PUBLIC_HOST:-localhost}:$API_PORT"
  local temp_file
  temp_file="$(mktemp)"
  {
    printf 'VITE_API_BASE=%s\n' "$api_base"
    printf '# Keep VITE_API_TOKEN unset for normal installs. Set the token in browser localStorage instead.\n'
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

  if [ "$INSTALL_FRONTEND_SERVICE" = "1" ] && port_is_in_use "$FRONTEND_PORT"; then
    "${SUDO[@]}" ss -ltnp | grep ":$FRONTEND_PORT" || true
    fail "Port $FRONTEND_PORT is already in use. Stop that process or set FRONTEND_PORT=another_port."
  fi
}

write_systemd_units() {
  log "Writing systemd services"

  local npm_bin
  npm_bin="$(command -v npm)"

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

  if [ "$INSTALL_FRONTEND_SERVICE" = "1" ]; then
    "${SUDO[@]}" tee "/etc/systemd/system/$SERVICE_PREFIX-frontend.service" >/dev/null <<EOF
[Unit]
Description=OCI Migrator frontend
After=network-online.target $SERVICE_PREFIX-api.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$FRONTEND_DIR
Environment=NODE_ENV=production
ExecStart=$npm_bin run preview -- --host 0.0.0.0 --port $FRONTEND_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  fi
}

start_services() {
  log "Starting services"
  "${SUDO[@]}" systemctl daemon-reload
  "${SUDO[@]}" systemctl enable --now redis-server
  "${SUDO[@]}" systemctl enable --now "$SERVICE_PREFIX-api.service"
  "${SUDO[@]}" systemctl enable --now "$SERVICE_PREFIX-worker.service"
  "${SUDO[@]}" systemctl enable --now "$SERVICE_PREFIX-scheduler.timer"

  if [ "$INSTALL_FRONTEND_SERVICE" = "1" ]; then
    "${SUDO[@]}" systemctl enable --now "$SERVICE_PREFIX-frontend.service"
  fi
}

open_firewall_ports() {
  [ "$OPEN_FIREWALL" = "1" ] || return 0

  log "Opening local firewall ports"
  if command -v ufw >/dev/null 2>&1 && "${SUDO[@]}" ufw status | grep -q 'Status: active'; then
    "${SUDO[@]}" ufw allow "$API_PORT/tcp"
    [ "$INSTALL_FRONTEND_SERVICE" = "1" ] && "${SUDO[@]}" ufw allow "$FRONTEND_PORT/tcp"
    return
  fi

  if command -v iptables >/dev/null 2>&1; then
    "${SUDO[@]}" iptables -C INPUT -p tcp --dport "$API_PORT" -j ACCEPT 2>/dev/null || "${SUDO[@]}" iptables -I INPUT 1 -p tcp --dport "$API_PORT" -j ACCEPT
    if [ "$INSTALL_FRONTEND_SERVICE" = "1" ]; then
      "${SUDO[@]}" iptables -C INPUT -p tcp --dport "$FRONTEND_PORT" -j ACCEPT 2>/dev/null || "${SUDO[@]}" iptables -I INPUT 1 -p tcp --dport "$FRONTEND_PORT" -j ACCEPT
    fi
    command -v netfilter-persistent >/dev/null 2>&1 && "${SUDO[@]}" netfilter-persistent save || true
  fi
}

print_summary() {
  printf '\n'
  printf 'Installation complete.\n'
  printf 'Backend:  http://%s:%s\n' "${PUBLIC_HOST:-localhost}" "$API_PORT"
  if [ "$INSTALL_FRONTEND_SERVICE" = "1" ]; then
    printf 'Frontend: http://%s:%s\n' "${PUBLIC_HOST:-localhost}" "$FRONTEND_PORT"
  fi
  printf 'Env file: %s\n' "$ENV_FILE"
  if [ "$PRINT_TOKEN" = "1" ] && [ -f "$ENV_FILE" ]; then
    printf 'API token: %s\n' "$(grep '^OCI_MIGRATOR_API_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2- || printf '<not found>')"
  else
    printf 'API token: stored in %s (use --print-token only on trusted terminals)\n' "$ENV_FILE"
  fi
  printf '\n'
  printf 'Useful commands:\n'
  printf '  sudo systemctl status %s-api %s-worker %s-frontend %s-scheduler.timer\n' "$SERVICE_PREFIX" "$SERVICE_PREFIX" "$SERVICE_PREFIX" "$SERVICE_PREFIX"
  printf '  journalctl -u %s-api -f\n' "$SERVICE_PREFIX"
}

main() {
  parse_args "$@"
  initialize_runtime_paths
  ensure_supported_os
  install_system_dependencies
  install_node
  install_rclone
  ensure_env_file
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
