#!/usr/bin/env bash

set -euo pipefail

APP_NAME="${APP_NAME:-oci-migrator}"
NETPLAN_FILE="${OCI_MIGRATOR_NETPLAN_FILE:-/etc/netplan/99-oci-migrator-network.yaml}"
STATE_DIR="${OCI_MIGRATOR_NETWORK_STATE_DIR:-/var/lib/oci-migrator/network}"
PENDING_FILE="$STATE_DIR/pending.json"
CURRENT_FILE="$STATE_DIR/current.json"
BACKUP_NETPLAN="$STATE_DIR/netplan.previous"
BACKUP_CURRENT="$STATE_DIR/current.previous"
NETPLAN_ABSENT="$STATE_DIR/netplan.was-absent"
CURRENT_ABSENT="$STATE_DIR/current.was-absent"
ROLLBACK_TIMER="oci-migrator-network-rollback.timer"
APPLY_UNIT="oci-migrator-network-apply"
ROLLBACK_SECONDS="${OCI_MIGRATOR_NETWORK_ROLLBACK_SECONDS:-180}"

fail() {
  printf '[%s network] ERROR: %s\n' "$APP_NAME" "$*" >&2
  exit 1
}

usage() {
  cat <<EOF
OCI Migrator network settings helper

Usage:
  oci-migrator-network status
  oci-migrator-network stage --mode dhcp --interface IFACE
  oci-migrator-network stage --mode static --interface IFACE --address IPv4/PREFIX --gateway IPv4 --dns "IPv4 IPv4"
  oci-migrator-network apply-pending
  oci-migrator-network confirm
  oci-migrator-network rollback
EOF
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail "This helper must run as root."
}

require_commands() {
  command -v ip >/dev/null 2>&1 || fail "iproute2 is required."
  command -v netplan >/dev/null 2>&1 || fail "Netplan is required."
  command -v systemctl >/dev/null 2>&1 || fail "systemd is required."
  command -v python3 >/dev/null 2>&1 || fail "Python 3 is required."
}

ensure_state_dir() {
  install -d -o root -g root -m 700 "$STATE_DIR"
}

validate_interface() {
  local interface="$1"
  [ -n "$interface" ] || fail "Network interface is required."
  [[ "$interface" =~ ^[A-Za-z0-9_.:-]+$ ]] || fail "Invalid network interface name."
  [ "$interface" != "lo" ] || fail "The loopback interface cannot be managed."
  [ -d "/sys/class/net/$interface" ] || fail "Network interface does not exist: $interface"
}

validate_static_values() {
  local address="$1" gateway="$2" dns_servers="$3"
  ADDRESS="$address" GATEWAY="$gateway" DNS_SERVERS="$dns_servers" python3 - <<'PY'
import ipaddress
import os
import sys

try:
    interface = ipaddress.IPv4Interface(os.environ["ADDRESS"])
    gateway = ipaddress.IPv4Address(os.environ["GATEWAY"])
    dns_servers = [item for item in os.environ["DNS_SERVERS"].replace(",", " ").split() if item]
    if not dns_servers:
        raise ValueError("At least one IPv4 DNS server is required.")
    for server in dns_servers:
        ipaddress.IPv4Address(server)
    if gateway == interface.ip:
        raise ValueError("Gateway cannot be the same as the static IPv4 address.")
except ValueError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1)
PY
}

backup_managed_files() {
  rm -f "$BACKUP_NETPLAN" "$BACKUP_CURRENT" "$NETPLAN_ABSENT" "$CURRENT_ABSENT"
  if [ -f "$NETPLAN_FILE" ]; then
    cp -a "$NETPLAN_FILE" "$BACKUP_NETPLAN"
  else
    touch "$NETPLAN_ABSENT"
  fi
  if [ -f "$CURRENT_FILE" ]; then
    cp -a "$CURRENT_FILE" "$BACKUP_CURRENT"
  else
    touch "$CURRENT_ABSENT"
  fi
}

restore_managed_files() {
  if [ -f "$NETPLAN_ABSENT" ]; then
    rm -f "$NETPLAN_FILE"
  elif [ -f "$BACKUP_NETPLAN" ]; then
    install -o root -g root -m 600 "$BACKUP_NETPLAN" "$NETPLAN_FILE"
  fi

  if [ -f "$CURRENT_ABSENT" ]; then
    rm -f "$CURRENT_FILE"
  elif [ -f "$BACKUP_CURRENT" ]; then
    install -o root -g root -m 600 "$BACKUP_CURRENT" "$CURRENT_FILE"
  fi
}

