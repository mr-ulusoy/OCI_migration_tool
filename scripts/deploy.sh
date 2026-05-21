#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SSH_HOST="${SSH_HOST:-ubuntu@207.127.90.146}"
SSH_KEY="${SSH_KEY:-/Users/mr-ulusoy/Documents/ssh1/cloudssh}"
REMOTE_DIR="${REMOTE_DIR:-/home/ubuntu/oci-migrator}"
PUBLIC_HOST="${PUBLIC_HOST:-${SSH_HOST#*@}}"
API_PORT="${API_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
INSTALL_FRONTEND_SERVICE="${INSTALL_FRONTEND_SERVICE:-1}"
OPEN_FIREWALL="${OPEN_FIREWALL:-0}"
STOP_LEGACY_PROCESSES="${STOP_LEGACY_PROCESSES:-0}"

case "$REMOTE_DIR" in
  *" "*) echo "REMOTE_DIR cannot contain spaces: $REMOTE_DIR" >&2; exit 1 ;;
esac

quote() {
  printf '%q' "$1"
}

SSH_OPTS=(
  -i "$SSH_KEY"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
)

RSYNC_EXCLUDES=(
  --exclude '.git/'
  --include '.env.example'
  --exclude '.env'
  --exclude '.env.*'
  --exclude 'venv/'
  --exclude 'backend/__pycache__/'
  --exclude 'backend/*.log'
  --exclude 'frontend/node_modules/'
  --exclude 'frontend/dist/'
  --include 'frontend/.env.example'
  --exclude 'frontend/.env'
  --exclude 'frontend/.env.*'
)

RSYNC_DELETE_ARGS=()
if [ "${RSYNC_DELETE:-0}" = "1" ]; then
  RSYNC_DELETE_ARGS=(--delete)
fi

echo "Deploying $PROJECT_DIR to $SSH_HOST:$REMOTE_DIR"

ssh "${SSH_OPTS[@]}" "$SSH_HOST" "mkdir -p $(quote "$REMOTE_DIR")"

rsync -az --progress \
  "${RSYNC_DELETE_ARGS[@]}" \
  "${RSYNC_EXCLUDES[@]}" \
  -e "ssh -i $SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
  "$PROJECT_DIR/" \
  "$SSH_HOST:$REMOTE_DIR/"

remote_cmd="cd $(quote "$REMOTE_DIR") && chmod +x install.sh && "
remote_cmd+="PUBLIC_HOST=$(quote "$PUBLIC_HOST") "
remote_cmd+="API_PORT=$(quote "$API_PORT") "
remote_cmd+="FRONTEND_PORT=$(quote "$FRONTEND_PORT") "
remote_cmd+="INSTALL_FRONTEND_SERVICE=$(quote "$INSTALL_FRONTEND_SERVICE") "
remote_cmd+="OPEN_FIREWALL=$(quote "$OPEN_FIREWALL") "
remote_cmd+="STOP_LEGACY_PROCESSES=$(quote "$STOP_LEGACY_PROCESSES") "

if [ -n "${OCI_MIGRATOR_API_TOKEN:-}" ]; then
  remote_cmd+="OCI_MIGRATOR_API_TOKEN=$(quote "$OCI_MIGRATOR_API_TOKEN") "
fi

remote_cmd+="./install.sh"

ssh "${SSH_OPTS[@]}" "$SSH_HOST" "$remote_cmd"
