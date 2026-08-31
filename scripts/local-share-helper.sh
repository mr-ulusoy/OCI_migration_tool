#!/usr/bin/env bash

set -euo pipefail

APP_NAME="${APP_NAME:-oci-migrator}"
CONFIG_FILE="/etc/oci-migrator/local-share.conf"
SMB_CONF="/etc/samba/smb.conf"
SMB_PORT="445"
NFS_EXPORTS_FILE="/etc/exports.d/oci-migrator.exports"
NFS_PORT="2049"
SHARE_ALLOW_CIDR=""

ACTION=""
SHARE_NAME=""
SHARE_PATH=""
ACCESS_MODE=""
SHARE_USER=""
PASSWORD_FILE=""
NFS_CLIENTS=""

log() {
  printf '[%s local-share] %s\n' "$APP_NAME" "$*" >&2
}

fail() {
  printf '[%s local-share] ERROR: %s\n' "$APP_NAME" "$*" >&2
  exit 1
}

usage() {
  cat <<EOF
OCI Migrator local SMB/NFS share helper

Usage:
  oci-migrator-local-share enable --share-name NAME --path PATH --access everyone|user [--user USER --password-file PATH]
  oci-migrator-local-share disable --share-name NAME
  oci-migrator-local-share enable-nfs --share-name NAME --path PATH --clients CLIENT[,CLIENT...]
  oci-migrator-local-share disable-nfs --share-name NAME
EOF
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail "This helper must run as root."
}