clear_pending_files() {
  rm -f "$PENDING_FILE" "$BACKUP_NETPLAN" "$BACKUP_CURRENT" "$NETPLAN_ABSENT" "$CURRENT_ABSENT"
}

stop_rollback_timer() {
  systemctl disable --now "$ROLLBACK_TIMER" >/dev/null 2>&1 || true
  systemctl reset-failed "${ROLLBACK_TIMER%.timer}.service" >/dev/null 2>&1 || true
}

write_candidate() {
  local mode="$1" interface="$2" address="$3" gateway="$4" dns_servers="$5"
  local candidate
  candidate="$(mktemp "$(dirname "$NETPLAN_FILE")/.oci-migrator-network.XXXXXX")"

  if [ "$mode" = "dhcp" ]; then
    {
      printf '# Managed by OCI Migrator Pro.\n'
      printf 'network:\n'
      printf '  version: 2\n'
      printf '  ethernets:\n'
      printf '    %s:\n' "$interface"
      printf '      dhcp4: true\n'
      printf '      optional: true\n'
    } > "$candidate"
  else
    local dns_yaml="" server separator=""
    for server in $dns_servers; do
      dns_yaml+="${separator}${server}"
      separator=", "
    done
    {
      printf '# Managed by OCI Migrator Pro.\n'
      printf 'network:\n'
      printf '  version: 2\n'
      printf '  ethernets:\n'
      printf '    %s:\n' "$interface"
      printf '      dhcp4: false\n'
      printf '      addresses: [%s]\n' "$address"
      printf '      routes:\n'
      printf '        - to: default\n'
      printf '          via: %s\n' "$gateway"
      printf '          on-link: true\n'
      printf '      nameservers:\n'
      printf '        addresses: [%s]\n' "$dns_yaml"
      printf '      optional: true\n'
    } > "$candidate"
  fi

  chown root:root "$candidate"
  chmod 600 "$candidate"
  mv "$candidate" "$NETPLAN_FILE"
}

write_pending_state() {
  local mode="$1" interface="$2" address="$3" gateway="$4" dns_servers="$5"
  MODE="$mode" INTERFACE="$interface" ADDRESS="$address" GATEWAY="$gateway" \
    DNS_SERVERS="$dns_servers" ROLLBACK_SECONDS="$ROLLBACK_SECONDS" PENDING_FILE="$PENDING_FILE" python3 - <<'PY'
import json
import os
import secrets
import time

now = time.time()
state = {
    "token": secrets.token_urlsafe(32),
    "mode": os.environ["MODE"],
    "interface": os.environ["INTERFACE"],
    "address": os.environ["ADDRESS"],
    "gateway": os.environ["GATEWAY"],
    "dns_servers": os.environ["DNS_SERVERS"].split(),
    "created_at": now,
    "rollback_at": now + int(os.environ["ROLLBACK_SECONDS"]),
}
with open(os.environ["PENDING_FILE"], "w", encoding="utf-8") as handle:
    json.dump(state, handle)
    handle.write("\n")
os.chmod(os.environ["PENDING_FILE"], 0o600)
PY
}

schedule_apply_and_rollback() {
  systemctl enable --now "$ROLLBACK_TIMER" >/dev/null
  systemctl reset-failed "$APPLY_UNIT.service" >/dev/null 2>&1 || true
  systemd-run --quiet --collect --unit="$APPLY_UNIT" --on-active=3s --timer-property=AccuracySec=1s \
    "$(readlink -f "$0")" apply-pending
}

