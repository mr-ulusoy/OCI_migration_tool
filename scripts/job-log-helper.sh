#!/usr/bin/env bash

set -euo pipefail

APP_NAME="${APP_NAME:-oci-migrator}"
LOGROTATE_FILE="/etc/logrotate.d/migrator-job-logs"
JOB_LOG_DIR=""
MAX_SIZE=""
RETENTION_DAYS=""
RUN_USER=""

fail() {
  printf '[%s job-log] ERROR: %s\n' "$APP_NAME" "$*" >&2
  exit 1
}

usage() {
  cat <<EOF
OCI Migrator job log helper

Usage:
  oci-migrator-job-log configure --job-log-dir PATH --max-size SIZE --retention-days DAYS --run-user USER [--logrotate-file PATH]
EOF
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail "This helper must run as root."
}

validate_job_log_dir() {
  [ -n "$JOB_LOG_DIR" ] || fail "Job log directory is required."
  [[ "$JOB_LOG_DIR" = /* ]] || fail "Job log directory must be absolute."

  case "$JOB_LOG_DIR" in
    "/"|"/bin"|"/boot"|"/dev"|"/etc"|"/home"|"/lib"|"/lib64"|"/opt"|"/proc"|"/root"|"/run"|"/sbin"|"/sys"|"/tmp"|"/usr"|"/var")
      fail "Choose a specific job log directory, not a system directory."
      ;;
  esac
}

validate_max_size() {
  [ -n "$MAX_SIZE" ] || fail "Max size is required."
  [[ "$MAX_SIZE" =~ ^[1-9][0-9]*[KkMmGg]?$ ]] || fail "Max size must look like 10M, 512K, or 1G."
}

validate_retention_days() {
  [ -n "$RETENTION_DAYS" ] || fail "Retention days is required."
  [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || fail "Retention days must be a number."
  [ "$RETENTION_DAYS" -ge 1 ] || fail "Retention days must be at least 1."
  [ "$RETENTION_DAYS" -le 365 ] || fail "Retention days cannot exceed 365."
}

validate_run_user() {
  [ -n "$RUN_USER" ] || fail "Run user is required."
  id "$RUN_USER" >/dev/null 2>&1 || fail "Run user does not exist: $RUN_USER"
}

validate_logrotate_file() {
  [ -n "$LOGROTATE_FILE" ] || fail "Logrotate file is required."
  [[ "$LOGROTATE_FILE" = /etc/logrotate.d/* ]] || fail "Logrotate file must be under /etc/logrotate.d."
  [[ "$(basename "$LOGROTATE_FILE")" =~ ^[A-Za-z0-9._-]+$ ]] || fail "Invalid logrotate file name."
}

write_logrotate_config() {
  install -d -o "$RUN_USER" -g "$RUN_USER" -m 750 "$JOB_LOG_DIR"

  local candidate_file
  candidate_file="$(mktemp "/etc/logrotate.d/.oci-migrator-job-logs.XXXXXX")"

  {
    printf '%s/*.log {\n' "$JOB_LOG_DIR"
    printf '    daily\n'
    printf '    rotate %s\n' "$RETENTION_DAYS"
    printf '    maxsize %s\n' "$MAX_SIZE"
    printf '    compress\n'
    printf '    delaycompress\n'
    printf '    missingok\n'
    printf '    notifempty\n'
    printf '    copytruncate\n'
    printf '    su %s %s\n' "$RUN_USER" "$RUN_USER"
    printf '    create 0640 %s %s\n' "$RUN_USER" "$RUN_USER"
    printf '}\n'
  } > "$candidate_file"

  chown root:root "$candidate_file"
  chmod 644 "$candidate_file"

  if ! logrotate -d "$candidate_file" >/dev/null; then
    rm -f "$candidate_file"
    fail "Generated logrotate configuration did not validate."
  fi

  mv "$candidate_file" "$LOGROTATE_FILE"
}

print_json() {
  JOB_LOG_DIR="$JOB_LOG_DIR" \
  MAX_SIZE="$MAX_SIZE" \
  RETENTION_DAYS="$RETENTION_DAYS" \
  LOGROTATE_FILE="$LOGROTATE_FILE" \
  python3 - <<'PY'
import json
import os

print(json.dumps({
    "message": "Job log rotation updated",
    "job_log_dir": os.environ["JOB_LOG_DIR"],
    "max_size": os.environ["MAX_SIZE"],
    "retention_days": int(os.environ["RETENTION_DAYS"]),
    "logrotate_file": os.environ["LOGROTATE_FILE"],
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
      --job-log-dir)
        JOB_LOG_DIR="$2"
        shift 2
        ;;
      --max-size)
        MAX_SIZE="$2"
        shift 2
        ;;
      --retention-days)
        RETENTION_DAYS="$2"
        shift 2
        ;;
      --run-user)
        RUN_USER="$2"
        shift 2
        ;;
      --logrotate-file)
        LOGROTATE_FILE="$2"
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
  validate_job_log_dir
  validate_max_size
  validate_retention_days
  validate_run_user
  validate_logrotate_file
  write_logrotate_config
  print_json
}

main "$@"