validate_config_file() {
  [ -f "$CONFIG_FILE" ] || fail "Missing $CONFIG_FILE. Rerun install.sh."

  local owner mode
  owner="$(stat -c '%u' "$CONFIG_FILE")"
  mode="$(stat -c '%a' "$CONFIG_FILE")"
  [ "$owner" = "0" ] || fail "$CONFIG_FILE must be owned by root."

  local group_write other_write
  group_write=$(( (10#$mode / 10) % 10 ))
  other_write=$(( 10#$mode % 10 ))
  [ $(( group_write & 2 )) -eq 0 ] || fail "$CONFIG_FILE must not be group-writable."
  [ $(( other_write & 2 )) -eq 0 ] || fail "$CONFIG_FILE must not be world-writable."
}

load_config() {
  validate_config_file
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"

  [ -n "${LOCAL_DATA_ROOT:-}" ] || fail "LOCAL_DATA_ROOT is missing in $CONFIG_FILE."
  [ -n "${RUN_USER:-}" ] || fail "RUN_USER is missing in $CONFIG_FILE."
  [ -n "${SHARE_ALLOW_CIDR:-}" ] || fail "SHARE_ALLOW_CIDR is missing in $CONFIG_FILE. Rerun install.sh."
  id "$RUN_USER" >/dev/null 2>&1 || fail "Configured RUN_USER does not exist: $RUN_USER"
}

validate_share_name() {
  [ -n "$SHARE_NAME" ] || fail "Share name is required."
  [[ "$SHARE_NAME" =~ ^[A-Za-z0-9._-]{1,80}$ ]] || fail "Share name may only contain letters, numbers, dot, underscore, and dash."

  case "$(printf '%s' "$SHARE_NAME" | tr '[:upper:]' '[:lower:]')" in
    global|homes|printers|print\$)
      fail "Reserved share name: $SHARE_NAME"
      ;;
  esac
}

validate_share_user() {
  [ -n "$SHARE_USER" ] || fail "SMB user is required for user access."
  [[ "$SHARE_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail "SMB user must be lowercase and may contain letters, numbers, underscore, and dash."
  [ "$SHARE_USER" != "root" ] || fail "SMB user cannot be root."
}

validate_share_path() {
  [ -n "$SHARE_PATH" ] || fail "Share path is required."
  [[ "$SHARE_PATH" = /* ]] || fail "Share path must be absolute."

  local resolved_root resolved_path
  resolved_root="$(realpath -m "$LOCAL_DATA_ROOT")"
  resolved_path="$(realpath -m "$SHARE_PATH")"

  case "$resolved_path" in
    "$resolved_root"/*)
      ;;
    *)
      fail "Share path must be inside $resolved_root."
      ;;
  esac

  [ -d "$resolved_path" ] || fail "Share path does not exist: $resolved_path"
  SHARE_PATH="$resolved_path"
}

validate_nfs_clients() {
  [ -n "$NFS_CLIENTS" ] || fail "NFS client allow list is required."

  local raw token normalized
  raw="${NFS_CLIENTS//,/ }"
  normalized=""
  for token in $raw; do
    [[ "$token" =~ ^[A-Za-z0-9_.:/-]{1,128}$ ]] || fail "NFS clients must be hostnames, IP addresses, or CIDR ranges."
    case "$token" in
      *"*"*|*"("*|*")"*|*"["*|*"]"*)
        fail "NFS client allow list may not contain wildcards or export options."
        ;;
    esac
    normalized="${normalized:+$normalized }$token"
  done

  [ -n "$normalized" ] || fail "NFS client allow list is required."
  NFS_CLIENTS="$normalized"
}

install_samba() {
  if command -v smbd >/dev/null 2>&1; then
    return
  fi

  command -v apt-get >/dev/null 2>&1 || fail "smbd is missing and apt-get is not available."
  log "Installing Samba"
  env DEBIAN_FRONTEND=noninteractive apt-get update
  env DEBIAN_FRONTEND=noninteractive apt-get install -y samba
}

install_nfs() {
  if command -v exportfs >/dev/null 2>&1 && command -v rpc.nfsd >/dev/null 2>&1; then
    return
  fi

  command -v apt-get >/dev/null 2>&1 || fail "NFS server is missing and apt-get is not available."
  log "Installing NFS server"
  env DEBIAN_FRONTEND=noninteractive apt-get update
  env DEBIAN_FRONTEND=noninteractive apt-get install -y nfs-kernel-server
}

ensure_samba_user() {
  validate_share_user
  [ -n "$PASSWORD_FILE" ] || fail "Password file is required for user access."
  [ -f "$PASSWORD_FILE" ] || fail "Password file does not exist: $PASSWORD_FILE"

  local password
  password="$(tr -d '\r\n' < "$PASSWORD_FILE")"
  [ "${#password}" -ge 8 ] || fail "SMB password must be at least 8 characters."

  if ! id "$SHARE_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SHARE_USER"
  fi

  printf '%s\n%s\n' "$password" "$password" | smbpasswd -s -a "$SHARE_USER" >/dev/null
  smbpasswd -e "$SHARE_USER" >/dev/null
}

write_samba_config() {
  local backup_file
  backup_file="$(mktemp)"
  cp "$SMB_CONF" "$backup_file"

  SHARE_NAME="$SHARE_NAME" \
  SHARE_PATH="$SHARE_PATH" \
  ACCESS_MODE="$ACCESS_MODE" \
  SHARE_USER="$SHARE_USER" \
  RUN_USER="$RUN_USER" \
  SMB_CONF="$SMB_CONF" \
  python3 - <<'PY'
import os
import shutil
import tempfile
from pathlib import Path

share_name = os.environ["SHARE_NAME"]
share_path = os.environ["SHARE_PATH"]
access_mode = os.environ["ACCESS_MODE"]
share_user = os.environ.get("SHARE_USER", "")
smb_conf = Path(os.environ["SMB_CONF"])

begin = f"# BEGIN OCI Migrator local share {share_name}"
end = f"# END OCI Migrator local share {share_name}"

if smb_conf.exists():
    lines = smb_conf.read_text(encoding="utf-8").splitlines()
else:
    lines = ["[global]", "   server role = standalone server"]

filtered = []
skip = False
for line in lines:
    if line.strip() == begin:
        skip = True
        continue
    if line.strip() == end:
        skip = False
        continue
    if not skip:
        filtered.append(line)

lines = filtered

if access_mode == "everyone":
    global_index = None
    next_section = len(lines)
    for idx, line in enumerate(lines):
        if line.strip().lower() == "[global]":
            global_index = idx
            break

    if global_index is None:
        lines = ["[global]", "   map to guest = Bad User", ""] + lines
    else:
        for idx in range(global_index + 1, len(lines)):
            stripped = lines[idx].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                next_section = idx
                break

        map_index = None
        for idx in range(global_index + 1, next_section):
            if lines[idx].strip().lower().startswith("map to guest"):
                map_index = idx
                break

        if map_index is None:
            lines.insert(global_index + 1, "   map to guest = Bad User")
        else:
            lines[map_index] = "   map to guest = Bad User"

block = [
    "",
    begin,
    f"[{share_name}]",
    f"   path = {share_path}",
    "   browseable = yes",
    "   read only = no",
    "   force user = " + os.environ.get("RUN_USER", ""),
    "   create mask = 0664",
    "   directory mask = 0775",
]

if access_mode == "everyone":
    block.extend([
        "   guest ok = yes",
        "   guest only = yes",
    ])
else:
    block.extend([
        "   guest ok = no",
        f"   valid users = {share_user}",
    ])

block.append(end)
lines.extend(block)

tmp_fd, tmp_name = tempfile.mkstemp(dir=str(smb_conf.parent), text=True)
with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines).rstrip() + "\n")

shutil.move(tmp_name, smb_conf)
PY

  if ! testparm -s "$SMB_CONF" >/dev/null 2>&1; then
    cp "$backup_file" "$SMB_CONF"
    rm -f "$backup_file"
    fail "Generated Samba configuration did not pass testparm."
  fi

  chown root:root "$SMB_CONF"
  chmod 644 "$SMB_CONF"
  rm -f "$backup_file"
}

remove_samba_config() {
  [ -f "$SMB_CONF" ] || return 0

  SHARE_NAME="$SHARE_NAME" SMB_CONF="$SMB_CONF" python3 - <<'PY'
import os
import shutil
import tempfile
from pathlib import Path

share_name = os.environ["SHARE_NAME"]
smb_conf = Path(os.environ["SMB_CONF"])
begin = f"# BEGIN OCI Migrator local share {share_name}"
end = f"# END OCI Migrator local share {share_name}"

lines = smb_conf.read_text(encoding="utf-8").splitlines()
filtered = []
skip = False
for line in lines:
    if line.strip() == begin:
        skip = True
        continue
    if line.strip() == end:
        skip = False
        continue
    if not skip:
        filtered.append(line)

tmp_fd, tmp_name = tempfile.mkstemp(dir=str(smb_conf.parent), text=True)
with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(filtered).rstrip() + "\n")

shutil.move(tmp_name, smb_conf)
PY

  if command -v testparm >/dev/null 2>&1; then
    testparm -s "$SMB_CONF" >/dev/null 2>&1 || fail "Samba configuration is invalid after removing share."
  fi

  chown root:root "$SMB_CONF"
  chmod 644 "$SMB_CONF"
}

write_nfs_exports() {
  local backup_file had_file
  backup_file="$(mktemp)"
  had_file=0
  if [ -f "$NFS_EXPORTS_FILE" ]; then
    cp "$NFS_EXPORTS_FILE" "$backup_file"
    had_file=1
  fi

  install -d -o root -g root -m 755 "$(dirname "$NFS_EXPORTS_FILE")"

  SHARE_NAME="$SHARE_NAME" \
  SHARE_PATH="$SHARE_PATH" \
  NFS_CLIENTS="$NFS_CLIENTS" \
  NFS_EXPORTS_FILE="$NFS_EXPORTS_FILE" \
  python3 - <<'PY'
import os
import shutil
import tempfile
from pathlib import Path

share_name = os.environ["SHARE_NAME"]
share_path = os.environ["SHARE_PATH"]
clients = os.environ["NFS_CLIENTS"].split()
exports_file = Path(os.environ["NFS_EXPORTS_FILE"])

begin = f"# BEGIN OCI Migrator NFS share {share_name}"
end = f"# END OCI Migrator NFS share {share_name}"

if exports_file.exists():
    lines = exports_file.read_text(encoding="utf-8").splitlines()
else:
    lines = []

filtered = []
skip = False
for line in lines:
    if line.strip() == begin:
        skip = True
        continue
    if line.strip() == end:
        skip = False
        continue
    if not skip:
        filtered.append(line)

options = "rw,sync,no_subtree_check,root_squash"
client_specs = " ".join(f"{client}({options})" for client in clients)
block = ["", begin, f"{share_path} {client_specs}", end]
filtered.extend(block)

tmp_fd, tmp_name = tempfile.mkstemp(dir=str(exports_file.parent), text=True)
with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(filtered).rstrip() + "\n")

shutil.move(tmp_name, exports_file)
PY

  if ! exportfs -ra >/dev/null 2>&1; then
    if [ "$had_file" -eq 1 ]; then
      cp "$backup_file" "$NFS_EXPORTS_FILE"
    else
      rm -f "$NFS_EXPORTS_FILE"
    fi
    exportfs -ra >/dev/null 2>&1 || true
    rm -f "$backup_file"
    fail "Generated NFS exports configuration did not pass exportfs."
  fi

  chown root:root "$NFS_EXPORTS_FILE"
  chmod 644 "$NFS_EXPORTS_FILE"
  rm -f "$backup_file"
}

remove_nfs_exports() {
  [ -f "$NFS_EXPORTS_FILE" ] || return 0

  SHARE_NAME="$SHARE_NAME" NFS_EXPORTS_FILE="$NFS_EXPORTS_FILE" python3 - <<'PY'
import os
import shutil
import tempfile
from pathlib import Path

share_name = os.environ["SHARE_NAME"]
exports_file = Path(os.environ["NFS_EXPORTS_FILE"])
begin = f"# BEGIN OCI Migrator NFS share {share_name}"
end = f"# END OCI Migrator NFS share {share_name}"

lines = exports_file.read_text(encoding="utf-8").splitlines()
filtered = []
skip = False
for line in lines:
    if line.strip() == begin:
        skip = True
        continue
    if line.strip() == end:
        skip = False
        continue
    if not skip:
        filtered.append(line)

tmp_fd, tmp_name = tempfile.mkstemp(dir=str(exports_file.parent), text=True)
with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(filtered).rstrip() + "\n")

shutil.move(tmp_name, exports_file)
PY

  exportfs -ra >/dev/null 2>&1 || fail "NFS exports configuration is invalid after removing share."
  chown root:root "$NFS_EXPORTS_FILE"
  chmod 644 "$NFS_EXPORTS_FILE"
}

restart_samba() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now smbd.service
    systemctl restart smbd.service
  else
    service smbd restart
  fi
}

restart_nfs() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now nfs-server.service >/dev/null 2>&1 || systemctl enable --now nfs-kernel-server.service
    systemctl restart nfs-server.service >/dev/null 2>&1 || systemctl restart nfs-kernel-server.service
  else
    service nfs-kernel-server restart || service nfs-server restart
  fi
}

open_smb_firewall() {
  log "Opening inbound SMB TCP $SMB_PORT from $SHARE_ALLOW_CIDR"
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q 'Status: active'; then
    ufw --force delete allow "$SMB_PORT/tcp" >/dev/null 2>&1 || true
    local cidr
    while IFS= read -r cidr; do
      [ -n "$cidr" ] || continue
      ufw allow from "$cidr" to any port "$SMB_PORT" proto tcp
    done < <(printf '%s\n' "$SHARE_ALLOW_CIDR" | tr ',' '\n')
    return
  fi

  if command -v iptables >/dev/null 2>&1; then
    while iptables -C INPUT -p tcp --dport "$SMB_PORT" -j ACCEPT 2>/dev/null; do
      iptables -D INPUT -p tcp --dport "$SMB_PORT" -j ACCEPT
    done
    local cidr
    while IFS= read -r cidr; do
      [ -n "$cidr" ] || continue
      iptables -C INPUT -p tcp -s "$cidr" --dport "$SMB_PORT" -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp -s "$cidr" --dport "$SMB_PORT" -j ACCEPT
    done < <(printf '%s\n' "$SHARE_ALLOW_CIDR" | tr ',' '\n')
    command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save || true
  fi
}

open_nfs_firewall() {
  log "Opening inbound NFSv4 TCP $NFS_PORT from $SHARE_ALLOW_CIDR"
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q 'Status: active'; then
    ufw --force delete allow "$NFS_PORT/tcp" >/dev/null 2>&1 || true
    local cidr
    while IFS= read -r cidr; do
      [ -n "$cidr" ] || continue
      ufw allow from "$cidr" to any port "$NFS_PORT" proto tcp
    done < <(printf '%s\n' "$SHARE_ALLOW_CIDR" | tr ',' '\n')
    return
  fi

  if command -v iptables >/dev/null 2>&1; then
    while iptables -C INPUT -p tcp --dport "$NFS_PORT" -j ACCEPT 2>/dev/null; do
      iptables -D INPUT -p tcp --dport "$NFS_PORT" -j ACCEPT
    done
    local cidr
    while IFS= read -r cidr; do
      [ -n "$cidr" ] || continue
      iptables -C INPUT -p tcp -s "$cidr" --dport "$NFS_PORT" -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp -s "$cidr" --dport "$NFS_PORT" -j ACCEPT
    done < <(printf '%s\n' "$SHARE_ALLOW_CIDR" | tr ',' '\n')
    command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save || true
  fi
}

print_json() {
  local message="$1"
  local port="${2:-445}"
  local protocol="${3:-smb}"
  MESSAGE="$message" \
  SHARE_NAME="$SHARE_NAME" \
  SHARE_PATH="$SHARE_PATH" \
  ACCESS_MODE="$ACCESS_MODE" \
  SHARE_USER="$SHARE_USER" \
  NFS_CLIENTS="$NFS_CLIENTS" \
  PORT="$port" \
  PROTOCOL="$protocol" \
  python3 - <<'PY'
import json
import os

payload = {
    "message": os.environ["MESSAGE"],
    "protocol": os.environ.get("PROTOCOL", ""),
    "share_name": os.environ.get("SHARE_NAME", ""),
    "path": os.environ.get("SHARE_PATH", ""),
    "access": os.environ.get("ACCESS_MODE", ""),
    "user": os.environ.get("SHARE_USER", ""),
    "clients": os.environ.get("NFS_CLIENTS", ""),
    "port": int(os.environ.get("PORT", "445")),
}
print(json.dumps(payload))
PY
}

parse_args() {
  [ "$#" -gt 0 ] || {
    usage
    exit 1
  }

  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
  esac

  ACTION="$1"
  shift

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --share-name)
        SHARE_NAME="$2"
        shift 2
        ;;
      --path)
        SHARE_PATH="$2"
        shift 2
        ;;
      --access)
        ACCESS_MODE="$2"
        shift 2
        ;;
      --user)
        SHARE_USER="$2"
        shift 2
        ;;
      --password-file)
        PASSWORD_FILE="$2"
        shift 2
        ;;
      --clients)
        NFS_CLIENTS="$2"
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

enable_share() {
  validate_share_name
  validate_share_path

  case "$ACCESS_MODE" in
    everyone|user)
      ;;
    *)
      fail "Access mode must be everyone or user."
      ;;
  esac

  install_samba
  install -d -o "$RUN_USER" -g "$RUN_USER" -m 775 "$SHARE_PATH"

  if [ "$ACCESS_MODE" = "user" ]; then
    ensure_samba_user
  fi

  write_samba_config
  restart_samba
  open_smb_firewall
  print_json "SMB share enabled" "$SMB_PORT" "smb"
}

disable_share() {
  validate_share_name
  remove_samba_config
  if command -v smbd >/dev/null 2>&1; then
    restart_samba
  fi
  print_json "SMB share disabled" "$SMB_PORT" "smb"
}

enable_nfs_share() {
  validate_share_name
  validate_share_path
  validate_nfs_clients

  install_nfs
  install -d -o "$RUN_USER" -g "$RUN_USER" -m 775 "$SHARE_PATH"
  write_nfs_exports
  restart_nfs
  open_nfs_firewall
  print_json "NFSv4 share enabled" "$NFS_PORT" "nfs4"
}

disable_nfs_share() {
  validate_share_name
  remove_nfs_exports
  if command -v exportfs >/dev/null 2>&1; then
    exportfs -ra >/dev/null 2>&1 || true
  fi
  print_json "NFSv4 share disabled" "$NFS_PORT" "nfs4"
}

main() {
  parse_args "$@"
  require_root
  load_config

  case "$ACTION" in
    enable)
      enable_share
      ;;
    disable)
      disable_share
      ;;
    enable-nfs)
      enable_nfs_share
      ;;
    disable-nfs)
      disable_nfs_share
      ;;
    *)
      fail "Unknown action: $ACTION"
      ;;
  esac
}

main "$@"