pending_target_active() {
  PENDING_FILE="$PENDING_FILE" python3 - <<'PY'
import json
import subprocess
import sys
import time

with open(__import__("os").environ["PENDING_FILE"], "r", encoding="utf-8") as handle:
    pending = json.load(handle)
if time.time() - float(pending.get("created_at", 0)) < 5:
    raise SystemExit(1)
try:
    raw = subprocess.check_output(["ip", "-j", "address", "show", "dev", pending["interface"]], text=True)
    links = json.loads(raw)
except Exception:
    raise SystemExit(1)
ipv4 = [item for link in links for item in link.get("addr_info", []) if item.get("family") == "inet"]
if pending["mode"] == "static":
    target_ip, target_prefix = pending["address"].split("/", 1)
    active = any(item.get("local") == target_ip and str(item.get("prefixlen")) == target_prefix for item in ipv4)
else:
    active = any(bool(item.get("dynamic")) or "dynamic" in item.get("flags", []) for item in ipv4)
raise SystemExit(0 if active else 1)
PY
}

stage_change() {
  local mode="$1" interface="$2" address="$3" gateway="$4" dns_servers="$5"
  [ "$mode" = "dhcp" ] || [ "$mode" = "static" ] || fail "Mode must be dhcp or static."
  validate_interface "$interface"
  if [ "$mode" = "static" ]; then
    validate_static_values "$address" "$gateway" "$dns_servers"
    dns_servers="$(printf '%s' "$dns_servers" | tr ',' ' ' | awk '{$1=$1; print}')"
  else
    address=""
    gateway=""
    dns_servers=""
  fi

  [ ! -f "$PENDING_FILE" ] || fail "A network change is already pending confirmation."
  backup_managed_files
  write_candidate "$mode" "$interface" "$address" "$gateway" "$dns_servers"
  if ! netplan generate; then
    restore_managed_files
    clear_pending_files
    fail "Netplan rejected the generated configuration."
  fi

  write_pending_state "$mode" "$interface" "$address" "$gateway" "$dns_servers"
  if ! schedule_apply_and_rollback; then
    stop_rollback_timer
    restore_managed_files
    netplan generate >/dev/null 2>&1 || true
    clear_pending_files
    fail "Unable to schedule the network change and automatic rollback."
  fi
  print_status
}

apply_pending() {
  [ -f "$PENDING_FILE" ] || exit 0
  netplan apply
}

confirm_change() {
  [ -f "$PENDING_FILE" ] || fail "There is no pending network change."
  pending_target_active || fail "The requested network configuration is not active yet."
  PENDING_FILE="$PENDING_FILE" CURRENT_FILE="$CURRENT_FILE" python3 - <<'PY'
import json
import os

with open(os.environ["PENDING_FILE"], "r", encoding="utf-8") as handle:
    pending = json.load(handle)
current = {key: pending.get(key) for key in ("mode", "interface", "address", "gateway", "dns_servers")}
with open(os.environ["CURRENT_FILE"], "w", encoding="utf-8") as handle:
    json.dump(current, handle)
    handle.write("\n")
os.chmod(os.environ["CURRENT_FILE"], 0o600)
PY
  stop_rollback_timer
  clear_pending_files
  print_status
}

rollback_change() {
  if [ ! -f "$PENDING_FILE" ]; then
    stop_rollback_timer
    print_status
    return
  fi
  restore_managed_files
  netplan generate
  netplan apply
  stop_rollback_timer
  clear_pending_files
  print_status
}

print_status() {
  NETPLAN_FILE="$NETPLAN_FILE" STATE_DIR="$STATE_DIR" PENDING_FILE="$PENDING_FILE" \
    CURRENT_FILE="$CURRENT_FILE" python3 - <<'PY'
import json
import os
import subprocess
import time

def command_json(command):
    try:
        return json.loads(subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL))
    except Exception:
        return []

links = command_json(["ip", "-j", "link", "show"])
addresses = command_json(["ip", "-j", "address", "show"])
routes = command_json(["ip", "-j", "route", "show", "default"])
address_by_name = {item.get("ifname"): item for item in addresses}
gateway_by_name = {}
for route in routes:
    gateway_by_name.setdefault(route.get("dev", ""), route.get("gateway", ""))

def dns_for_interface(name):
    try:
        output = subprocess.check_output(["resolvectl", "dns", name], text=True, stderr=subprocess.DEVNULL)
        values = output.split(":", 1)[1].split() if ":" in output else []
        return [value for value in values if value and value != "none"]
    except Exception:
        return []

