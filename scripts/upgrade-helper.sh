#!/usr/bin/env bash

set -euo pipefail

APP_NAME="${APP_NAME:-oci-migrator}"
CONFIG_FILE="${OCI_MIGRATOR_UPGRADE_CONFIG:-/etc/oci-migrator/upgrade.conf}"

fail() {
  printf '[%s upgrade] ERROR: %s\n' "$APP_NAME" "$*" >&2
  exit 1
}

usage() {
  cat <<EOF
Cloud Migration Console upgrade helper

Usage:
  oci-migrator-upgrade start
  oci-migrator-upgrade run <release-tag>
EOF
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail "This helper must run as root."
}

load_config() {
  [ -f "$CONFIG_FILE" ] || fail "Missing upgrade config: $CONFIG_FILE"
  # shellcheck source=/etc/oci-migrator/upgrade.conf
  . "$CONFIG_FILE"

  : "${INSTALL_DIR:?Missing INSTALL_DIR}"
  : "${RUN_USER:?Missing RUN_USER}"
  : "${REPO_URL:?Missing REPO_URL}"
  : "${BRANCH:?Missing BRANCH}"
  : "${API_PORT:?Missing API_PORT}"
  : "${SERVICE_PREFIX:?Missing SERVICE_PREFIX}"
  : "${ENV_FILE:?Missing ENV_FILE}"
  : "${UPGRADE_HELPER:?Missing UPGRADE_HELPER}"
  : "${UPGRADE_STATE_DIR:?Missing UPGRADE_STATE_DIR}"
  : "${UPGRADE_STATUS_FILE:?Missing UPGRADE_STATUS_FILE}"
  : "${UPGRADE_LOG_FILE:?Missing UPGRADE_LOG_FILE}"
  UPGRADE_REQUEST_FILE="${UPGRADE_REQUEST_FILE:-$UPGRADE_STATE_DIR/request.json}"
}

