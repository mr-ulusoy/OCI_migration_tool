#!/usr/bin/env bash

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/mr-ulusoy/OCI_migration_tool.git}"
BRANCH="${BRANCH:-}"
RELEASE_TAG="${RELEASE_TAG:-latest}"
INSTALL_DIR="${INSTALL_DIR:-/opt/oci-migrator}"
PUBLIC_HOST="${PUBLIC_HOST:-}"
RUN_USER="${RUN_USER:-}"
ADMIN_USERNAME="${OCI_MIGRATOR_ADMIN_USERNAME:-}"
ADMIN_PASSWORD="${OCI_MIGRATOR_ADMIN_PASSWORD:-}"
ADMIN_PASSWORD_FILE="${OCI_MIGRATOR_ADMIN_PASSWORD_FILE:-}"
PROMPT_ADMIN_PASSWORD="${PROMPT_ADMIN_PASSWORD:-0}"
SERVER_TIMEZONE="${OCI_MIGRATOR_TIMEZONE:-}"
NTP_SERVERS="${OCI_MIGRATOR_NTP_SERVERS:-}"
INSTALL_ARGS=()

if [ "$(id -u)" -eq 0 ]; then
  SUDO=()
else
  SUDO=(sudo)
fi

usage() {
  cat <<EOF
Cloud Migration Console bootstrap

This script clones or updates Cloud Migration Console, then runs install.sh.

Usage:
  scripts/bootstrap.sh [options] [-- extra install.sh args]

Options:
  --repo URL                  Git repository URL. Default: $REPO_URL
  --release TAG              Published release tag. Default: latest
  --branch NAME               Developer override; install a branch instead of a release.
  --install-dir PATH          Install directory. Default: $INSTALL_DIR
  --public-host HOST          Public IP/DNS passed to install.sh.
  --run-user USER             Linux user that owns files and runs services.
  --admin-username USERNAME   Admin login username.
  --admin-password PASSWORD   Set or reset the admin password.
  --admin-password-file PATH  Read admin password from a file.
  --prompt-admin-password     Prompt for admin password without storing it in shell history.
  --timezone ZONE             Server timezone passed to install.sh.
  --ntp-servers "LIST"        Space/comma separated NTP servers passed to install.sh.
  --no-open-firewall          Do not open local firewall ports during install.
  -h, --help                  Show this help.

Examples:
  ./scripts/bootstrap.sh --public-host <server-ip-or-dns>
  ./scripts/bootstrap.sh --public-host <server-ip-or-dns> --prompt-admin-password
  ./scripts/bootstrap.sh --install-dir /opt/oci-migrator-dev --public-host dev.example.com -- --service-prefix migrator-dev --api-port 8100 --env-file ~/.oci-migrator-dev.env
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
    --release)
      RELEASE_TAG="$2"
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
    --admin-username)
      ADMIN_USERNAME="$2"
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
    --timezone)
      SERVER_TIMEZONE="$2"
      shift 2
      ;;
    --ntp-servers)
      NTP_SERVERS="$2"
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

if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  command -v apt-get >/dev/null 2>&1 || {
    echo "git, curl, and python3 are required and apt-get was not found." >&2
    exit 1
  }
  "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get update
  "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y git curl python3
fi

github_repository_from_url() {
  local repository="$REPO_URL"
  repository="${repository#https://github.com/}"
  repository="${repository#http://github.com/}"
  repository="${repository#git@github.com:}"
  repository="${repository%.git}"
  repository="${repository%/}"
  [[ "$repository" =~ ^[^/]+/[^/]+$ ]] || return 1
  printf '%s\n' "$repository"
}

latest_release_tag() {
  local repository
  repository="$(github_repository_from_url)" || {
    echo "Automatic release discovery requires a github.com repository. Use --release <tag> or --branch <name>." >&2
    return 1
  }
  curl -fsSL \
    -H 'Accept: application/vnd.github+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    -H 'User-Agent: cloud-migration-console-bootstrap' \
    "https://api.github.com/repos/$repository/releases/latest" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tag_name", ""))'
}

CHECKOUT_REF="$BRANCH"
CHECKOUT_MODE="branch"
if [ -z "$CHECKOUT_REF" ]; then
  CHECKOUT_MODE="release"
  if [ "$RELEASE_TAG" = "latest" ]; then
    CHECKOUT_REF="$(latest_release_tag)"
  else
    CHECKOUT_REF="$RELEASE_TAG"
  fi
  [[ "$CHECKOUT_REF" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || {
    echo "Invalid release tag '$CHECKOUT_REF'. Expected vMAJOR.MINOR.PATCH." >&2
    exit 1
  }
fi

"${SUDO[@]}" mkdir -p "$INSTALL_DIR"
"${SUDO[@]}" chown "$RUN_USER:$RUN_USER" "$INSTALL_DIR"

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "Updating existing checkout in $INSTALL_DIR"
  if [ "$CHECKOUT_MODE" = "release" ]; then
    "${SUDO[@]}" -H -u "$RUN_USER" git -C "$INSTALL_DIR" fetch origin "refs/tags/$CHECKOUT_REF:refs/tags/$CHECKOUT_REF"
    "${SUDO[@]}" -H -u "$RUN_USER" git -C "$INSTALL_DIR" checkout --detach "$CHECKOUT_REF"
  else
    "${SUDO[@]}" -H -u "$RUN_USER" git -C "$INSTALL_DIR" fetch origin "$CHECKOUT_REF"
    "${SUDO[@]}" -H -u "$RUN_USER" git -C "$INSTALL_DIR" checkout "$CHECKOUT_REF"
    "${SUDO[@]}" -H -u "$RUN_USER" git -C "$INSTALL_DIR" pull --ff-only origin "$CHECKOUT_REF"
  fi
else
  if [ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    echo "$INSTALL_DIR exists and is not empty. Choose another --install-dir or clear it first." >&2
    exit 1
  fi
  echo "Cloning $REPO_URL#$CHECKOUT_REF into $INSTALL_DIR"
  "${SUDO[@]}" -H -u "$RUN_USER" git clone --branch "$CHECKOUT_REF" "$REPO_URL" "$INSTALL_DIR"
fi

cmd=(./install.sh --run-user "$RUN_USER")
if [ -n "$PUBLIC_HOST" ]; then
  cmd+=(--public-host "$PUBLIC_HOST")
fi
if [ -n "$ADMIN_USERNAME" ]; then
  cmd+=(--admin-username "$ADMIN_USERNAME")
fi
if [ -n "$ADMIN_PASSWORD" ]; then
  cmd+=(--admin-password "$ADMIN_PASSWORD")
fi
if [ -n "$ADMIN_PASSWORD_FILE" ]; then
  cmd+=(--admin-password-file "$ADMIN_PASSWORD_FILE")
fi
if [ "$PROMPT_ADMIN_PASSWORD" = "1" ]; then
  cmd+=(--prompt-admin-password)
fi
if [ -n "$SERVER_TIMEZONE" ]; then
  cmd+=(--timezone "$SERVER_TIMEZONE")
fi
if [ -n "$NTP_SERVERS" ]; then
  cmd+=(--ntp-servers "$NTP_SERVERS")
fi
cmd+=("${INSTALL_ARGS[@]}")

cd "$INSTALL_DIR"
chmod +x install.sh
exec "${cmd[@]}"
