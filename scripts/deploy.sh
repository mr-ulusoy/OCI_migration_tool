#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SSH_HOST="${SSH_HOST:-}"
SSH_KEY="${SSH_KEY:-}"
REMOTE_DIR="${REMOTE_DIR:-/opt/oci-migrator}"
PUBLIC_HOST="${PUBLIC_HOST:-}"
API_PORT="${API_PORT:-8000}"
LOCAL_DATA_ROOT="${OCI_MIGRATOR_LOCAL_DATA_ROOT:-}"
SERVICE_PREFIX="${SERVICE_PREFIX:-migrator}"
CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-2}"
OPEN_FIREWALL="${OPEN_FIREWALL:-0}"
SHARE_ALLOW_CIDR="${OCI_MIGRATOR_SHARE_ALLOW_CIDR:-10.0.0.0/8}"
STOP_LEGACY_PROCESSES="${STOP_LEGACY_PROCESSES:-0}"
PRINT_TOKEN="${PRINT_TOKEN:-0}"

case "$REMOTE_DIR" in
  *" "*) echo "REMOTE_DIR cannot contain spaces: $REMOTE_DIR" >&2; exit 1 ;;
esac

if [ -z "$SSH_HOST" ] || [ -z "$SSH_KEY" ]; then
  cat >&2 <<EOF
SSH_HOST and SSH_KEY are required.

Example:
  SSH_HOST=ubuntu@1.2.3.4 SSH_KEY=/path/to/key PUBLIC_HOST=1.2.3.4 ./scripts/deploy.sh
EOF
  exit 1
fi

if [ -z "$PUBLIC_HOST" ]; then
  PUBLIC_HOST="${SSH_HOST#*@}"
fi

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
if [ -n "$LOCAL_DATA_ROOT" ]; then
  remote_cmd+="OCI_MIGRATOR_LOCAL_DATA_ROOT=$(quote "$LOCAL_DATA_ROOT") "
fi
remote_cmd+="SERVICE_PREFIX=$(quote "$SERVICE_PREFIX") "
remote_cmd+="CELERY_CONCURRENCY=$(quote "$CELERY_CONCURRENCY") "
remote_cmd+="OPEN_FIREWALL=$(quote "$OPEN_FIREWALL") "
remote_cmd+="OCI_MIGRATOR_SHARE_ALLOW_CIDR=$(quote "$SHARE_ALLOW_CIDR") "
remote_cmd+="STOP_LEGACY_PROCESSES=$(quote "$STOP_LEGACY_PROCESSES") "
remote_cmd+="PRINT_TOKEN=$(quote "$PRINT_TOKEN") "

if [ -n "${OCI_MIGRATOR_API_TOKEN:-}" ]; then
  remote_cmd+="OCI_MIGRATOR_API_TOKEN=$(quote "$OCI_MIGRATOR_API_TOKEN") "
fi
if [ -n "${OCI_MIGRATOR_ADMIN_USERNAME:-}" ]; then
  remote_cmd+="OCI_MIGRATOR_ADMIN_USERNAME=$(quote "$OCI_MIGRATOR_ADMIN_USERNAME") "
fi
if [ -n "${OCI_MIGRATOR_ADMIN_PASSWORD:-}" ]; then
  remote_cmd+="OCI_MIGRATOR_ADMIN_PASSWORD=$(quote "$OCI_MIGRATOR_ADMIN_PASSWORD") "
fi
if [ -n "${OCI_MIGRATOR_ADMIN_PASSWORD_FILE:-}" ]; then
  remote_cmd+="OCI_MIGRATOR_ADMIN_PASSWORD_FILE=$(quote "$OCI_MIGRATOR_ADMIN_PASSWORD_FILE") "
fi
if [ -n "${PROMPT_ADMIN_PASSWORD:-}" ]; then
  remote_cmd+="PROMPT_ADMIN_PASSWORD=$(quote "$PROMPT_ADMIN_PASSWORD") "
fi
if [ -n "${OCI_MIGRATOR_ENV_FILE:-}" ]; then
  remote_cmd+="OCI_MIGRATOR_ENV_FILE=$(quote "$OCI_MIGRATOR_ENV_FILE") "
fi
if [ -n "${RUN_USER:-}" ]; then
  remote_cmd+="RUN_USER=$(quote "$RUN_USER") "
fi

remote_cmd+="./install.sh"

ssh "${SSH_OPTS[@]}" "$SSH_HOST" "$remote_cmd"