validate_config() {
  [[ "$INSTALL_DIR" = /* ]] || fail "INSTALL_DIR must be absolute."
  [[ "$ENV_FILE" = /* ]] || fail "ENV_FILE must be absolute."
  [[ "$UPGRADE_HELPER" = /* ]] || fail "UPGRADE_HELPER must be absolute."
  [[ "$UPGRADE_STATE_DIR" = /* ]] || fail "UPGRADE_STATE_DIR must be absolute."
  [[ "$UPGRADE_STATUS_FILE" = /* ]] || fail "UPGRADE_STATUS_FILE must be absolute."
  [[ "$UPGRADE_LOG_FILE" = /* ]] || fail "UPGRADE_LOG_FILE must be absolute."
  [[ "$UPGRADE_REQUEST_FILE" = /* ]] || fail "UPGRADE_REQUEST_FILE must be absolute."
  [[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || fail "Invalid branch name."
  id "$RUN_USER" >/dev/null 2>&1 || fail "Run user does not exist: $RUN_USER"
  [ -d "$INSTALL_DIR/.git" ] || fail "Install directory is not a git checkout: $INSTALL_DIR"
  [ -x "$INSTALL_DIR/install.sh" ] || fail "Missing install.sh in $INSTALL_DIR"
}

prepare_paths() {
  install -d -o "$RUN_USER" -g "$RUN_USER" -m 750 "$UPGRADE_STATE_DIR"
  install -d -o "$RUN_USER" -g "$RUN_USER" -m 750 "$(dirname "$UPGRADE_LOG_FILE")"
  touch "$UPGRADE_LOG_FILE"
  chown "$RUN_USER:$RUN_USER" "$UPGRADE_LOG_FILE"
  chmod 640 "$UPGRADE_LOG_FILE"
}

now_utc() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

write_status() {
  local state="$1"
  local message="$2"
  local current_commit="${3:-}"
  local target_commit="${4:-}"
  local phase="${5:-}"
  local current_version="${6:-}"
  local target_version="${7:-}"

  STATUS_FILE="$UPGRADE_STATUS_FILE" \
  STATE="$state" \
  MESSAGE="$message" \
  CURRENT_COMMIT="$current_commit" \
  TARGET_COMMIT="$target_commit" \
  PHASE="$phase" \
  CURRENT_VERSION="$current_version" \
  TARGET_VERSION="$target_version" \
  LOG_FILE="$UPGRADE_LOG_FILE" \
  python3 - <<'PY'
import json
import os
import tempfile
from datetime import datetime, timezone

path = os.environ["STATUS_FILE"]
state = os.environ["STATE"]
now = datetime.now(timezone.utc).isoformat()

data = {}
try:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
except (FileNotFoundError, json.JSONDecodeError):
    data = {}

if state == "running" and data.get("status") != "running":
    data["started_at"] = now
    data["finished_at"] = None
if state in {"success", "failed"}:
    data["finished_at"] = now

data.update(
    {
        "status": state,
        "message": os.environ["MESSAGE"],
        "phase": os.environ.get("PHASE", ""),
        "current_commit": os.environ.get("CURRENT_COMMIT", ""),
        "target_commit": os.environ.get("TARGET_COMMIT", ""),
        "current_version": os.environ.get("CURRENT_VERSION", ""),
        "target_version": os.environ.get("TARGET_VERSION", ""),
        "updated_at": now,
        "log_file": os.environ["LOG_FILE"],
    }
)

os.makedirs(os.path.dirname(path), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
finally:
    if os.path.exists(tmp):
        os.remove(tmp)
PY
  chown "$RUN_USER:$RUN_USER" "$UPGRADE_STATUS_FILE" 2>/dev/null || true
}

log() {
  printf '[%s] %s\n' "$(now_utc)" "$*" | tee -a "$UPGRADE_LOG_FILE" >/dev/null
}

run_cmd() {
  log "+ $*"
  "$@" >>"$UPGRADE_LOG_FILE" 2>&1
}

run_as_user() {
  run_cmd sudo -H -u "$RUN_USER" "$@"
}

git_value() {
  sudo -H -u "$RUN_USER" git -C "$INSTALL_DIR" "$@" 2>/dev/null || true
}

version_value() {
  local version_file="$INSTALL_DIR/VERSION"
  [ -f "$version_file" ] || return 0
  tr -d '[:space:]' < "$version_file"
}

validate_release_tag() {
  [[ "${1:-}" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
    || fail "Invalid release tag '${1:-}'. Expected vMAJOR.MINOR.PATCH."
}

requested_release_tag() {
  [ -f "$UPGRADE_REQUEST_FILE" ] || fail "No release request was prepared by the API."
  REQUEST_FILE="$UPGRADE_REQUEST_FILE" python3 - <<'PY'
import json
import os

with open(os.environ["REQUEST_FILE"], "r", encoding="utf-8") as handle:
    payload = json.load(handle)
print(str(payload.get("tag", "")).strip())
PY
}

upgrade_process_is_running() {
  local lock_dir="$UPGRADE_STATE_DIR/upgrade.lock"
  local pid_file="$lock_dir/pid"
  if [ -f "$pid_file" ]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi

  pgrep -f -- "$UPGRADE_HELPER run" >/dev/null 2>&1
}

clear_stale_upgrade_lock() {
  local lock_dir="$UPGRADE_STATE_DIR/upgrade.lock"
  [ -d "$lock_dir" ] || return 0

  if upgrade_process_is_running; then
    return 1
  fi

  rm -rf "$lock_dir"
  return 0
}

finish_failed() {
  local exit_code=$?
  if [ "$exit_code" -ne 0 ]; then
    log "Upgrade failed with exit code $exit_code."
    write_status "failed" "Upgrade failed. Open the technical log for details." "$(git_value rev-parse HEAD)" "${ACTIVE_TARGET_COMMIT:-}" "failed" "$(version_value)" "${ACTIVE_TARGET_VERSION:-}"
  fi
  rm -rf "$UPGRADE_STATE_DIR/upgrade.lock"
  exit "$exit_code"
}

schedule_upgrade() {
  require_root
  load_config
  validate_config
  prepare_paths

  local target_tag target_version
  target_tag="$(requested_release_tag)"
  validate_release_tag "$target_tag"
  target_version="${target_tag#v}"

  clear_stale_upgrade_lock || true
  if [ -d "$UPGRADE_STATE_DIR/upgrade.lock" ]; then
    write_status "running" "Upgrade is already running." "$(git_value rev-parse HEAD)" "" "queued" "$(version_value)" "$target_version"
    fail "Upgrade is already running."
  fi

  write_status "running" "Release $target_tag queued." "$(git_value rev-parse HEAD)" "" "queued" "$(version_value)" "$target_version"

  if command -v systemd-run >/dev/null 2>&1; then
    systemd-run \
      --unit="$SERVICE_PREFIX-self-upgrade" \
      --collect \
      --description="Cloud Migration Console self upgrade" \
      --setenv="OCI_MIGRATOR_UPGRADE_CONFIG=$CONFIG_FILE" \
      "$UPGRADE_HELPER" run "$target_tag" >>"$UPGRADE_LOG_FILE" 2>&1
  else
    nohup env OCI_MIGRATOR_UPGRADE_CONFIG="$CONFIG_FILE" "$UPGRADE_HELPER" run "$target_tag" >>"$UPGRADE_LOG_FILE" 2>&1 &
  fi
}

run_upgrade() {
  local target_tag="${1:-}"
  validate_release_tag "$target_tag"
  require_root
  load_config
  validate_config
  prepare_paths

  clear_stale_upgrade_lock || true
  if ! mkdir "$UPGRADE_STATE_DIR/upgrade.lock" 2>/dev/null; then
    write_status "running" "Upgrade is already running." "$(git_value rev-parse HEAD)" "" "queued" "$(version_value)" "${target_tag#v}"
    fail "Upgrade is already running."
  fi
  printf '%s\n' "$$" > "$UPGRADE_STATE_DIR/upgrade.lock/pid"
  chown "$RUN_USER:$RUN_USER" "$UPGRADE_STATE_DIR/upgrade.lock/pid" 2>/dev/null || true
  trap finish_failed EXIT

  : > "$UPGRADE_LOG_FILE"
  chown "$RUN_USER:$RUN_USER" "$UPGRADE_LOG_FILE"
  chmod 640 "$UPGRADE_LOG_FILE"

  local current_commit current_version target_commit target_version new_commit new_version
  current_commit="$(git_value rev-parse HEAD)"
  current_version="$(version_value)"
  target_version="${target_tag#v}"
  ACTIVE_TARGET_VERSION="$target_version"
  write_status "running" "Checking published release $target_tag." "$current_commit" "" "checking" "$current_version" "$target_version"
  log "Starting controlled upgrade for $INSTALL_DIR to release $target_tag."

  run_as_user git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
  run_as_user git -C "$INSTALL_DIR" fetch origin "refs/tags/$target_tag:refs/tags/$target_tag"
  target_commit="$(git_value rev-list -n 1 "$target_tag")"
  [ -n "$target_commit" ] || fail "Release tag $target_tag could not be resolved."
  ACTIVE_TARGET_COMMIT="$target_commit"

  if [ -n "$current_version" ] && [ "$current_version" = "$target_version" ]; then
    log "Already on release $target_tag ($current_commit)."
    write_status "success" "Already on release $target_tag." "$current_commit" "$target_commit" "complete" "$current_version" "$target_version"
    rm -rf "$UPGRADE_STATE_DIR/upgrade.lock"
    trap - EXIT
    exit 0
  fi

  write_status "running" "Downloading release $target_tag." "$current_commit" "$target_commit" "downloading" "$current_version" "$target_version"
  run_as_user git -C "$INSTALL_DIR" checkout --detach "$target_tag"
  new_commit="$(git_value rev-parse HEAD)"
  new_version="$(version_value)"
  [ "$new_version" = "$target_version" ] \
    || fail "Release tag $target_tag contains VERSION '$new_version'; expected '$target_version'."

  write_status "running" "Installing release $target_tag and restarting services." "$new_commit" "$target_commit" "installing" "$new_version" "$target_version"
  local install_cmd
  install_cmd=(
    ./install.sh
    --run-user "$RUN_USER"
    --service-prefix "$SERVICE_PREFIX"
    --api-port "$API_PORT"
    --env-file "$ENV_FILE"
    --local-data-root "${LOCAL_DATA_ROOT:-/var/lib/oci-migrator/local}"
    --job-log-dir "${JOB_LOG_DIR:-/var/log/oci-migrator/jobs}"
    --job-log-max-size "${JOB_LOG_MAX_SIZE:-10M}"
    --job-log-retention-days "${JOB_LOG_RETENTION_DAYS:-14}"
    --job-log-helper "${JOB_LOG_HELPER:-/usr/local/sbin/oci-migrator-job-log}"
    --local-share-helper "${LOCAL_SHARE_HELPER:-/usr/local/sbin/oci-migrator-local-share}"
    --time-sync-helper "${TIME_SYNC_HELPER:-/usr/local/sbin/oci-migrator-time-sync}"
    --network-helper "${NETWORK_HELPER:-/usr/local/sbin/oci-migrator-network}"
    --tls-helper "${TLS_HELPER:-/usr/local/sbin/oci-migrator-tls}"
    --upgrade-helper "$UPGRADE_HELPER"
    --uninstall-helper "${UNINSTALL_HELPER:-/usr/local/sbin/oci-migrator-uninstall}"
    --timezone "${SERVER_TIMEZONE:-Europe/Stockholm}"
    --ntp-servers "${NTP_SERVERS:-0.se.pool.ntp.org 1.se.pool.ntp.org 2.se.pool.ntp.org 3.se.pool.ntp.org}"
    --celery-concurrency "${CELERY_CONCURRENCY:-2}"
  )
  if [ -n "${PUBLIC_HOST:-}" ]; then
    install_cmd+=(--public-host "$PUBLIC_HOST")
  fi
  if [ "${OPEN_FIREWALL:-0}" = "1" ]; then
    install_cmd+=(--open-firewall)
  fi

  cd "$INSTALL_DIR"
  log "+ ${install_cmd[*]}"
  "${install_cmd[@]}" >>"$UPGRADE_LOG_FILE" 2>&1

  write_status "success" "Upgrade to $target_tag complete." "$new_commit" "$target_commit" "complete" "$new_version" "$target_version"
  log "Upgrade to $target_tag complete at $new_commit."
  rm -rf "$UPGRADE_STATE_DIR/upgrade.lock"
  trap - EXIT
}

main() {
  case "${1:-}" in
    start)
      schedule_upgrade
      ;;
    run)
      run_upgrade "${2:-}"
      ;;
    -h|--help)
      usage
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
