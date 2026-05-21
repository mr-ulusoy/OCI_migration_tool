#!/usr/bin/env bash

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/mr-ulusoy/OCI_migration_tool.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/oci-migrator}"
PUBLIC_HOST="${PUBLIC_HOST:-}"
RUN_USER="${RUN_USER:-}"
INSTALL_ARGS=()

if [ "$(id -u)" -eq 0 ]; then
  SUDO=()
else
  SUDO=(sudo)
fi

usage() {
  cat <<EOF
OCI Migrator bootstrap

This script clones or updates OCI Migrator, then runs install.sh.

Usage:
  scripts/bootstrap.sh [options] [-- extra install.sh args]

Options:
  --repo URL                  Git repository URL. Default: $REPO_URL
  --branch NAME               Git branch/tag. Default: $BRANCH
  --install-dir PATH          Install directory. Default: $INSTALL_DIR
  --public-host HOST          Public IP/DNS passed to install.sh.
  --run-user USER             Linux user that owns files and runs services.
  -h, --help                  Show this help.

Examples:
  ./scripts/bootstrap.sh --public-host <server-ip-or-dns>
  ./scripts/bootstrap.sh --install-dir /opt/oci-migrator-dev --public-host dev.example.com -- --service-prefix migrator-dev --api-port 8100 --frontend-port 5174 --env-file ~/.oci-migrator-dev.env
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo)
      REPO_URL="$2"
      shift 2
      ;;
    --branch)
      BRANCH="$2"
      shift 2
      ;;
    --install-dir)
      INSTALL_DIR="$2"
      shift 2
      ;;
    --public-host)
      PUBLIC_HOST="$2"
      shift 2
      ;;
    --run-user)
      RUN_USER="$2"
      shift 2
      ;;
    --)
      shift
      INSTALL_ARGS+=("$@")
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      INSTALL_ARGS+=("$1")
      shift
      ;;
  esac
done

if [ -z "$RUN_USER" ]; then
  if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER:-}" != "root" ]; then
    RUN_USER="$SUDO_USER"
  else
    RUN_USER="$(id -un)"
  fi
fi

case "$INSTALL_DIR" in
  ""|"/"|"/opt"|"/usr"|"/home"|"/var"|"/tmp")
    echo "Refusing unsafe install directory: $INSTALL_DIR" >&2
    exit 1
    ;;
esac

if ! command -v git >/dev/null 2>&1; then
  command -v apt-get >/dev/null 2>&1 || {
    echo "git is required and apt-get was not found." >&2
    exit 1
  }
  "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get update
  "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y git
fi

"${SUDO[@]}" mkdir -p "$INSTALL_DIR"
"${SUDO[@]}" chown "$RUN_USER:$RUN_USER" "$INSTALL_DIR"

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "Updating existing checkout in $INSTALL_DIR"
  "${SUDO[@]}" -H -u "$RUN_USER" git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  "${SUDO[@]}" -H -u "$RUN_USER" git -C "$INSTALL_DIR" checkout "$BRANCH"
  "${SUDO[@]}" -H -u "$RUN_USER" git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
else
  if [ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    echo "$INSTALL_DIR exists and is not empty. Choose another --install-dir or clear it first." >&2
    exit 1
  fi
  echo "Cloning $REPO_URL#$BRANCH into $INSTALL_DIR"
  "${SUDO[@]}" -H -u "$RUN_USER" git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

cmd=(./install.sh --run-user "$RUN_USER")
if [ -n "$PUBLIC_HOST" ]; then
  cmd+=(--public-host "$PUBLIC_HOST")
fi
cmd+=("${INSTALL_ARGS[@]}")

cd "$INSTALL_DIR"
chmod +x install.sh
exec "${cmd[@]}"
