#!/usr/bin/env bash

set -euo pipefail

APP_NAME="${APP_NAME:-oci-migrator}"
CONFIG_FILE="${OCI_MIGRATOR_TLS_CONFIG:-/etc/oci-migrator/tls.conf}"

fail() {
  printf '[%s tls] ERROR: %s\n' "$APP_NAME" "$*" >&2
  exit 1
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail "This helper must run as root."
}

load_config() {
  [ -f "$CONFIG_FILE" ] || fail "Missing TLS helper config: $CONFIG_FILE"
  # shellcheck source=/etc/oci-migrator/tls.conf
  . "$CONFIG_FILE"
  : "${ENV_FILE:?Missing ENV_FILE}"
  : "${API_PORT:?Missing API_PORT}"
  : "${SERVICE_PREFIX:?Missing SERVICE_PREFIX}"
  : "${CADDYFILE:?Missing CADDYFILE}"
  : "${TLS_STATE_DIR:?Missing TLS_STATE_DIR}"
  : "${TLS_SERVICE:?Missing TLS_SERVICE}"
}

validate_config() {
  [[ "$ENV_FILE" = /* ]] || fail "ENV_FILE must be absolute."
  [[ "$CADDYFILE" = /* ]] || fail "CADDYFILE must be absolute."
  [[ "$TLS_STATE_DIR" = /* ]] || fail "TLS_STATE_DIR must be absolute."
  [[ "$TLS_SERVICE" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] || fail "Invalid TLS service name."
  [[ "$API_PORT" =~ ^[0-9]+$ ]] && [ "$API_PORT" -ge 1 ] && [ "$API_PORT" -le 65535 ] || fail "Invalid API port."
}

env_value() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); value=$0} END {print value}' "$ENV_FILE"
}

set_env_values() {
  MODE="$1" HOSTNAME_VALUE="$2" EMAIL_VALUE="$3" CERT_SOURCE_VALUE="$4" HTTP_ACKNOWLEDGED_VALUE="$5" \
    CURRENT_ORIGINS_VALUE="$(env_value OCI_MIGRATOR_ALLOWED_ORIGINS)" \
    ENV_FILE_VALUE="$ENV_FILE" API_PORT_VALUE="$API_PORT" python3 - <<'PY'
import os
import tempfile

path = os.environ["ENV_FILE_VALUE"]
mode = os.environ["MODE"]
hostname = os.environ["HOSTNAME_VALUE"]
email = os.environ["EMAIL_VALUE"]
cert_source = os.environ["CERT_SOURCE_VALUE"]
http_acknowledged = os.environ["HTTP_ACKNOWLEDGED_VALUE"]
current_origins = os.environ["CURRENT_ORIGINS_VALUE"]
api_port = os.environ["API_PORT_VALUE"]

updates = {
    "OCI_MIGRATOR_TLS_MODE": mode,
    "OCI_MIGRATOR_TLS_HOSTNAME": hostname,
    "OCI_MIGRATOR_TLS_EMAIL": email,
    "OCI_MIGRATOR_TLS_CERT_SOURCE": cert_source,
    "OCI_MIGRATOR_TLS_HTTP_ACKNOWLEDGED": http_acknowledged,
}

origins = [
    f"http://localhost:{api_port}",
    f"http://127.0.0.1:{api_port}",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
if mode == "http" and hostname:
    origins.append(f"http://{hostname}:{api_port}")
elif hostname:
    origins.append(f"https://{hostname}")
if mode == "http":
    origins.extend(origin.strip() for origin in current_origins.split(",") if origin.strip())
updates["OCI_MIGRATOR_ALLOWED_ORIGINS"] = ",".join(dict.fromkeys(origins))

lines = []
if os.path.exists(path):
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()

remaining = dict(updates)
output = []
for raw in lines:
    line = raw.rstrip("\n")
    if not line or line.lstrip().startswith("#") or "=" not in line:
        output.append(raw if raw.endswith("\n") else raw + "\n")
        continue
    key = line.split("=", 1)[0]
    if key in remaining:
        output.append(f"{key}={remaining.pop(key)}\n")
    else:
        output.append(raw if raw.endswith("\n") else raw + "\n")
for key, value in remaining.items():
    output.append(f"{key}={value}\n")

directory = os.path.dirname(path)
os.makedirs(directory, exist_ok=True)
fd, temporary = tempfile.mkstemp(dir=directory)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.writelines(output)
    os.chmod(temporary, 0o600)
    if os.path.exists(path):
        stat = os.stat(path)
        os.chown(temporary, stat.st_uid, stat.st_gid)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.remove(temporary)
PY
}

validate_hostname() {
  local hostname="$1"
  [ -n "$hostname" ] || fail "Hostname is required."
  [ "${#hostname}" -le 253 ] || fail "Hostname is too long."
  [[ "$hostname" == *.* ]] || fail "Enter a fully qualified DNS hostname, for example migrator.example.com."
  local label
  IFS='.' read -r -a labels <<< "$hostname"
  for label in "${labels[@]}"; do
    [ "${#label}" -le 63 ] || fail "A DNS hostname label is too long."
    [[ "$label" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] || fail "Enter a valid DNS hostname without scheme, path, or port."
  done
}

validate_email() {
  local email="$1"
  [ -z "$email" ] || [[ "$email" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || fail "Invalid ACME email address."
}

validate_custom_certificate() {
  local hostname="$1" cert_path="$2" key_path="$3"
  [ -f "$cert_path" ] || fail "Certificate file not found: $cert_path"
  [ -f "$key_path" ] || fail "Private key file not found: $key_path"
  openssl x509 -in "$cert_path" -noout >/dev/null 2>&1 || fail "Certificate must be PEM encoded."
  openssl pkey -in "$key_path" -passin pass: -noout >/dev/null 2>&1 || fail "Private key must be an unencrypted PEM key."
  openssl x509 -in "$cert_path" -checkend 86400 -noout >/dev/null 2>&1 || fail "Certificate is expired or expires within 24 hours."
  openssl x509 -in "$cert_path" -checkhost "$hostname" -noout >/dev/null 2>&1 || fail "Certificate does not cover $hostname."

  local cert_key_hash private_key_hash
  cert_key_hash="$(openssl x509 -in "$cert_path" -pubkey -noout | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | awk '{print $1}')"
  private_key_hash="$(openssl pkey -in "$key_path" -passin pass: -pubout -outform DER 2>/dev/null | sha256sum | awk '{print $1}')"
  [ -n "$cert_key_hash" ] && [ "$cert_key_hash" = "$private_key_hash" ] || fail "Certificate and private key do not match."
}

prepare_directories() {
  install -d -o root -g root -m 755 "$(dirname "$CADDYFILE")"
  install -d -o root -g root -m 700 "$TLS_STATE_DIR"
  install -d -o caddy -g caddy -m 750 "$TLS_STATE_DIR/data" "$TLS_STATE_DIR/config" "$TLS_STATE_DIR/logs"
}

write_caddyfile() {
  local mode="$1" hostname="$2" email="$3"
  local candidate
  candidate="$(mktemp "$(dirname "$CADDYFILE")/.Caddyfile.XXXXXX")"
  {
    printf '{\n'
    printf '\tadmin off\n'
    if [ "$mode" = "letsencrypt" ] && [ -n "$email" ]; then
      printf '\temail %s\n' "$email"
    fi
    printf '}\n\n'
    printf '%s {\n' "$hostname"
    if [ "$mode" = "custom" ]; then
      printf '\ttls %s %s\n' "$TLS_STATE_DIR/certificate.pem" "$TLS_STATE_DIR/private-key.pem"
    fi
    printf '\tencode zstd gzip\n'
    printf '\theader {\n'
    printf '\t\tStrict-Transport-Security "max-age=31536000"\n'
    printf '\t\tX-Content-Type-Options "nosniff"\n'
    printf '\t\tReferrer-Policy "same-origin"\n'
    printf '\t}\n'
    printf '\treverse_proxy 127.0.0.1:%s\n' "$API_PORT"
    printf '\tlog {\n'
    printf '\t\toutput file %s/access.log {\n' "$TLS_STATE_DIR/logs"
    printf '\t\t\troll_size 10MiB\n'
    printf '\t\t\troll_keep 5\n'
    printf '\t\t}\n'
    printf '\t}\n'
    printf '}\n'
  } > "$candidate"
  chown root:caddy "$candidate"
  chmod 640 "$candidate"
  caddy validate --config "$candidate" --adapter caddyfile >/dev/null || {
    rm -f "$candidate"
    return 1
  }
  mv "$candidate" "$CADDYFILE"
}

open_https_firewall() {
  local port
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q 'Status: active'; then
    for port in 80 443; do
      ufw allow "$port/tcp" >/dev/null
    done
  elif command -v iptables >/dev/null 2>&1; then
    for port in 80 443; do
      iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport "$port" -j ACCEPT
    done
    command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save >/dev/null 2>&1 || true
  fi
}

service_state() {
  local state
  state="$(systemctl is-active "$TLS_SERVICE" 2>/dev/null || true)"
  printf '%s' "${state:-inactive}"
}

service_enabled_state() {
  local state
  state="$(systemctl is-enabled "$TLS_SERVICE" 2>/dev/null || true)"
  printf '%s' "${state:-disabled}"
}

backup_managed_tls() {
  local backup_dir="$1" file base
  install -d -o root -g root -m 700 "$backup_dir"
  for file in "$CADDYFILE" "$TLS_STATE_DIR/certificate.pem" "$TLS_STATE_DIR/private-key.pem"; do
    base="$(basename "$file")"
    if [ -f "$file" ]; then
      cp -a "$file" "$backup_dir/$base"
    else
      touch "$backup_dir/$base.absent"
    fi
  done
}

restore_managed_tls() {
  local backup_dir="$1" file base
  for file in "$CADDYFILE" "$TLS_STATE_DIR/certificate.pem" "$TLS_STATE_DIR/private-key.pem"; do
    base="$(basename "$file")"
    if [ -f "$backup_dir/$base.absent" ]; then
      rm -f "$file"
    elif [ -f "$backup_dir/$base" ]; then
      cp -a "$backup_dir/$base" "$file"
    fi
  done
}

restore_service_state() {
  local active_state="$1" enabled_state="$2"
  if [ "$enabled_state" = "enabled" ]; then
    systemctl enable "$TLS_SERVICE" >/dev/null 2>&1 || true
  else
    systemctl disable "$TLS_SERVICE" >/dev/null 2>&1 || true
  fi

  if [ "$active_state" = "active" ]; then
    systemctl restart "$TLS_SERVICE" >/dev/null 2>&1 || true
  else
    systemctl stop "$TLS_SERVICE" >/dev/null 2>&1 || true
  fi
}

start_caddy_or_rollback() {
  local backup_dir="$1" hostname="$2" previous_active="$3" previous_enabled="$4"
  if ! systemctl enable "$TLS_SERVICE" >/dev/null 2>&1 || ! systemctl restart "$TLS_SERVICE" >/dev/null 2>&1; then
    restore_managed_tls "$backup_dir"
    restore_service_state "$previous_active" "$previous_enabled"
    fail "Caddy TLS service did not start. The previous managed TLS configuration was restored."
  fi

  local attempt
  for attempt in $(seq 1 30); do
    if curl --fail --silent --show-error --insecure --max-time 5 \
      --resolve "$hostname:443:127.0.0.1" "https://$hostname/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done

  restore_managed_tls "$backup_dir"
  restore_service_state "$previous_active" "$previous_enabled"
  fail "The HTTPS health check failed. The previous managed TLS configuration was restored. Check DNS, ports 80/443, and the TLS service log."
}

print_status() {
  local mode hostname email cert_source http_acknowledged state caddy_installed=false
  mode="$(env_value OCI_MIGRATOR_TLS_MODE)"
  hostname="$(env_value OCI_MIGRATOR_TLS_HOSTNAME)"
  email="$(env_value OCI_MIGRATOR_TLS_EMAIL)"
  cert_source="$(env_value OCI_MIGRATOR_TLS_CERT_SOURCE)"
  http_acknowledged="$(env_value OCI_MIGRATOR_TLS_HTTP_ACKNOWLEDGED)"
  [ -n "$mode" ] || mode="http"
  command -v caddy >/dev/null 2>&1 && caddy_installed=true
  state="$(service_state)"
  MODE="$mode" HOSTNAME_VALUE="$hostname" EMAIL_VALUE="$email" CERT_SOURCE_VALUE="$cert_source" HTTP_ACKNOWLEDGED_VALUE="$http_acknowledged" \
    SERVICE_STATE="$state" CADDY_INSTALLED="$caddy_installed" TLS_SERVICE_VALUE="$TLS_SERVICE" python3 - <<'PY'
import json
import os

mode = os.environ["MODE"]
hostname = os.environ["HOSTNAME_VALUE"]
state = os.environ["SERVICE_STATE"]
caddy_installed = os.environ["CADDY_INSTALLED"] == "true"
https_url = f"https://{hostname}" if hostname and mode != "http" else ""
secure = mode in {"external", "letsencrypt", "custom"}
http_acknowledged = os.environ["HTTP_ACKNOWLEDGED_VALUE"].strip().lower() == "true"
service_required = mode in {"letsencrypt", "custom"}
healthy = secure and (not service_required or state == "active")

messages = {
    "http": (
        "HTTP risk has been acknowledged by an administrator. Traffic is not encrypted."
        if http_acknowledged
        else "HTTPS is not configured. Acknowledge HTTP setup mode or configure HTTPS."
    ),
    "external": "HTTPS is terminated by an external load balancer or reverse proxy.",
    "letsencrypt": "Caddy manages and renews the Let's Encrypt certificate automatically.",
    "custom": "Caddy serves the installed corporate certificate.",
}
print(json.dumps({
    "supported": True,
    "helper_installed": True,
    "caddy_installed": caddy_installed,
    "mode": mode,
    "hostname": hostname,
    "email": os.environ["EMAIL_VALUE"],
    "certificate_source": os.environ["CERT_SOURCE_VALUE"],
    "service": os.environ["TLS_SERVICE_VALUE"],
    "service_state": state,
    "https_url": https_url,
    "secure": secure,
    "http_acknowledged": http_acknowledged,
    "status": "ok" if healthy or (mode == "http" and http_acknowledged) else ("warn" if mode == "http" else "error"),
    "message": messages.get(mode, "Unknown TLS mode."),
}))
PY
}

apply_mode() {
  local mode="$1" hostname="$2" email="$3" cert_path="$4" key_path="$5" acknowledge_http="${6:-false}"
  case "$mode" in
    http)
      [ "$acknowledge_http" = "true" ] || fail "Confirm that you understand HTTP traffic is not encrypted."
      systemctl disable --now "$TLS_SERVICE" >/dev/null 2>&1 || true
      set_env_values "$mode" "$hostname" "" "" "true"
      ;;
    external)
      validate_hostname "$hostname"
      systemctl disable --now "$TLS_SERVICE" >/dev/null 2>&1 || true
      set_env_values "$mode" "$hostname" "" "external" "false"
      ;;
    letsencrypt)
      command -v caddy >/dev/null 2>&1 || fail "Caddy is not installed. Rerun install.sh on the server."
      validate_hostname "$hostname"
      validate_email "$email"
      prepare_directories
      local letsencrypt_backup letsencrypt_previous_active letsencrypt_previous_enabled
      letsencrypt_backup="$(mktemp -d)"
      letsencrypt_previous_active="$(service_state)"
      letsencrypt_previous_enabled="$(service_enabled_state)"
      backup_managed_tls "$letsencrypt_backup"
      if ! write_caddyfile "$mode" "$hostname" "$email"; then
        restore_managed_tls "$letsencrypt_backup"
        rm -rf "$letsencrypt_backup"
        fail "Caddy rejected the generated TLS configuration."
      fi
      start_caddy_or_rollback "$letsencrypt_backup" "$hostname" "$letsencrypt_previous_active" "$letsencrypt_previous_enabled"
      rm -rf "$letsencrypt_backup"
      set_env_values "$mode" "$hostname" "$email" "letsencrypt" "false"
      open_https_firewall
      ;;
    custom)
      command -v caddy >/dev/null 2>&1 || fail "Caddy is not installed. Rerun install.sh on the server."
      validate_hostname "$hostname"
      validate_custom_certificate "$hostname" "$cert_path" "$key_path"
      prepare_directories
      local custom_backup custom_previous_active custom_previous_enabled
      custom_backup="$(mktemp -d)"
      custom_previous_active="$(service_state)"
      custom_previous_enabled="$(service_enabled_state)"
      backup_managed_tls "$custom_backup"
      install -o root -g caddy -m 640 "$cert_path" "$TLS_STATE_DIR/certificate.pem"
      install -o root -g caddy -m 640 "$key_path" "$TLS_STATE_DIR/private-key.pem"
      if ! write_caddyfile "$mode" "$hostname" ""; then
        restore_managed_tls "$custom_backup"
        rm -rf "$custom_backup"
        fail "Caddy rejected the generated TLS configuration."
      fi
      start_caddy_or_rollback "$custom_backup" "$hostname" "$custom_previous_active" "$custom_previous_enabled"
      rm -rf "$custom_backup"
      set_env_values "$mode" "$hostname" "" "uploaded" "false"
      open_https_firewall
      ;;
    *)
      fail "Mode must be http, external, letsencrypt, or custom."
      ;;
  esac
  print_status
}

main() {
  require_root
  load_config
  validate_config

  local command="${1:-status}"
  shift || true
  case "$command" in
    status)
      print_status
      ;;
    apply)
      local mode="" hostname="" email="" cert_path="" key_path="" acknowledge_http="false"
      while [ "$#" -gt 0 ]; do
        case "$1" in
          --mode) mode="${2:-}"; shift 2 ;;
          --hostname) hostname="${2:-}"; shift 2 ;;
          --email) email="${2:-}"; shift 2 ;;
          --cert-path) cert_path="${2:-}"; shift 2 ;;
          --key-path) key_path="${2:-}"; shift 2 ;;
          --acknowledge-http) acknowledge_http="${2:-}"; shift 2 ;;
          *) fail "Unknown option: $1" ;;
        esac
      done
      case "$acknowledge_http" in
        true|false) ;;
        *) fail "--acknowledge-http must be true or false." ;;
      esac
      apply_mode "$mode" "$hostname" "$email" "$cert_path" "$key_path" "$acknowledge_http"
      ;;
    *)
      fail "Usage: $0 status | apply --mode MODE [--hostname HOST] [--email EMAIL] [--cert-path PATH --key-path PATH] [--acknowledge-http true|false]"
      ;;
  esac
}

main "$@"