interfaces = []
for link in links:
    name = link.get("ifname", "")
    if not name or name == "lo":
        continue
    addr_info = address_by_name.get(name, {}).get("addr_info", [])
    ipv4 = [item for item in addr_info if item.get("family") == "inet"]
    interfaces.append({
        "name": name,
        "state": str(link.get("operstate", "unknown")).lower(),
        "mac_address": link.get("address", ""),
        "ipv4_addresses": [f"{item.get('local')}/{item.get('prefixlen')}" for item in ipv4],
        "gateway": gateway_by_name.get(name, ""),
        "dns_servers": dns_for_interface(name),
        "dynamic": any(bool(item.get("dynamic")) or "dynamic" in item.get("flags", []) for item in ipv4),
        "default_route": name in gateway_by_name,
    })

primary = next((item for item in interfaces if item["default_route"]), interfaces[0] if interfaces else None)
configured = {}
if os.path.isfile(os.environ["CURRENT_FILE"]):
    try:
        with open(os.environ["CURRENT_FILE"], "r", encoding="utf-8") as handle:
            configured = json.load(handle)
    except Exception:
        configured = {}

if configured:
    mode = configured.get("mode", "dhcp")
    interface = configured.get("interface", primary["name"] if primary else "")
    address = configured.get("address", "")
    gateway = configured.get("gateway", "")
    dns_servers = configured.get("dns_servers", [])
else:
    mode = "dhcp" if not primary or primary.get("dynamic", False) else "static"
    interface = primary["name"] if primary else ""
    address = primary["ipv4_addresses"][0] if primary and primary["ipv4_addresses"] else ""
    gateway = primary.get("gateway", "") if primary else ""
    dns_servers = []

pending = None
if os.path.isfile(os.environ["PENDING_FILE"]):
    try:
        with open(os.environ["PENDING_FILE"], "r", encoding="utf-8") as handle:
            raw_pending = json.load(handle)
        pending_interface = next((item for item in interfaces if item["name"] == raw_pending.get("interface")), None)
        target_active = False
        if pending_interface:
            if raw_pending.get("mode") == "static":
                target_active = raw_pending.get("address") in pending_interface.get("ipv4_addresses", [])
            else:
                target_active = bool(pending_interface.get("dynamic"))
        pending = {key: value for key, value in raw_pending.items() if key != "token"}
        pending["seconds_remaining"] = max(0, int(float(raw_pending.get("rollback_at", 0)) - time.time()))
        pending["target_active"] = target_active
    except Exception:
        pending = {"status": "invalid"}

print(json.dumps({
    "supported": bool(interfaces),
    "managed": os.path.isfile(os.environ["NETPLAN_FILE"]),
    "netplan_file": os.environ["NETPLAN_FILE"],
    "mode": mode,
    "interface": interface,
    "address": address,
    "gateway": gateway,
    "dns_servers": dns_servers,
    "interfaces": interfaces,
    "pending": pending,
}))
PY
}

parse_stage_args() {
  MODE=""
  INTERFACE=""
  ADDRESS=""
  GATEWAY=""
  DNS_SERVERS=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --mode) MODE="${2:-}"; shift 2 ;;
      --interface) INTERFACE="${2:-}"; shift 2 ;;
      --address) ADDRESS="${2:-}"; shift 2 ;;
      --gateway) GATEWAY="${2:-}"; shift 2 ;;
      --dns) DNS_SERVERS="${2:-}"; shift 2 ;;
      *) fail "Unknown option: $1" ;;
    esac
  done
}

main() {
  [ "$#" -gt 0 ] || { usage; exit 1; }
  local action="$1"
  shift
  require_root
  require_commands
  ensure_state_dir

  case "$action" in
    status)
      [ "$#" -eq 0 ] || fail "status does not accept arguments."
      print_status
      ;;
    stage)
      parse_stage_args "$@"
      stage_change "$MODE" "$INTERFACE" "$ADDRESS" "$GATEWAY" "$DNS_SERVERS"
      ;;
    apply-pending)
      [ "$#" -eq 0 ] || fail "apply-pending does not accept arguments."
      apply_pending
      ;;
    confirm)
      [ "$#" -eq 0 ] || fail "confirm does not accept arguments."
      confirm_change
      ;;
    rollback)
      [ "$#" -eq 0 ] || fail "rollback does not accept arguments."
      rollback_change
      ;;
    -h|--help)
      usage
      ;;
    *)
      fail "Unknown action: $action"
      ;;
  esac
}

main "$@"
