#!/usr/bin/env bash

set -euo pipefail

APP_NAME="${APP_NAME:-oci-migrator}"
TIMEZONE=""
NTP_SERVERS=""
TIMESYNCD_CONF="/etc/systemd/timesyncd.conf.d/oci-migrator.conf"

fail() {
  printf '[%s time-sync] ERROR: %s\n' "$APP_NAME" "$*" >&2
  exit 1
}

usage() {
  cat <<EOF
Cloud Migration Console time sync helper

Usage:
  oci-migrator-time-sync configure --timezone ZONE --ntp-servers "LIST" [--timesyncd-conf PATH]
EOF
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail "This helper must run as root."
}

normalize_ntp_servers() {
  NTP_SERVERS="$(printf '%s' "$NTP_SERVERS" | tr ',' ' ' | awk '{$1=$1; print}')"
}

validate_timezone() {
  [ -n "$TIMEZONE" ] || fail "Timezone is required."
  [[ "$TIMEZONE" =~ ^[A-Za-z0-9_+.-]+(/[A-Za-z0-9_+.-]+)*$ ]] || fail "Invalid timezone format."
  [ -f "/usr/share/zoneinfo/$TIMEZONE" ] || fail "Timezone data not found for $TIMEZONE."
}

validate_ntp_servers() {
  normalize_ntp_servers
  [ -n "$NTP_SERVERS" ] || fail "NTP servers are required."

  local server
  for server in $NTP_SERVERS; do
    [[ "$server" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]*$ ]] || fail "Invalid NTP server: $server"
  done
}

validate_timesyncd_conf() {
  [ -n "$TIMESYNCD_CONF" ] || fail "timesyncd config path is required."
  [[ "$TIMESYNCD_CONF" = /etc/systemd/timesyncd.conf.d/* ]] || fail "timesyncd config must be under /etc/systemd/timesyncd.conf.d."
  [[ "$(basename "$TIMESYNCD_CONF")" =~ ^[A-Za-z0-9._-]+$ ]] || fail "Invalid timesyncd config file name."
}

write_timesyncd_config() {
  local timesyncd_dir candidate_file
  timesyncd_dir="$(dirname "$TIMESYNCD_CONF")"
  install -d -o root -g root -m 755 "$timesyncd_dir"
  candidate_file="$(mktemp "$timesyncd_dir/.oci-migrator-timesyncd.XXXXXX")"

  {
    printf '[Time]\n'
    printf 'NTP=%s\n' "$NTP_SERVERS"
    printf 'FallbackNTP=0.pool.ntp.org 1.pool.ntp.org 2.pool.ntp.org 3.pool.ntp.org\n'
  } > "$candidate_file"

  chown root:root "$candidate_file"
  chmod 644 "$candidate_file"
  mv "$candidate_file" "$TIMESYNCD_CONF"
}

apply_time_settings() {
  write_timesyncd_config

  if command -v timedatectl >/dev/null 2>&1; then
    timedatectl set-timezone "$TIMEZONE"
    timedatectl set-ntp true
  fi

  systemctl enable --now systemd-timesyncd.service >/dev/null 2>&1 || true
  systemctl restart systemd-timesyncd.service >/dev/null 2>&1 || true
}

print_json() {
  TIMEZONE="$TIMEZONE" \
  NTP_SERVERS="$NTP_SERVERS" \
  TIMESYNCD_CONF="$TIMESYNCD_CONF" \
  python3 - <<'PY'
import json
import os

print(json.dumps({
    "message": "Time sync settings updated",
    "timezone": os.environ["TIMEZONE"],
    "ntp_servers": os.environ["NTP_SERVERS"],
    "timesyncd_conf": os.environ["TIMESYNCD_CONF"],
}))
PY
}

parse_args() {
  [ "$#" -gt 0 ] || {
    usage
    exit 1
  }

  local action
  action="$1"
  shift
  [ "$action" = "configure" ] || fail "Unknown action: $action"

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --timezone)
        TIMEZONE="$2"
        shift 2
        ;;
      --ntp-servers)
        NTP_SERVERS="$2"
        shift 2
        ;;
      --timesyncd-conf)
        TIMESYNCD_CONF="$2"
        shift 2
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

main() {
  parse_args "$@"
  require_root
  validate_timezone
  validate_ntp_servers
  validate_timesyncd_conf
  apply_time_settings
  print_json
}

main "$@"
