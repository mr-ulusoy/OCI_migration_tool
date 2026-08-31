import configparser
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import pwd
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from threading import Lock

import oci
import uvicorn
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional
from job_logs import JOB_LOG_DIR, job_log_path, legacy_job_log_path, resolve_readable_log_path, tail_file
from job_store import JOB_HISTORY_FILE, get_job_run, list_job_runs, locked_history_file, upsert_job_run
from notifications import (
    get_notification_settings,
    send_test_notification,
    validate_notification_settings,
)
from worker import migrate_single_vm, rclone_sync_task
from celery.result import AsyncResult 

logging.basicConfig(level=os.getenv("OCI_MIGRATOR_LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


ENV_FILE_PATH = os.path.expanduser(os.getenv("OCI_MIGRATOR_ENV_FILE", "~/.oci-migrator.env"))
PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIST_DIR = Path(
    os.getenv("OCI_MIGRATOR_FRONTEND_DIST_DIR", str(PROJECT_ROOT / "frontend" / "dist"))
).resolve()


def _read_env_file(path: str) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file.

    - Ignores empty lines and comments (#...)
    - Does not support quoting/escaping (matches how we generate ~/.oci-migrator.env)
    """
    values: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                values[key.strip()] = val.strip()
    except FileNotFoundError:
        return {}
    return values


_CONFIG_CACHE_LOCK = Lock()
_CONFIG_CACHE: dict[str, object] = {
    "mtime": "__not_loaded__",
    "api_token": "",
    "allowed_origins": None,
    "admin_username": "admin",
    "admin_password_hash": "",
    "session_ttl_seconds": 43200,
}


def get_runtime_config() -> dict[str, object]:
    """Return runtime config reloaded when env file changes.

    This avoids requiring a service restart after editing ~/.oci-migrator.env.
    """
    try:
        mtime = os.path.getmtime(ENV_FILE_PATH)
    except FileNotFoundError:
        mtime = None

    with _CONFIG_CACHE_LOCK:
        if _CONFIG_CACHE["mtime"] != mtime:
            file_env = _read_env_file(ENV_FILE_PATH) if mtime is not None else {}
            api_token = (file_env.get("OCI_MIGRATOR_API_TOKEN") or os.getenv("OCI_MIGRATOR_API_TOKEN", "")).strip()

            raw_origins = (
                file_env.get("OCI_MIGRATOR_ALLOWED_ORIGINS")
                or os.getenv("OCI_MIGRATOR_ALLOWED_ORIGINS")
                or "http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173,http://127.0.0.1:5173"
            )
            allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

            admin_username = (
                file_env.get("OCI_MIGRATOR_ADMIN_USERNAME")
                or os.getenv("OCI_MIGRATOR_ADMIN_USERNAME")
                or "admin"
            ).strip()
            admin_password_hash = (
                file_env.get("OCI_MIGRATOR_ADMIN_PASSWORD_HASH")
                or os.getenv("OCI_MIGRATOR_ADMIN_PASSWORD_HASH")
                or ""
            ).strip()
            try:
                session_ttl_seconds = int(
                    file_env.get("OCI_MIGRATOR_SESSION_TTL_SECONDS")
                    or os.getenv("OCI_MIGRATOR_SESSION_TTL_SECONDS", "43200")
                )
            except ValueError:
                session_ttl_seconds = 43200

            _CONFIG_CACHE["mtime"] = mtime
            _CONFIG_CACHE["api_token"] = api_token
            _CONFIG_CACHE["allowed_origins"] = allowed_origins
            _CONFIG_CACHE["admin_username"] = admin_username
            _CONFIG_CACHE["admin_password_hash"] = admin_password_hash
            _CONFIG_CACHE["session_ttl_seconds"] = session_ttl_seconds

        return {k: v for k, v in _CONFIG_CACHE.items() if k != "mtime"}
CONFIG_LOCK = Lock()
JOBS_LOCK = Lock()
RCLONE_LOCK = Lock()
SESSION_LOCK = Lock()
REVOKED_SESSIONS: dict[str, float] = {}


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 390000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iterations_raw, salt_raw, digest_raw = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False

        iterations = int(iterations_raw)
        salt = _b64decode(salt_raw)
        expected_digest = _b64decode(digest_raw)
        actual_digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual_digest, expected_digest)
    except Exception:
        return False


def session_signing_key(config: dict[str, object]) -> bytes:
    secret_material = "\0".join(
        [
            str(config.get("api_token", "")),
            str(config.get("admin_password_hash", "")),
            ENV_FILE_PATH,
        ]
    )
    return hashlib.sha256(secret_material.encode("utf-8")).digest()


def sign_session_payload(payload: str, config: dict[str, object]) -> str:
    digest = hmac.new(session_signing_key(config), payload.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def decode_session_token(token: str, config: dict[str, object]) -> dict | None:
    payload_raw, separator, signature = token.partition(".")
    if not separator or not payload_raw or not signature:
        return None

    expected_signature = sign_session_payload(payload_raw, config)
    if not hmac.compare_digest(signature, expected_signature):
        return None

    try:
        payload = json.loads(_b64url_decode(payload_raw).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None

    return payload if isinstance(payload, dict) else None


def prune_revoked_sessions(now: float) -> None:
    expired_tokens = [token for token, expiry in REVOKED_SESSIONS.items() if expiry <= now]
    for token in expired_tokens:
        REVOKED_SESSIONS.pop(token, None)


def create_session_token() -> str:
    config = get_runtime_config()
    ttl = int(config.get("session_ttl_seconds", 43200))
    now = int(time.time())
    expires_at = now + max(ttl, 300)
    payload = {
        "ver": 1,
        "sub": str(config.get("admin_username", "admin")),
        "iat": now,
        "exp": expires_at,
        "nonce": secrets.token_urlsafe(18),
    }
    payload_raw = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{payload_raw}.{sign_session_payload(payload_raw, config)}"


def invalidate_session_token(token: str) -> None:
    config = get_runtime_config()
    payload = decode_session_token(token, config) or {}
    try:
        expires_at = float(payload.get("exp", time.time() + int(config.get("session_ttl_seconds", 43200))))
    except (TypeError, ValueError):
        expires_at = time.time() + int(config.get("session_ttl_seconds", 43200))

    with SESSION_LOCK:
        prune_revoked_sessions(time.time())
        REVOKED_SESSIONS[token] = expires_at


def session_token_is_valid(token: str) -> bool:
    config = get_runtime_config()
    now = time.time()
    with SESSION_LOCK:
        prune_revoked_sessions(now)
        if token in REVOKED_SESSIONS:
            return False

    payload = decode_session_token(token, config)
    if not payload:
        return False

    try:
        expires_at = float(payload.get("exp", 0))
    except (TypeError, ValueError):
        return False
    if expires_at <= now:
        return False
    if payload.get("sub") != config.get("admin_username", "admin"):
        return False

    return True


def bearer_token_from_header(authorization: Optional[str]) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def request_is_authenticated(request) -> bool:
    config = get_runtime_config()

    api_token = str(config.get("api_token", "")).strip()
    x_api_token = request.headers.get("X-API-Token", "")
    if api_token and hmac.compare_digest(x_api_token, api_token):
        return True

    bearer_token = bearer_token_from_header(request.headers.get("Authorization"))
    return bool(bearer_token and session_token_is_valid(bearer_token))


def _write_env_values(path: str, updates: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing_lines: list[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            existing_lines = handle.readlines()

    remaining = dict(updates)
    output_lines: list[str] = []

    for raw_line in existing_lines:
        line = raw_line.rstrip("\n")
        if not line or line.lstrip().startswith("#") or "=" not in line:
            output_lines.append(raw_line if raw_line.endswith("\n") else f"{raw_line}\n")
            continue

        key, _ = line.split("=", 1)
        if key in remaining:
            output_lines.append(f"{key}={remaining.pop(key)}\n")
        else:
            output_lines.append(raw_line if raw_line.endswith("\n") else f"{raw_line}\n")

    for key, value in remaining.items():
        output_lines.append(f"{key}={value}\n")

    file_descriptor, temp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temp_file:
            temp_file.writelines(output_lines)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        with _CONFIG_CACHE_LOCK:
            _CONFIG_CACHE["mtime"] = "__not_loaded__"
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def require_api_token(x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")) -> None:
    config = get_runtime_config()
    api_token = str(config.get("api_token", "")).strip()

    if not api_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server API token is not configured.",
        )

    if not hmac.compare_digest(x_api_token or "", api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token.",
        )


app = FastAPI(title="OCI Migration & Sync Engine")

PUBLIC_PATHS = {
    "/",
    "/health",
    "/index.html",
    "/auth/login",
    "/docs",
    "/favicon.ico",
    "/favicon.svg",
    "/openapi.json",
    "/redoc",
    "/vite.svg",
}
PUBLIC_PATH_PREFIXES = ("/assets/",)


@app.middleware("http")
async def dynamic_cors_allowlist(request, call_next):
    """Dynamic CORS + allowlist enforcement.

    We implement CORS ourselves (including OPTIONS preflight) so that:
    - the allowlist can be changed without restarting the service
    - preflight requests behave consistently
    """
    origin = request.headers.get("origin")

    if origin:
        config = get_runtime_config()
        allowed_origins = list(config.get("allowed_origins", []))
        if origin not in allowed_origins:
            return JSONResponse(
                status_code=403,
                content={"detail": "Origin not allowed"},
                headers={"Vary": "Origin"},
            )

        # Preflight: reply immediately with required headers
        if request.method == "OPTIONS":
            request_headers = request.headers.get("access-control-request-headers", "")
            request_method = request.headers.get("access-control-request-method", "")

            headers = {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": request_method or "GET,POST,PUT,PATCH,DELETE,OPTIONS",
                "Access-Control-Allow-Headers": request_headers or "Content-Type, X-API-Token, Authorization",
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
            }
            return JSONResponse(status_code=204, content=None, headers=headers)

    is_public_login = request.method == "POST" and request.url.path == "/auth/login"
    is_public_static = request.method in {"GET", "HEAD"} and (
        request.url.path in PUBLIC_PATHS
        or any(request.url.path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES)
    )

    if not (is_public_login or is_public_static) and not request_is_authenticated(request):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing admin session."},
            headers={"Vary": "Origin"} if origin else {},
        )

    response = await call_next(request)

    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"

    return response

# --- Paths ---
OCI_DIR = os.path.expanduser("~/.oci")
CONFIG_PATH = os.path.join(OCI_DIR, "config")
RCLONE_CONF = os.path.expanduser("~/.config/rclone/rclone.conf")
JOBS_FILE = os.path.join(OCI_DIR, "jobs.json")
LOCAL_DATA_ROOT = Path(os.getenv("OCI_MIGRATOR_LOCAL_DATA_ROOT", "/var/lib/oci-migrator/local")).resolve()
RUNTIME_RESTORE_BACKUP_DIR = Path(
    os.getenv("OCI_MIGRATOR_RESTORE_BACKUP_DIR", os.path.join(OCI_DIR, "runtime-restore-backups"))
).expanduser()
try:
    RUNTIME_CONFIG_IMPORT_MAX_BYTES = int(os.getenv("OCI_MIGRATOR_IMPORT_MAX_BYTES", str(25 * 1024 * 1024)))
except ValueError:
    RUNTIME_CONFIG_IMPORT_MAX_BYTES = 25 * 1024 * 1024
LOCAL_SHARE_HELPER = Path(os.getenv("OCI_MIGRATOR_LOCAL_SHARE_HELPER", "/usr/local/sbin/oci-migrator-local-share")).resolve()
JOB_LOG_HELPER = Path(os.getenv("OCI_MIGRATOR_JOB_LOG_HELPER", "/usr/local/sbin/oci-migrator-job-log")).resolve()
TIME_SYNC_HELPER = Path(os.getenv("OCI_MIGRATOR_TIME_SYNC_HELPER", "/usr/local/sbin/oci-migrator-time-sync")).resolve()
NETWORK_HELPER = Path(os.getenv("OCI_MIGRATOR_NETWORK_HELPER", "/usr/local/sbin/oci-migrator-network")).resolve()
TIMESYNCD_CONF = Path(os.getenv("OCI_MIGRATOR_TIMESYNCD_CONF", "/etc/systemd/timesyncd.conf.d/oci-migrator.conf")).resolve()
JOB_LOGROTATE_FILE = Path(os.getenv("OCI_MIGRATOR_JOB_LOGROTATE_FILE", "/etc/logrotate.d/migrator-job-logs"))
UPGRADE_HELPER = Path(os.getenv("OCI_MIGRATOR_UPGRADE_HELPER", "/usr/local/sbin/oci-migrator-upgrade")).resolve()
UNINSTALL_HELPER = Path(os.getenv("OCI_MIGRATOR_UNINSTALL_HELPER", "/usr/local/sbin/oci-migrator-uninstall")).resolve()
UPGRADE_STATUS_FILE = Path(
    os.getenv("OCI_MIGRATOR_UPGRADE_STATUS_FILE", "/var/lib/oci-migrator/upgrade/status.json")
).resolve()
UPGRADE_LOG_FILE = Path(os.getenv("OCI_MIGRATOR_UPGRADE_LOG_FILE", "/var/log/oci-migrator/upgrade.log")).resolve()
UPGRADE_LOCK_DIR = UPGRADE_STATUS_FILE.parent / "upgrade.lock"
SERVICE_PREFIX = os.getenv("OCI_MIGRATOR_SERVICE_PREFIX", "migrator")
EXPECTED_TIMEZONE = os.getenv("OCI_MIGRATOR_TIMEZONE", "Europe/Stockholm")
NTP_SERVERS = os.getenv(
    "OCI_MIGRATOR_NTP_SERVERS",
    "0.se.pool.ntp.org 1.se.pool.ntp.org 2.se.pool.ntp.org 3.se.pool.ntp.org",
).replace(",", " ")
try:
    LOCAL_SHARE_TIMEOUT_SECONDS = int(os.getenv("OCI_MIGRATOR_LOCAL_SHARE_TIMEOUT_SECONDS", "300"))
except ValueError:
    LOCAL_SHARE_TIMEOUT_SECONDS = 300
JOB_LOG_SETTINGS_LOCK = Lock()
TIME_SETTINGS_LOCK = Lock()
NETWORK_SETTINGS_LOCK = Lock()
UPGRADE_LOCK = Lock()

# Säkerställ att mappar finns
os.makedirs(os.path.dirname(RCLONE_CONF), exist_ok=True)
os.makedirs(OCI_DIR, exist_ok=True)

# --- Schemas ---
class LoginRequest(BaseModel):
    username: str = "admin"
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class UninstallRequest(BaseModel):
    current_password: str
    confirmation: str
    purge_local_backups: bool = False


class ConfigSchema(BaseModel):
    profile_name: str
    user_ocid: str
    tenancy_ocid: str
    fingerprint: str
    region: str
    key_file_name: Optional[str] = None
    compartment_ocid: str
    storage_compartment_ocid: str = ""

class ScheduleSchema(BaseModel):
    frequency: str
    time: str
    day_of_week: Optional[str] = None
    day_of_month: Optional[str] = None


class MetadataTag(BaseModel):
    key: str
    value: str


class LifecycleFilterConfig(BaseModel):
    type: str = "include_prefix"
    value: str = ""


class LifecycleRuleConfig(BaseModel):
    name: str = ""
    target: str = "objects"
    action: str = "ARCHIVE"
    days: Optional[int] = None
    enabled: bool = True
    filters: List[LifecycleFilterConfig] = Field(default_factory=list)

    @field_validator("days", mode="before")
    @classmethod
    def blank_rule_days_to_none(cls, value):
        return None if value == "" else value


class LifecyclePolicyConfig(BaseModel):
    enabled: bool = False
    prefix: str = ""
    filters: List[LifecycleFilterConfig] = Field(default_factory=list)
    rules: List[LifecycleRuleConfig] = Field(default_factory=list)
    infrequent_access_after_days: Optional[int] = None
    archive_after_days: Optional[int] = None
    delete_after_days: Optional[int] = None
    previous_versions_delete_after_days: Optional[int] = None

    @field_validator(
        "infrequent_access_after_days",
        "archive_after_days",
        "delete_after_days",
        "previous_versions_delete_after_days",
        mode="before",
    )
    @classmethod
    def blank_lifecycle_days_to_none(cls, value):
        return None if value == "" else value


class LocalRetentionConfig(BaseModel):
    enabled: bool = False
    delete_after_days: int = 30
    min_file_age_hours: int = 24


class DataSyncJob(BaseModel):
    name: str
    previous_name: str = ""
    source_remote: str
    dest_profile: str
    dest_bucket: str
    sync_mode: str = "copy"
    transfers: int = 4
    checkers: int = 8
    buffer_size: str = "16M"
    bwlimit: str = ""
    tpslimit: Optional[float] = None
    is_active: bool = True
    metadata_tags: List[MetadataTag] = Field(default_factory=list)
    lifecycle_policy: LifecyclePolicyConfig = Field(default_factory=LifecyclePolicyConfig)
    local_retention: LocalRetentionConfig = Field(default_factory=LocalRetentionConfig)
    schedule: ScheduleSchema

class BulkMigrationJob(BaseModel):
    vm_ids: List[str]
    source_profile: str
    dest_profile: str
    bucket_name: str


class JobLogSettingsRequest(BaseModel):
    max_size: str
    retention_days: int


class LocalDiskSettingsRequest(BaseModel):
    warning_percent: int
    critical_percent: int


class TimeSettingsRequest(BaseModel):
    timezone: str
    ntp_servers: str


class NetworkSettingsRequest(BaseModel):
    mode: str = "dhcp"
    interface: str
    address: str = ""
    prefix_length: int = 24
    gateway: str = ""
    dns_servers: str = ""


class RcloneDefaultSettingsRequest(BaseModel):
    bwlimit: str = ""
    tpslimit: Optional[float] = None


class NotificationSettingsRequest(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 514
    protocol: str = "udp"
    facility: str = "local0"
    events: str = "failures_recovery"


# NYA SCHEMAS FÖR STORAGE EXPLORER
class CreateBucketReq(BaseModel):
    profile_name: str
    bucket_name: str
    storage_tier: str = "Standard"
    auto_tiering: str = "Disabled"
    versioning: str = "Disabled"

class CreateFolderReq(BaseModel):
    profile_name: str
    bucket_name: str
    folder_name: str


class BucketProtectionReq(BaseModel):
    profile_name: str
    bucket_name: str


class BucketVersioningReq(BaseModel):
    profile_name: str
    bucket_name: str
    versioning: str


class BucketAutoTieringReq(BaseModel):
    profile_name: str
    bucket_name: str
    auto_tiering: str


class BucketLifecyclePolicyReq(BaseModel):
    profile_name: str
    bucket_name: str
    lifecycle_policy: LifecyclePolicyConfig = Field(default_factory=LifecyclePolicyConfig)

# --- Helpers ---


def sanitize_filename(filename: str, default_name: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)
    return safe_name or default_name


def normalize_job_name(job_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", job_name.strip()) or "default"


def normalize_local_folder_name(folder_name: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", folder_name.strip()).strip("._-")
    if not safe_name:
        raise HTTPException(status_code=400, detail="Local folder name is required.")
    return safe_name


def local_path_is_under_root(path: Path) -> bool:
    try:
        path.relative_to(LOCAL_DATA_ROOT)
        return True
    except ValueError:
        return False


def create_server_local_folder(folder_name: str) -> Path:
    safe_name = normalize_local_folder_name(folder_name)
    target = (LOCAL_DATA_ROOT / safe_name).resolve()
    if not local_path_is_under_root(target):
        raise HTTPException(status_code=400, detail="Local folder path is outside the managed data root.")

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise_operation_error(
            500,
            "Create local folder",
            exc,
            f"Check write permissions for {LOCAL_DATA_ROOT}.",
        )
    return target


def normalize_smb_share_name(raw_name: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name.strip()).strip("._-")
    if not safe_name:
        raise HTTPException(status_code=400, detail="SMB share name is required.")
    if safe_name.lower() in {"global", "homes", "printers", "print$"}:
        raise HTTPException(status_code=400, detail="This SMB share name is reserved.")
    return safe_name[:80]


def validate_smb_username(username: str) -> str:
    username = username.strip()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
        raise HTTPException(
            status_code=400,
            detail="SMB username must be lowercase and may contain letters, numbers, underscore, and dash.",
        )
    if username == "root":
        raise HTTPException(status_code=400, detail="SMB username cannot be root.")
    return username


def parse_form_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def validate_nfs_clients(clients: str) -> str:
    normalized_clients = []
    for token in re.split(r"[\s,]+", clients.strip()):
        if not token:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", token):
            raise HTTPException(
                status_code=400,
                detail="NFS clients must be hostnames, IP addresses, or CIDR ranges.",
            )
        if any(character in token for character in "*()[]"):
            raise HTTPException(
                status_code=400,
                detail="NFS client allow list may not contain wildcards or export options.",
            )
        normalized_clients.append(token)

    if not normalized_clients:
        raise HTTPException(status_code=400, detail="NFS client allow list is required.")
    return " ".join(normalized_clients)


def share_host_from_request(request: Request) -> str:
    host = request.url.hostname or request.headers.get("host", "server").split(":", 1)[0]
    return host.strip("[]") or "server"


def local_share_helper_command() -> list[str]:
    if not LOCAL_SHARE_HELPER.is_file():
        raise HTTPException(
            status_code=503,
            detail="Local share helper is not installed. Rerun ./install.sh and try again.",
        )

    if os.geteuid() == 0:
        return [str(LOCAL_SHARE_HELPER)]

    sudo_path = shutil.which("sudo")
    if not sudo_path:
        raise HTTPException(
            status_code=503,
            detail="sudo is required for local share setup. Rerun ./install.sh and try again.",
        )
    return [sudo_path, "-n", str(LOCAL_SHARE_HELPER)]


def run_local_share_helper(args: list[str], password: str = "") -> dict:
    password_path = ""
    try:
        if password:
            file_descriptor, password_path = tempfile.mkstemp(prefix="oci-migrator-smb-", text=True)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(password)
            os.chmod(password_path, 0o600)

        command = local_share_helper_command() + args
        if password_path:
            command.extend(["--password-file", password_path])

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=LOCAL_SHARE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise RuntimeError(truncate_text(result.stderr or result.stdout or f"helper exited with code {result.returncode}"))

        try:
            return json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        except (json.JSONDecodeError, IndexError):
            return {"raw_output": truncate_text(result.stdout, 600)}
    except subprocess.TimeoutExpired as exc:
        raise_operation_error(
            504,
            "Configure local share",
            exc,
            "Local share installation/configuration took too long. Check apt, systemd, and firewall status on the server.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise_operation_error(
            500,
            "Configure local share",
            exc,
            "Check that install.sh installed /usr/local/sbin/oci-migrator-local-share and sudoers access for the service user.",
        )
    finally:
        if password_path and os.path.exists(password_path):
            os.remove(password_path)


def enable_local_share(
    local_path: Path,
    share_name: str,
    access_mode: str,
    username: str = "",
    password: str = "",
) -> dict:
    if access_mode not in {"everyone", "user"}:
        raise HTTPException(status_code=400, detail="Unsupported SMB share access mode.")

    command_args = [
        "enable",
        "--share-name",
        share_name,
        "--path",
        str(local_path),
        "--access",
        access_mode,
    ]

    if access_mode == "user":
        username = validate_smb_username(username)
        if len(password) < 8:
            raise HTTPException(status_code=400, detail="SMB password must be at least 8 characters.")
        command_args.extend(["--user", username])

    return run_local_share_helper(command_args, password=password if access_mode == "user" else "")


def disable_local_share(share_name: str) -> None:
    run_local_share_helper(["disable", "--share-name", share_name])


def enable_local_nfs_share(local_path: Path, share_name: str, clients: str) -> dict:
    nfs_clients = validate_nfs_clients(clients)
    return run_local_share_helper(
        [
            "enable-nfs",
            "--share-name",
            share_name,
            "--path",
            str(local_path),
            "--clients",
            nfs_clients,
        ]
    )


def disable_local_nfs_share(share_name: str) -> None:
    run_local_share_helper(["disable-nfs", "--share-name", share_name])


def validate_external_mount_path(raw_path: str) -> Path:
    if not raw_path.strip():
        raise HTTPException(status_code=400, detail="Mount path is required.")

    path = Path(raw_path.strip()).expanduser()
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail="Mount path must be absolute.")

    resolved = path.resolve()
    blocked_paths = {
        Path("/"),
        Path("/bin"),
        Path("/boot"),
        Path("/dev"),
        Path("/etc"),
        Path("/home"),
        Path("/lib"),
        Path("/lib64"),
        Path("/opt"),
        Path("/proc"),
        Path("/root"),
        Path("/run"),
        Path("/sbin"),
        Path("/sys"),
        Path("/tmp"),
        Path("/usr"),
        Path("/var"),
    }
    if resolved in blocked_paths:
        raise HTTPException(status_code=400, detail="Choose a specific mounted share path, not a system directory.")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="Mount path does not exist or is not a directory.")
    return resolved


def list_local_source_entries(base_path: Path) -> list[dict]:
    if not base_path.is_dir():
        raise HTTPException(status_code=404, detail="Local source path does not exist.")

    entries = [{"name": f"This folder ({base_path})", "value": str(base_path)}]
    try:
        children = sorted(base_path.iterdir(), key=lambda child: child.name.lower())
    except OSError as exc:
        raise_operation_error(500, "List local folder", exc, "Check read permissions for this folder.")

    for child in children:
        if child.is_dir():
            entries.append({"name": f"{child.name}/", "value": str(child.resolve())})
    return entries


def write_ini_atomically(parser: configparser.ConfigParser, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    file_descriptor, temp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(file_descriptor, "w") as temp_file:
            parser.write(temp_file)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def load_jobs() -> list[dict]:
    if not os.path.exists(JOBS_FILE):
        return []

    with open(JOBS_FILE, "r", encoding="utf-8") as file_handle:
        try:
            return json.load(file_handle)
        except json.JSONDecodeError:
            logger.warning("Unable to parse %s, returning an empty job list.", JOBS_FILE)
            return []


def write_jobs_atomically(jobs: list[dict]) -> None:
    file_descriptor, temp_path = tempfile.mkstemp(dir=os.path.dirname(JOBS_FILE))
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temp_file:
            json.dump(jobs, temp_file, indent=4)
        os.replace(temp_path, JOBS_FILE)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def truncate_text(value: str, limit: int = 1200) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else f"{value[:limit]}..."


def build_error_detail(operation: str, exc: Exception, hint: Optional[str] = None) -> dict:
    detail = {
        "message": f"{operation} failed.",
        "operation": operation,
        "error_type": exc.__class__.__name__,
    }

    if isinstance(exc, oci.exceptions.ServiceError):
        detail.update(
            {
                "status": exc.status,
                "code": exc.code,
                "service_message": truncate_text(exc.message),
                "opc_request_id": getattr(exc, "request_id", None),
            }
        )
    else:
        detail["error"] = truncate_text(str(exc))

    if hint:
        detail["hint"] = hint

    return detail


def raise_operation_error(
    status_code: int,
    operation: str,
    exc: Exception,
    hint: Optional[str] = None,
) -> None:
    logger.warning("%s failed: %s", operation, exc)
    raise HTTPException(status_code=status_code, detail=build_error_detail(operation, exc, hint))


def health_check_item(state: str, message: str) -> dict:
    return {"status": state, "message": message}


def status_rank(value: str) -> int:
    return {"ok": 0, "warn": 1, "error": 2}.get(str(value or "").lower(), 1)


def worst_status(*statuses: str) -> str:
    ranked = sorted((str(status or "warn").lower() for status in statuses), key=status_rank, reverse=True)
    return ranked[0] if ranked else "ok"


def parse_iso_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def iso_to_unix_seconds(value: str) -> float:
    parsed = parse_iso_timestamp(value)
    return parsed.timestamp() if parsed else 0.0


def systemd_unit_state(unit_name: str) -> dict:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {"status": "warn", "state": "unknown", "message": "systemctl is not available."}

    try:
        result = subprocess.run(
            [systemctl, "is-active", unit_name],
            capture_output=True,
            text=True,
            timeout=3,
        )
        state = (result.stdout or result.stderr or "").strip() or "unknown"
        status_value = "ok" if result.returncode == 0 and state == "active" else "error"
        return {"status": status_value, "state": state, "message": f"{unit_name} is {state}."}
    except Exception as exc:
        return {"status": "warn", "state": "unknown", "message": f"Unable to read {unit_name}: {truncate_text(str(exc), 300)}"}


def prometheus_escape_label(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def timedatectl_value(property_name: str) -> str:
    timedatectl_path = shutil.which("timedatectl")
    if not timedatectl_path:
        return ""

    try:
        result = subprocess.run(
            [timedatectl_path, "show", "-p", property_name, "--value"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def read_runtime_env() -> dict[str, str]:
    return _read_env_file(ENV_FILE_PATH) if os.path.exists(ENV_FILE_PATH) else {}


def normalize_notification_settings(settings: NotificationSettingsRequest, require_host: bool = False) -> dict:
    try:
        return validate_notification_settings(settings.model_dump(), require_host=require_host)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def redis_url_from_runtime() -> str:
    runtime_env = read_runtime_env()
    return runtime_env.get("OCI_MIGRATOR_REDIS_URL") or os.getenv(
        "OCI_MIGRATOR_REDIS_URL", "redis://localhost:6379/0"
    )


def normalize_job_log_max_size(value: str) -> str:
    max_size = (value or "").strip().upper()
    if not re.fullmatch(r"[1-9][0-9]*[KMG]?", max_size):
        raise HTTPException(status_code=400, detail="Max size must look like 10M, 512K, or 1G.")
    return max_size


def validate_job_log_retention_days(value: int) -> int:
    try:
        retention_days = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Retention days must be a number.")

    if retention_days < 1 or retention_days > 365:
        raise HTTPException(status_code=400, detail="Retention days must be between 1 and 365.")
    return retention_days


def normalize_time_zone(value: str) -> str:
    timezone_value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_+.-]+(/[A-Za-z0-9_+.-]+)*", timezone_value):
        raise HTTPException(status_code=400, detail="Timezone must be a valid IANA name, for example Europe/Stockholm or Asia/Singapore.")

    if not Path("/usr/share/zoneinfo", timezone_value).is_file():
        raise HTTPException(status_code=400, detail=f"Timezone data not found for {timezone_value}.")
    return timezone_value


def normalize_ntp_servers(value: str) -> str:
    servers = re.sub(r"[\s,]+", " ", str(value or "").strip())
    if not servers:
        raise HTTPException(status_code=400, detail="At least one NTP server is required.")

    for server in servers.split(" "):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", server):
            raise HTTPException(status_code=400, detail=f"Invalid NTP server: {server}")
    return servers


def normalize_network_settings(settings: NetworkSettingsRequest) -> dict:
    mode = str(settings.mode or "").strip().lower()
    if mode not in {"dhcp", "static"}:
        raise HTTPException(status_code=400, detail="Network mode must be DHCP or static IPv4.")

    interface = str(settings.interface or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", interface) or interface == "lo":
        raise HTTPException(status_code=400, detail="Select a valid non-loopback network interface.")
    if not Path("/sys/class/net", interface).is_dir():
        raise HTTPException(status_code=400, detail=f"Network interface does not exist: {interface}")

    if mode == "dhcp":
        return {
            "mode": mode,
            "interface": interface,
            "address": "",
            "gateway": "",
            "dns_servers": "",
        }

    try:
        prefix_length = int(settings.prefix_length)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="IPv4 prefix length must be a number between 1 and 32.")
    if prefix_length < 1 or prefix_length > 32:
        raise HTTPException(status_code=400, detail="IPv4 prefix length must be between 1 and 32.")

    try:
        address = ipaddress.IPv4Address(str(settings.address or "").strip())
        gateway = ipaddress.IPv4Address(str(settings.gateway or "").strip())
    except ipaddress.AddressValueError:
        raise HTTPException(status_code=400, detail="Static address and gateway must be valid IPv4 addresses.")

    network = ipaddress.IPv4Network(f"{address}/{prefix_length}", strict=False)
    if prefix_length <= 30 and address in {network.network_address, network.broadcast_address}:
        raise HTTPException(status_code=400, detail="Static IPv4 address cannot be the network or broadcast address.")
    if address == gateway:
        raise HTTPException(status_code=400, detail="Gateway cannot be the same as the static IPv4 address.")

    raw_dns = str(settings.dns_servers or "").replace(",", " ")
    dns_servers = [item for item in raw_dns.split() if item]
    if not dns_servers:
        raise HTTPException(status_code=400, detail="At least one IPv4 DNS server is required for static mode.")
    if len(dns_servers) > 4:
        raise HTTPException(status_code=400, detail="No more than four DNS servers may be configured.")
    try:
        normalized_dns = [str(ipaddress.IPv4Address(item)) for item in dns_servers]
    except ipaddress.AddressValueError:
        raise HTTPException(status_code=400, detail="DNS servers must be valid IPv4 addresses.")

    return {
        "mode": mode,
        "interface": interface,
        "address": f"{address}/{prefix_length}",
        "gateway": str(gateway),
        "dns_servers": " ".join(normalized_dns),
    }


def configured_time_settings() -> tuple[str, str]:
    runtime_env = read_runtime_env()
    timezone_value = runtime_env.get("OCI_MIGRATOR_TIMEZONE") or os.getenv("OCI_MIGRATOR_TIMEZONE", EXPECTED_TIMEZONE)
    ntp_servers = runtime_env.get("OCI_MIGRATOR_NTP_SERVERS") or os.getenv("OCI_MIGRATOR_NTP_SERVERS", NTP_SERVERS)
    return str(timezone_value).strip(), normalize_ntp_servers(ntp_servers)


def current_time_settings() -> dict:
    configured_timezone, configured_ntp_servers = configured_time_settings()
    current_timezone = timedatectl_value("Timezone")
    ntp_synchronized = timedatectl_value("NTPSynchronized")
    ntp_service_active = timedatectl_value("NTP")
    return {
        "configured_timezone": configured_timezone,
        "timezone": current_timezone or configured_timezone,
        "timezone_matches_config": bool(current_timezone and current_timezone == configured_timezone),
        "ntp_servers": configured_ntp_servers,
        "ntp_synchronized": ntp_synchronized == "yes",
        "ntp_enabled": ntp_service_active == "yes",
        "timesyncd_conf": str(TIMESYNCD_CONF),
        "helper_installed": TIME_SYNC_HELPER.is_file(),
    }


def current_job_log_settings() -> dict:
    runtime_env = read_runtime_env()
    max_size = normalize_job_log_max_size(
        runtime_env.get("OCI_MIGRATOR_JOB_LOG_MAX_SIZE")
        or os.getenv("OCI_MIGRATOR_JOB_LOG_MAX_SIZE", "10M")
    )
    retention_days = validate_job_log_retention_days(
        runtime_env.get("OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS")
        or os.getenv("OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS", "14")
    )
    return {
        "job_log_dir": str(JOB_LOG_DIR),
        "max_size": max_size,
        "retention_days": retention_days,
        "rotation_frequency": "daily",
        "logrotate_file": str(JOB_LOGROTATE_FILE),
    }


def validate_local_retention_config(local_retention: LocalRetentionConfig | dict | None) -> dict:
    if isinstance(local_retention, LocalRetentionConfig):
        raw_policy = local_retention.model_dump()
    elif isinstance(local_retention, dict):
        raw_policy = local_retention
    else:
        raw_policy = {}

    enabled = bool(raw_policy.get("enabled", False))
    try:
        delete_after_days = int(raw_policy.get("delete_after_days", 30))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Local cleanup retention days must be a whole number.")
    try:
        min_file_age_hours = int(raw_policy.get("min_file_age_hours", 24))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Local cleanup minimum file age must be a whole number.")

    if delete_after_days < 1 or delete_after_days > 3650:
        raise HTTPException(status_code=400, detail="Local cleanup retention days must be between 1 and 3650.")
    if min_file_age_hours < 1 or min_file_age_hours > 720:
        raise HTTPException(status_code=400, detail="Local cleanup minimum file age must be between 1 and 720 hours.")

    return {
        "enabled": enabled,
        "delete_after_days": delete_after_days,
        "min_file_age_hours": min_file_age_hours,
    }


def validate_rclone_bwlimit(value: str | None) -> str:
    bwlimit = str(value or "").strip()
    if not bwlimit:
        return ""
    if any(char in bwlimit for char in "\r\n\0") or bwlimit.startswith("-"):
        raise HTTPException(status_code=400, detail="Bandwidth limit is invalid.")
    if bwlimit.lower() == "off":
        return "off"
    if not re.fullmatch(r"\d+(?:\.\d+)?[KkMmGgTtPp]?", bwlimit):
        raise HTTPException(status_code=400, detail="Bandwidth limit must be empty, off, or a value like 700M, 1G, or 500K.")
    return bwlimit


def validate_rclone_tpslimit(value: float | int | str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="TPS limit must be a number.")
    if parsed == 0:
        return None
    if parsed < 0 or parsed > 10000:
        raise HTTPException(status_code=400, detail="TPS limit must be between 0 and 10000.")
    return parsed


def validate_rclone_limits(job: DataSyncJob) -> dict:
    return {
        "bwlimit": validate_rclone_bwlimit(job.bwlimit),
        "tpslimit": validate_rclone_tpslimit(job.tpslimit),
    }


def current_rclone_default_settings() -> dict:
    runtime_env = read_runtime_env()
    return {
        "bwlimit": validate_rclone_bwlimit(
            runtime_env.get("OCI_MIGRATOR_DEFAULT_BWLIMIT")
            or os.getenv("OCI_MIGRATOR_DEFAULT_BWLIMIT", "")
        ),
        "tpslimit": validate_rclone_tpslimit(
            runtime_env.get("OCI_MIGRATOR_DEFAULT_TPSLIMIT")
            or os.getenv("OCI_MIGRATOR_DEFAULT_TPSLIMIT", "")
        ),
    }


def source_remote_is_managed_local(source_remote: str) -> bool:
    source_value = str(source_remote or "")
    separator_index = source_value.find(":")
    if separator_index < 0:
        return False

    local_target = source_value[separator_index + 1 :]
    if not local_target.startswith("/"):
        return False

    try:
        Path(local_target).expanduser().resolve().relative_to(LOCAL_DATA_ROOT)
        return True
    except ValueError:
        return False


def validate_local_retention_usage(job_name: str, previous_job_name: str, source_remote: str, local_retention: dict, jobs: list[dict]) -> None:
    if not local_retention.get("enabled"):
        return

    if not source_remote_is_managed_local(source_remote):
        raise HTTPException(
            status_code=400,
            detail="Local cleanup can only be enabled for managed server local folders.",
        )

    for existing_job in jobs:
        if existing_job.get("name") in {job_name, previous_job_name}:
            continue
        existing_retention = validate_local_retention_config(existing_job.get("local_retention", {}))
        if existing_retention.get("enabled") and existing_job.get("source_remote") == source_remote:
            raise HTTPException(
                status_code=400,
                detail=f"Local cleanup is already enabled for this source by job '{existing_job.get('name')}'.",
            )


def format_bytes(size_bytes: int) -> str:
    size = float(max(int(size_bytes), 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size_bytes} B"


def local_disk_thresholds() -> tuple[int, int]:
    runtime_env = read_runtime_env()
    try:
        warning_percent = int(
            runtime_env.get("OCI_MIGRATOR_LOCAL_DISK_WARNING_PERCENT")
            or os.getenv("OCI_MIGRATOR_LOCAL_DISK_WARNING_PERCENT", "80")
        )
    except ValueError:
        warning_percent = 80
    try:
        critical_percent = int(
            runtime_env.get("OCI_MIGRATOR_LOCAL_DISK_CRITICAL_PERCENT")
            or os.getenv("OCI_MIGRATOR_LOCAL_DISK_CRITICAL_PERCENT", "90")
        )
    except ValueError:
        critical_percent = 90
    warning_percent = max(1, min(99, warning_percent))
    critical_percent = max(warning_percent + 1, min(100, critical_percent))
    return warning_percent, critical_percent


def validate_local_disk_thresholds(warning_percent: int, critical_percent: int) -> tuple[int, int]:
    try:
        warning = int(warning_percent)
        critical = int(critical_percent)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Local disk thresholds must be whole numbers.")

    if warning < 1 or warning > 99:
        raise HTTPException(status_code=400, detail="Warning threshold must be between 1 and 99 percent.")
    if critical < 2 or critical > 100:
        raise HTTPException(status_code=400, detail="Critical threshold must be between 2 and 100 percent.")
    if critical <= warning:
        raise HTTPException(status_code=400, detail="Critical threshold must be higher than warning threshold.")
    return warning, critical


def current_local_disk_settings() -> dict:
    warning_percent, critical_percent = local_disk_thresholds()
    exists = LOCAL_DATA_ROOT.exists()
    total = used = free = 0
    used_percent = 0.0
    status_value = "warn"
    message = f"Local data root does not exist yet: {LOCAL_DATA_ROOT}"

    if exists:
        usage = shutil.disk_usage(LOCAL_DATA_ROOT)
        total = usage.total
        used = usage.used
        free = usage.free
        used_percent = round((used / total) * 100, 1) if total else 0.0
        status_value = "ok"
        if used_percent >= critical_percent:
            status_value = "error"
        elif used_percent >= warning_percent:
            status_value = "warn"
        message = f"Local data disk is {used_percent}% used."

    return {
        "local_data_root": str(LOCAL_DATA_ROOT),
        "exists": exists,
        "warning_percent": warning_percent,
        "critical_percent": critical_percent,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "used_percent": used_percent,
        "total": format_bytes(total),
        "used": format_bytes(used),
        "free": format_bytes(free),
        "status": status_value,
        "message": message,
    }


def normalize_metadata_tags(tags: list[MetadataTag | dict]) -> list[dict]:
    normalized_tags = []
    seen_keys = set()

    def normalize_metadata_key(raw_key: str) -> str:
        key = raw_key.strip().lower()
        suffix = key[len("opc-meta-"):] if key.startswith("opc-meta-") else key
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,118}", suffix):
            raise HTTPException(
                status_code=400,
                detail="Metadata names may contain lowercase letters, numbers, dot, underscore, and dash. OCI stores them as opc-meta-*.",
            )
        return suffix

    for tag in tags or []:
        raw_key = tag.key if isinstance(tag, MetadataTag) else str(tag.get("key", ""))
        raw_value = tag.value if isinstance(tag, MetadataTag) else str(tag.get("value", ""))
        key = raw_key.strip()
        value = raw_value.strip()

        if not key and not value:
            continue
        if not key or not value:
            raise HTTPException(status_code=400, detail="Metadata tags need both a key and a value.")
        key = normalize_metadata_key(key)
        if key in seen_keys:
            raise HTTPException(status_code=400, detail=f"Duplicate metadata tag key: {key}")
        if len(value) > 1024 or any(char in value for char in "\r\n\0"):
            raise HTTPException(status_code=400, detail="Metadata tag values must be single-line text up to 1024 characters.")

        seen_keys.add(key)
        normalized_tags.append({"key": key, "value": value})

    if len(normalized_tags) > 20:
        raise HTTPException(status_code=400, detail="A sync job can have at most 20 metadata tags.")

    return normalized_tags


def destination_bucket_name(value: str) -> str:
    bucket_name = str(value or "").strip().split("/", 1)[0]
    if not bucket_name:
        raise HTTPException(status_code=400, detail="Destination bucket is required.")
    return bucket_name


def normalize_lifecycle_prefix(value: str) -> str:
    prefix = str(value or "").strip().lstrip("/")
    if len(prefix) > 1024 or any(char in prefix for char in "\r\n\0"):
        raise HTTPException(status_code=400, detail="Lifecycle prefix must be single-line text up to 1024 characters.")
    return prefix


def normalize_lifecycle_filters(filters: object, legacy_prefix: str = "") -> list[dict]:
    normalized_filters: list[dict] = []
    allowed_types = {
        "include_prefix": "include_prefix",
        "include_pattern": "include_pattern",
        "exclude_pattern": "exclude_pattern",
    }

    if filters:
        if not isinstance(filters, list):
            raise HTTPException(status_code=400, detail="Lifecycle filters must be a list.")
        for item in filters:
            if isinstance(item, LifecycleFilterConfig):
                filter_data = item.model_dump()
            elif isinstance(item, dict):
                filter_data = item
            else:
                raise HTTPException(status_code=400, detail="Lifecycle filters must contain filter objects.")

            filter_type = str(filter_data.get("type", "include_prefix")).strip()
            if filter_type not in allowed_types:
                raise HTTPException(
                    status_code=400,
                    detail="Lifecycle filter type must be include_prefix, include_pattern, or exclude_pattern.",
                )
            value = str(filter_data.get("value", "")).strip()
            if not value:
                raise HTTPException(status_code=400, detail="Lifecycle filter values are required.")
            if len(value) > 1024 or any(char in value for char in "\r\n\0"):
                raise HTTPException(
                    status_code=400,
                    detail="Lifecycle filter values must be single-line text up to 1024 characters.",
                )
            normalized_filters.append({"type": allowed_types[filter_type], "value": value})

    legacy_prefix = normalize_lifecycle_prefix(legacy_prefix)
    if not normalized_filters and legacy_prefix:
        normalized_filters.append({"type": "include_prefix", "value": legacy_prefix})

    if len(normalized_filters) > 20:
        raise HTTPException(status_code=400, detail="A lifecycle policy can have at most 20 object name filters.")

    return normalized_filters


def normalize_lifecycle_days(value: Optional[int], field_name: str) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a number of days.")
    if days < 1 or days > 36500:
        raise HTTPException(status_code=400, detail=f"{field_name} must be between 1 and 36500 days.")
    return days


def normalize_storage_tier(value: str) -> str:
    tier = str(value or "Standard").strip()
    allowed = {
        "Standard": "Standard",
        "Archive": "Archive",
    }
    if tier not in allowed:
        raise HTTPException(status_code=400, detail="Bucket storage tier must be Standard or Archive.")
    return allowed[tier]


def normalize_versioning(value: str) -> str:
    versioning = str(value or "Disabled").strip()
    allowed = {
        "Disabled": "Disabled",
        "Enabled": "Enabled",
        "Suspended": "Suspended",
    }
    if versioning not in allowed:
        raise HTTPException(status_code=400, detail="Versioning must be Disabled, Enabled, or Suspended.")
    return allowed[versioning]


def normalize_update_versioning(value: str) -> str:
    versioning = str(value or "").strip()
    allowed = {
        "Enabled": "Enabled",
        "Suspended": "Suspended",
    }
    if versioning not in allowed:
        raise HTTPException(status_code=400, detail="Versioning update must be Enabled or Suspended.")
    return allowed[versioning]


def normalize_auto_tiering(value: str) -> str:
    auto_tiering = str(value or "Disabled").strip()
    allowed = {
        "Disabled": "Disabled",
        "InfrequentAccess": "InfrequentAccess",
    }
    if auto_tiering not in allowed:
        raise HTTPException(status_code=400, detail="Auto-Tiering must be Disabled or InfrequentAccess.")
    return allowed[auto_tiering]


def normalized_bucket_auto_tiering(value: object) -> str:
    auto_tiering = str(value or "Disabled").strip()
    if auto_tiering.replace("_", "").replace("-", "").lower() == "infrequentaccess":
        return "InfrequentAccess"
    return "Disabled"


def is_infrequent_access_lifecycle_rule(rule) -> bool:
    action = str(getattr(rule, "action", "") or "")
    return action.replace("_", "").replace("-", "").lower() == "infrequentaccess"


LIFECYCLE_ACTIONS = {
    "INFREQUENT_ACCESS": {
        "suffix": "infrequent-access",
        "targets": {"objects", "previous-object-versions"},
        "label": "Move to Infrequent Access",
    },
    "ARCHIVE": {
        "suffix": "archive",
        "targets": {"objects", "previous-object-versions"},
        "label": "Move to Archive",
    },
    "DELETE": {
        "suffix": "delete",
        "targets": {"objects", "previous-object-versions"},
        "label": "Delete",
    },
    "ABORT": {
        "suffix": "abort-multipart",
        "targets": {"multipart-uploads"},
        "label": "Abort uncommitted multipart uploads",
    },
}

LIFECYCLE_TARGETS = {"objects", "previous-object-versions", "multipart-uploads"}


def lifecycle_managed_rule_prefix(managed_key: str) -> str:
    return f"oci-migrator-{normalize_job_name(managed_key).lower()}-"


def is_managed_lifecycle_rule(managed_key: str, rule) -> bool:
    rule_name = str(getattr(rule, "name", "") or "")
    return rule_name in lifecycle_managed_rule_names(managed_key) or rule_name.startswith(lifecycle_managed_rule_prefix(managed_key))


def normalize_lifecycle_target(value: object) -> str:
    target = str(value or "objects").strip().lower().replace("_", "-")
    if target not in LIFECYCLE_TARGETS:
        raise HTTPException(
            status_code=400,
            detail="Lifecycle rule target must be objects, previous-object-versions, or multipart-uploads.",
        )
    return target


def normalize_lifecycle_action(value: object) -> str:
    action = str(value or "ARCHIVE").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "MOVE_TO_INFREQUENT_ACCESS": "INFREQUENT_ACCESS",
        "INFREQUENTACCESS": "INFREQUENT_ACCESS",
        "MOVE_TO_ARCHIVE": "ARCHIVE",
        "ABORT_MULTIPART": "ABORT",
        "ABORT_MULTIPART_UPLOADS": "ABORT",
    }
    action = aliases.get(action, action)
    if action not in LIFECYCLE_ACTIONS:
        raise HTTPException(status_code=400, detail="Lifecycle rule action is not supported.")
    return action


def normalize_lifecycle_rule_name(value: object, fallback: str, managed_key: str) -> str:
    raw_name = str(value or fallback).strip()
    if len(raw_name) > 255 or any(char in raw_name for char in "\r\n\0"):
        raise HTTPException(status_code=400, detail="Lifecycle rule names must be single-line text up to 255 characters.")
    safe_name = normalize_job_name(raw_name).lower()
    managed_prefix = lifecycle_managed_rule_prefix(managed_key)
    if safe_name.startswith(managed_prefix):
        return safe_name
    return f"{managed_prefix}{safe_name}"


def normalize_lifecycle_rule(rule: object, managed_key: str, index: int, legacy_filters: list[dict] | None = None) -> dict:
    if isinstance(rule, LifecycleRuleConfig):
        rule_data = rule.model_dump()
    elif isinstance(rule, dict):
        rule_data = dict(rule)
    else:
        raise HTTPException(status_code=400, detail="Lifecycle rules must contain rule objects.")

    target = normalize_lifecycle_target(rule_data.get("target", "objects"))
    action = normalize_lifecycle_action(rule_data.get("action", "ARCHIVE"))
    if target not in LIFECYCLE_ACTIONS[action]["targets"]:
        raise HTTPException(
            status_code=400,
            detail=f"{LIFECYCLE_ACTIONS[action]['label']} is not valid for target {target}.",
        )

    days = normalize_lifecycle_days(rule_data.get("days"), "Lifecycle rule days")
    if days is None:
        raise HTTPException(status_code=400, detail="Lifecycle rule days are required.")

    fallback = f"{LIFECYCLE_ACTIONS[action]['suffix']}-{index + 1}"
    filters = normalize_lifecycle_filters(rule_data.get("filters", legacy_filters or []))
    return {
        "name": normalize_lifecycle_rule_name(rule_data.get("name", ""), fallback, managed_key),
        "target": target,
        "action": action,
        "days": days,
        "enabled": bool(rule_data.get("enabled", True)),
        "filters": filters,
    }


def legacy_lifecycle_rules_from_policy(policy_data: dict, managed_key: str, filters: list[dict]) -> list[dict]:
    legacy_specs = [
        ("INFREQUENT_ACCESS", "objects", policy_data.get("infrequent_access_after_days")),
        ("ARCHIVE", "objects", policy_data.get("archive_after_days")),
        ("DELETE", "objects", policy_data.get("delete_after_days")),
        ("DELETE", "previous-object-versions", policy_data.get("previous_versions_delete_after_days")),
    ]
    rules: list[dict] = []
    for action, target, raw_days in legacy_specs:
        days = normalize_lifecycle_days(raw_days, LIFECYCLE_ACTIONS[action]["label"])
        if not days:
            continue
        rules.append(
            normalize_lifecycle_rule(
                {
                    "target": target,
                    "action": action,
                    "days": days,
                    "filters": filters,
                    "name": f"{LIFECYCLE_ACTIONS[action]['suffix']}-{len(rules) + 1}",
                },
                managed_key,
                len(rules),
            )
        )
    return rules


def normalize_lifecycle_policy(policy: LifecyclePolicyConfig | dict | None, managed_key: str | None = None) -> dict:
    if policy is None:
        policy = LifecyclePolicyConfig()
    if isinstance(policy, LifecyclePolicyConfig):
        policy_data = policy.model_dump()
    else:
        policy_data = dict(policy)

    managed_key = managed_key or policy_data.get("managed_key") or BUCKET_SETTINGS_LIFECYCLE_KEY
    filters = normalize_lifecycle_filters(policy_data.get("filters", []), policy_data.get("prefix", ""))
    first_prefix = next((item["value"] for item in filters if item["type"] == "include_prefix"), "")
    raw_rules = policy_data.get("rules", [])
    if raw_rules:
        if not isinstance(raw_rules, list):
            raise HTTPException(status_code=400, detail="Lifecycle rules must be a list.")
        rules = [normalize_lifecycle_rule(rule, managed_key, index, filters) for index, rule in enumerate(raw_rules)]
    else:
        rules = legacy_lifecycle_rules_from_policy(policy_data, managed_key, filters)

    if len(rules) > 100:
        raise HTTPException(status_code=400, detail="A lifecycle policy can have at most 100 managed rules.")
    rule_names = [rule["name"] for rule in rules]
    if len(set(rule_names)) != len(rule_names):
        raise HTTPException(status_code=400, detail="Lifecycle rule names must be unique.")

    legacy_fields = {
        "infrequent_access_after_days": None,
        "archive_after_days": None,
        "delete_after_days": None,
        "previous_versions_delete_after_days": None,
    }
    for rule in rules:
        if rule["target"] == "objects" and rule["action"] == "INFREQUENT_ACCESS":
            legacy_fields["infrequent_access_after_days"] = legacy_fields["infrequent_access_after_days"] or rule["days"]
        elif rule["target"] == "objects" and rule["action"] == "ARCHIVE":
            legacy_fields["archive_after_days"] = legacy_fields["archive_after_days"] or rule["days"]
        elif rule["target"] == "objects" and rule["action"] == "DELETE":
            legacy_fields["delete_after_days"] = legacy_fields["delete_after_days"] or rule["days"]
        elif rule["target"] == "previous-object-versions" and rule["action"] == "DELETE":
            legacy_fields["previous_versions_delete_after_days"] = (
                legacy_fields["previous_versions_delete_after_days"] or rule["days"]
            )

    normalized = {
        "enabled": bool(policy_data.get("enabled", False)),
        "prefix": first_prefix,
        "filters": filters,
        "rules": rules,
        **legacy_fields,
    }

    if normalized["enabled"] and not rules:
        raise HTTPException(status_code=400, detail="Add at least one lifecycle rule or disable lifecycle management.")

    return normalized


def lifecycle_managed_rule_names(job_name: str) -> set[str]:
    safe_job_name = normalize_job_name(job_name).lower()
    return {
        f"oci-migrator-{safe_job_name}-infrequent-access",
        f"oci-migrator-{safe_job_name}-archive",
        f"oci-migrator-{safe_job_name}-delete",
        f"oci-migrator-{safe_job_name}-delete-previous",
    }


BUCKET_SETTINGS_LIFECYCLE_KEY = "bucket-settings"


def object_storage_context(profile_name: str):
    config = oci.config.from_file(CONFIG_PATH, profile_name)
    client = oci.object_storage.ObjectStorageClient(config)
    namespace = client.get_namespace().data
    return config, client, namespace


def validate_destination_bucket(profile_name: str, destination: str) -> str:
    profile_name = str(profile_name or "").strip()
    if not profile_name:
        raise HTTPException(status_code=400, detail="Destination OCI profile is required.")

    bucket_name = destination_bucket_name(destination)
    try:
        _, client, namespace = object_storage_context(profile_name)
        client.get_bucket(namespace, bucket_name)
    except HTTPException:
        raise
    except (
        oci.exceptions.ConfigFileNotFound,
        oci.exceptions.InvalidConfig,
        oci.exceptions.ProfileNotFound,
    ) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"Destination OCI profile '{profile_name}' is missing or invalid.",
                "operation": "Validate backup destination",
                "error_type": exc.__class__.__name__,
                "hint": "Open Credentials and verify the destination OCI profile before saving the job.",
            },
        ) from exc
    except oci.exceptions.ServiceError as exc:
        if exc.status == 404:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"Destination bucket '{bucket_name}' was not found or is not accessible.",
                    "operation": "Validate backup destination",
                    "status": exc.status,
                    "code": exc.code,
                    "opc_request_id": getattr(exc, "request_id", None),
                    "hint": "Select an existing bucket in OCI Object Storage and try again.",
                },
            ) from exc
        if exc.status in {401, 403}:
            raise HTTPException(
                status_code=403,
                detail={
                    "message": f"OCI denied access to destination bucket '{bucket_name}'.",
                    "operation": "Validate backup destination",
                    "status": exc.status,
                    "code": exc.code,
                    "opc_request_id": getattr(exc, "request_id", None),
                    "hint": "Check the OCI profile, compartment, and Object Storage IAM permissions.",
                },
            ) from exc
        raise_operation_error(
            502,
            "Validate backup destination",
            exc,
            "Check OCI connectivity and Object Storage permissions, then try again.",
        )
    except Exception as exc:
        raise_operation_error(
            502,
            "Validate backup destination",
            exc,
            "Check the destination OCI profile and Object Storage connectivity.",
        )

    return bucket_name


def get_lifecycle_rules(client, namespace: str, bucket_name: str) -> list:
    try:
        policy = client.get_object_lifecycle_policy(namespace, bucket_name).data
        return list(getattr(policy, "items", None) or [])
    except oci.exceptions.ServiceError as exc:
        if exc.status == 404:
            return []
        raise


def get_bucket_with_auto_tiering(client, namespace: str, bucket_name: str):
    return client.get_bucket(namespace, bucket_name, fields=["autoTiering"]).data


def build_lifecycle_object_filter(filters: list[dict]):
    inclusion_prefixes = [item["value"] for item in filters if item["type"] == "include_prefix"]
    inclusion_patterns = [item["value"] for item in filters if item["type"] == "include_pattern"]
    exclusion_patterns = [item["value"] for item in filters if item["type"] == "exclude_pattern"]
    if not inclusion_prefixes and not inclusion_patterns and not exclusion_patterns:
        return None
    return oci.object_storage.models.ObjectNameFilter(
        inclusion_prefixes=inclusion_prefixes,
        inclusion_patterns=inclusion_patterns,
        exclusion_patterns=exclusion_patterns,
    )


def build_lifecycle_rule(name: str, target: str, action: str, days: int, filters: list[dict], enabled: bool = True):
    object_filter = None
    if target in ("objects", "object-versions", "previous-object-versions"):
        object_filter = build_lifecycle_object_filter(filters)
    return oci.object_storage.models.ObjectLifecycleRule(
        name=name,
        target=target,
        action=action,
        time_amount=days,
        time_unit=oci.object_storage.models.ObjectLifecycleRule.TIME_UNIT_DAYS,
        is_enabled=enabled,
        object_name_filter=object_filter,
    )


def desired_lifecycle_rules(job_name: str, policy: dict) -> list:
    rules = []
    for rule in policy.get("rules", []):
        rules.append(
            build_lifecycle_rule(
                rule["name"],
                rule["target"],
                rule["action"],
                rule["days"],
                list(rule.get("filters", [])),
                bool(rule.get("enabled", True)),
            )
        )
    return rules


def put_lifecycle_rules(client, namespace: str, bucket_name: str, rules: list) -> None:
    details = oci.object_storage.models.PutObjectLifecyclePolicyDetails(items=rules)
    client.put_object_lifecycle_policy(namespace, bucket_name, details)


def apply_job_lifecycle_policy(job_name: str, profile_name: str, destination: str, policy: dict) -> int:
    bucket_name = destination_bucket_name(destination)
    _, client, namespace = object_storage_context(profile_name)
    bucket = get_bucket_with_auto_tiering(client, namespace, bucket_name)
    if policy.get("enabled") and any(rule.get("action") == "INFREQUENT_ACCESS" for rule in policy.get("rules", [])):
        auto_tiering = normalized_bucket_auto_tiering(getattr(bucket, "auto_tiering", ""))
        if auto_tiering == "InfrequentAccess":
            raise HTTPException(
                status_code=400,
                detail="OCI does not allow an Infrequent Access lifecycle rule while Auto-Tiering is enabled on the bucket.",
            )
    existing_rules = [
        rule
        for rule in get_lifecycle_rules(client, namespace, bucket_name)
        if not is_managed_lifecycle_rule(job_name, rule)
    ]
    new_rules = desired_lifecycle_rules(job_name, policy) if policy.get("enabled") else []
    put_lifecycle_rules(client, namespace, bucket_name, [*existing_rules, *new_rules])
    return len(new_rules)


def remove_job_lifecycle_policy(job_name: str, profile_name: str, destination: str) -> None:
    bucket_name = destination_bucket_name(destination)
    _, client, namespace = object_storage_context(profile_name)
    existing_rules = get_lifecycle_rules(client, namespace, bucket_name)
    next_rules = [rule for rule in existing_rules if not is_managed_lifecycle_rule(job_name, rule)]
    if len(next_rules) != len(existing_rules):
        put_lifecycle_rules(client, namespace, bucket_name, next_rules)


def lifecycle_filters_from_rule(rule) -> list[dict]:
    object_filter = getattr(rule, "object_name_filter", None)
    if not object_filter:
        return []

    filters: list[dict] = []
    for value in getattr(object_filter, "inclusion_prefixes", None) or []:
        filters.append({"type": "include_prefix", "value": value})
    for value in getattr(object_filter, "inclusion_patterns", None) or []:
        filters.append({"type": "include_pattern", "value": value})
    for value in getattr(object_filter, "exclusion_patterns", None) or []:
        filters.append({"type": "exclude_pattern", "value": value})
    return filters


def managed_lifecycle_policy_from_rules(managed_key: str, rules: list) -> dict:
    policy = {
        "enabled": False,
        "prefix": "",
        "filters": [],
        "rules": [],
        "infrequent_access_after_days": None,
        "archive_after_days": None,
        "delete_after_days": None,
        "previous_versions_delete_after_days": None,
    }

    for rule in rules:
        if not is_managed_lifecycle_rule(managed_key, rule):
            continue
        target = normalize_lifecycle_target(getattr(rule, "target", "objects"))
        action = normalize_lifecycle_action(getattr(rule, "action", "ARCHIVE"))
        days = getattr(rule, "time_amount", None)
        filters = lifecycle_filters_from_rule(rule)
        policy["enabled"] = True
        policy["rules"].append(
            {
                "name": getattr(rule, "name", ""),
                "target": target,
                "action": action,
                "days": days,
                "enabled": bool(getattr(rule, "is_enabled", True)),
                "filters": filters,
            }
        )
        if not policy["filters"]:
            policy["filters"] = filters
            first_prefix = next((item["value"] for item in policy["filters"] if item["type"] == "include_prefix"), "")
            policy["prefix"] = first_prefix
        if target == "objects" and action == "INFREQUENT_ACCESS":
            policy["infrequent_access_after_days"] = policy["infrequent_access_after_days"] or days
        elif target == "objects" and action == "ARCHIVE":
            policy["archive_after_days"] = policy["archive_after_days"] or days
        elif target == "objects" and action == "DELETE":
            policy["delete_after_days"] = policy["delete_after_days"] or days
        elif target == "previous-object-versions" and action == "DELETE":
            policy["previous_versions_delete_after_days"] = policy["previous_versions_delete_after_days"] or days

    return policy


def retention_rule_summary(rule) -> dict:
    duration = getattr(rule, "duration", None)
    return {
        "id": getattr(rule, "id", ""),
        "display_name": getattr(rule, "display_name", ""),
        "duration": oci.util.to_dict(duration) if duration else None,
        "time_rule_locked": getattr(rule, "time_rule_locked", None).isoformat()
        if getattr(rule, "time_rule_locked", None)
        else "",
    }


def job_log_helper_command() -> list[str]:
    if not JOB_LOG_HELPER.is_file():
        raise HTTPException(
            status_code=503,
            detail="Job log settings helper is not installed. Rerun ./install.sh and try again.",
        )

    if os.geteuid() == 0:
        return [str(JOB_LOG_HELPER)]

    sudo_path = shutil.which("sudo")
    if not sudo_path:
        raise HTTPException(
            status_code=503,
            detail="sudo is required for job log settings. Rerun ./install.sh and try again.",
        )
    return [sudo_path, "-n", str(JOB_LOG_HELPER)]


def time_sync_helper_command() -> list[str]:
    if not TIME_SYNC_HELPER.is_file():
        raise HTTPException(
            status_code=503,
            detail="Time sync settings helper is not installed. Rerun ./install.sh and try again.",
        )

    if os.geteuid() == 0:
        return [str(TIME_SYNC_HELPER)]

    sudo_path = shutil.which("sudo")
    if not sudo_path:
        raise HTTPException(
            status_code=503,
            detail="sudo is required for time sync settings. Rerun ./install.sh and try again.",
        )
    return [sudo_path, "-n", str(TIME_SYNC_HELPER)]


def network_helper_command() -> list[str]:
    if not NETWORK_HELPER.is_file():
        raise HTTPException(
            status_code=503,
            detail="Network settings helper is not installed. Rerun ./install.sh and try again.",
        )

    if os.geteuid() == 0:
        return [str(NETWORK_HELPER)]

    sudo_path = shutil.which("sudo")
    if not sudo_path:
        raise HTTPException(
            status_code=503,
            detail="sudo is required for network settings. Rerun ./install.sh and try again.",
        )
    return [sudo_path, "-n", str(NETWORK_HELPER)]


def run_network_helper(args: list[str], timeout: int = 30) -> dict:
    command = network_helper_command() + args
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(truncate_text(result.stderr or result.stdout or f"helper exited with code {result.returncode}"))
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        except (json.JSONDecodeError, IndexError) as exc:
            raise RuntimeError(f"Network helper returned invalid JSON: {truncate_text(result.stdout, 600)}") from exc
        payload["helper_installed"] = True
        return payload
    except subprocess.TimeoutExpired as exc:
        raise_operation_error(
            504,
            "Update network settings",
            exc,
            "The network helper took too long. The automatic rollback timer will restore an unconfirmed change.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise_operation_error(
            500,
            "Update network settings",
            exc,
            "Check Netplan, systemd, and the installed network helper on the server.",
        )


def current_network_settings() -> dict:
    if not NETWORK_HELPER.is_file():
        return {
            "supported": False,
            "helper_installed": False,
            "mode": "dhcp",
            "interfaces": [],
            "pending": None,
        }
    return run_network_helper(["status"], timeout=15)


def apply_job_log_rotation_settings(max_size: str, retention_days: int) -> dict:
    run_user = pwd.getpwuid(os.geteuid()).pw_name
    command = job_log_helper_command() + [
        "configure",
        "--job-log-dir",
        str(JOB_LOG_DIR),
        "--max-size",
        max_size,
        "--retention-days",
        str(retention_days),
        "--run-user",
        run_user,
        "--logrotate-file",
        str(JOB_LOGROTATE_FILE),
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(truncate_text(result.stderr or result.stdout or f"helper exited with code {result.returncode}"))

        try:
            return json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        except (json.JSONDecodeError, IndexError):
            return {"raw_output": truncate_text(result.stdout, 600)}
    except subprocess.TimeoutExpired as exc:
        raise_operation_error(
            504,
            "Update job log rotation settings",
            exc,
            "The logrotate helper took too long. Check sudoers and logrotate on the server.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise_operation_error(
            500,
            "Update job log rotation settings",
            exc,
            "Check that install.sh installed /usr/local/sbin/oci-migrator-job-log and sudoers access for the service user.",
        )


def apply_time_sync_settings(timezone_value: str, ntp_servers: str) -> dict:
    command = time_sync_helper_command() + [
        "configure",
        "--timezone",
        timezone_value,
        "--ntp-servers",
        ntp_servers,
        "--timesyncd-conf",
        str(TIMESYNCD_CONF),
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(truncate_text(result.stderr or result.stdout or f"helper exited with code {result.returncode}"))

        try:
            return json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        except (json.JSONDecodeError, IndexError):
            return {"raw_output": truncate_text(result.stdout, 600)}
    except subprocess.TimeoutExpired as exc:
        raise_operation_error(
            504,
            "Update time sync settings",
            exc,
            "The time sync helper took too long. Check sudoers, timedatectl, and systemd-timesyncd on the server.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise_operation_error(
            500,
            "Update time sync settings",
            exc,
            "Check that install.sh installed /usr/local/sbin/oci-migrator-time-sync and sudoers access for the service user.",
        )


def mask_remote_url(value: str) -> str:
    return re.sub(r"(https?://)([^/@]+)@", r"\1***@", value or "")


def git_command(args: list[str], timeout: int = 10) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(truncate_text(result.stderr or result.stdout or f"git exited with code {result.returncode}"))
        return result.stdout.strip()
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git command timed out after {timeout}s") from exc


def safe_git_command(args: list[str], timeout: int = 10) -> str:
    try:
        return git_command(args, timeout=timeout)
    except Exception:
        return ""


def current_git_info() -> dict:
    commit = safe_git_command(["rev-parse", "HEAD"])
    branch = safe_git_command(["rev-parse", "--abbrev-ref", "HEAD"])
    remote_url = safe_git_command(["config", "--get", "remote.origin.url"])
    if branch == "HEAD":
        branch = ""

    return {
        "branch": branch,
        "current_commit": commit,
        "current_short": commit[:7] if commit else "",
        "remote_url": mask_remote_url(remote_url),
    }


def latest_git_info() -> dict:
    info = current_git_info()
    branch = info.get("branch") or "main"
    try:
        raw_output = git_command(["ls-remote", "origin", branch], timeout=20)
        first_line = raw_output.splitlines()[0] if raw_output else ""
        latest_commit = first_line.split()[0] if first_line else ""
        if not latest_commit:
            raise RuntimeError(f"No commit found for origin/{branch}.")
    except Exception as exc:
        raise_operation_error(
            502,
            "Check for updates",
            exc,
            "Confirm that the server has outbound GitHub access and that origin points to the project repository.",
        )

    return {
        **info,
        "branch": branch,
        "latest_commit": latest_commit,
        "latest_short": latest_commit[:7],
        "up_to_date": bool(info.get("current_commit")) and info.get("current_commit") == latest_commit,
    }


def read_upgrade_status_file() -> dict:
    default_status = {
        "status": "idle",
        "message": "No upgrade has run yet.",
        "started_at": None,
        "finished_at": None,
        "updated_at": None,
    }
    try:
        if not UPGRADE_STATUS_FILE.exists():
            return default_status
        with open(UPGRADE_STATUS_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return default_status
        return {**default_status, **payload}
    except (OSError, json.JSONDecodeError):
        return {**default_status, "status": "warn", "message": "Upgrade status file could not be read."}


def upgrade_status_payload() -> dict:
    payload = read_upgrade_status_file()
    payload.update(current_git_info())
    payload["helper_installed"] = UPGRADE_HELPER.is_file()
    payload["log_file"] = str(UPGRADE_LOG_FILE)
    return payload


def upgrade_helper_command() -> list[str]:
    if not UPGRADE_HELPER.is_file():
        raise HTTPException(
            status_code=503,
            detail="Upgrade helper is not installed. Rerun ./install.sh and try again.",
        )

    if os.geteuid() == 0:
        return [str(UPGRADE_HELPER), "start"]

    sudo_path = shutil.which("sudo")
    if not sudo_path:
        raise HTTPException(
            status_code=503,
            detail="sudo is required for controlled upgrades. Rerun ./install.sh and try again.",
        )
    return [sudo_path, "-n", str(UPGRADE_HELPER), "start"]


def uninstall_helper_command(purge_local_backups: bool = False) -> list[str]:
    if not UNINSTALL_HELPER.is_file():
        raise HTTPException(
            status_code=503,
            detail="Uninstall helper is not installed. Rerun ./install.sh and try again.",
        )

    command = [str(UNINSTALL_HELPER), "schedule"]
    if purge_local_backups:
        command.append("--purge-local-data")

    if os.geteuid() == 0:
        return command

    sudo_path = shutil.which("sudo")
    if not sudo_path:
        raise HTTPException(
            status_code=503,
            detail="sudo is required for controlled uninstall. Rerun ./install.sh and try again.",
        )
    return [sudo_path, "-n", *command]


def upgrade_process_is_running() -> bool:
    pid_file = UPGRADE_LOCK_DIR / "pid"
    try:
        if pid_file.is_file():
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            return True
    except (ValueError, OSError):
        return False

    return False


def history_status_for_api(run: dict) -> str:
    status_map = {
        "queued": "PENDING",
        "running": "PROGRESS",
        "retrying": "PROGRESS",
        "success": "SUCCESS",
        "failed": "FAILURE",
        "timeout": "FAILURE",
    }
    return status_map.get(str(run.get("status", "")).lower(), "PENDING")


def add_file_to_zip(archive: zipfile.ZipFile, manifest: dict, source_path: str, archive_name: str) -> None:
    expanded = os.path.expanduser(source_path)
    if os.path.isfile(expanded):
        archive.write(expanded, archive_name)
        manifest["included"].append(
            {
                "name": archive_name,
                "size_bytes": os.path.getsize(expanded),
                "modified_at": datetime.fromtimestamp(
                    os.path.getmtime(expanded), timezone.utc
                ).isoformat().replace("+00:00", "Z"),
            }
        )
    else:
        manifest["missing"].append(archive_name)


def runtime_config_import_targets() -> dict[str, Path]:
    return {
        "runtime/.oci-migrator.env": Path(ENV_FILE_PATH).expanduser(),
        "oci/config": Path(CONFIG_PATH).expanduser(),
        "oci/jobs.json": Path(JOBS_FILE).expanduser(),
        "oci/job_history.json": Path(JOB_HISTORY_FILE).expanduser(),
        "rclone/rclone.conf": Path(RCLONE_CONF).expanduser(),
    }


def build_runtime_config_archive() -> tuple[io.BytesIO, str]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_buffer = io.BytesIO()
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "included": [],
        "missing": [],
        "notes": [
            "This archive may contain secrets such as API keys, rclone credentials, and the admin password hash.",
            "Store it securely and delete old copies when they are no longer needed.",
        ],
    }

    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        add_file_to_zip(archive, manifest, ENV_FILE_PATH, "runtime/.oci-migrator.env")
        add_file_to_zip(archive, manifest, CONFIG_PATH, "oci/config")
        add_file_to_zip(archive, manifest, JOBS_FILE, "oci/jobs.json")
        add_file_to_zip(archive, manifest, JOB_HISTORY_FILE, "oci/job_history.json")
        add_file_to_zip(archive, manifest, RCLONE_CONF, "rclone/rclone.conf")

        added_paths = {
            os.path.realpath(path)
            for path in (ENV_FILE_PATH, CONFIG_PATH, JOBS_FILE, JOB_HISTORY_FILE, RCLONE_CONF)
        }

        if os.path.isfile(CONFIG_PATH):
            parser = configparser.ConfigParser()
            parser.read(CONFIG_PATH)
            for section in parser.sections():
                key_file = parser.get(section, "key_file", fallback="")
                if key_file and os.path.isfile(os.path.expanduser(key_file)):
                    real_path = os.path.realpath(os.path.expanduser(key_file))
                    if real_path not in added_paths:
                        archive_name = (
                            f"oci/keys/{sanitize_filename(section, 'profile')}_"
                            f"{sanitize_filename(os.path.basename(key_file), 'api_key.pem')}"
                        )
                        add_file_to_zip(archive, manifest, key_file, archive_name)
                        added_paths.add(real_path)

        if os.path.isfile(RCLONE_CONF):
            parser = configparser.ConfigParser()
            parser.read(RCLONE_CONF)
            for section in parser.sections():
                service_account_file = parser.get(section, "service_account_file", fallback="")
                if service_account_file and os.path.isfile(os.path.expanduser(service_account_file)):
                    real_path = os.path.realpath(os.path.expanduser(service_account_file))
                    if real_path not in added_paths:
                        archive_name = (
                            f"rclone/service-accounts/{sanitize_filename(section, 'remote')}_"
                            f"{sanitize_filename(os.path.basename(service_account_file), 'service_account.json')}"
                        )
                        add_file_to_zip(archive, manifest, service_account_file, archive_name)
                        added_paths.add(real_path)

        archive.writestr("manifest.json", json.dumps(manifest, indent=2))

    archive_buffer.seek(0)
    return archive_buffer, timestamp


def validate_text_file(content: bytes, archive_name: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{archive_name} must be UTF-8 text.") from exc


def validate_runtime_env_content(content: bytes, archive_name: str) -> None:
    text = validate_text_file(content, archive_name)
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise HTTPException(status_code=400, detail=f"{archive_name}:{line_number} is not a KEY=value line.")
        key = line.split("=", 1)[0].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise HTTPException(status_code=400, detail=f"{archive_name}:{line_number} has an invalid env key.")


def validate_ini_content(content: bytes, archive_name: str) -> None:
    text = validate_text_file(content, archive_name)
    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise HTTPException(status_code=400, detail=f"{archive_name} is not a valid INI config.") from exc


def validate_json_content(content: bytes, archive_name: str):
    text = validate_text_file(content, archive_name)
    try:
        return json.loads(text or "null")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{archive_name} is not valid JSON.") from exc


def validate_runtime_import_content(archive_name: str, content: bytes) -> None:
    if archive_name == "runtime/.oci-migrator.env":
        validate_runtime_env_content(content, archive_name)
    elif archive_name in {"oci/config", "rclone/rclone.conf"}:
        validate_ini_content(content, archive_name)
    elif archive_name == "oci/jobs.json":
        data = validate_json_content(content, archive_name)
        if not isinstance(data, list):
            raise HTTPException(status_code=400, detail="oci/jobs.json must contain a JSON list.")
    elif archive_name == "oci/job_history.json":
        data = validate_json_content(content, archive_name)
        if not isinstance(data, dict) or not isinstance(data.get("runs", []), list):
            raise HTTPException(status_code=400, detail="oci/job_history.json must contain a JSON object with a runs list.")


def normalize_zip_member_name(info: zipfile.ZipInfo) -> str:
    raw_name = info.filename
    if "\\" in raw_name:
        raise HTTPException(status_code=400, detail=f"Unsupported ZIP path: {raw_name}")

    path = PurePosixPath(raw_name)
    if path.is_absolute():
        raise HTTPException(status_code=400, detail=f"Absolute ZIP paths are not allowed: {raw_name}")
    if any(part in {"", ".", ".."} or ":" in part for part in path.parts):
        raise HTTPException(status_code=400, detail=f"Unsafe ZIP path: {raw_name}")

    return path.as_posix()


def runtime_import_member_is_allowed(archive_name: str) -> bool:
    return (
        archive_name == "manifest.json"
        or archive_name in runtime_config_import_targets()
        or archive_name.startswith("oci/keys/")
        or archive_name.startswith("rclone/service-accounts/")
    )


def parse_runtime_import_zip(payload: bytes) -> tuple[dict[str, bytes], dict[str, bytes], dict[str, bytes]]:
    config_files: dict[str, bytes] = {}
    oci_keys: dict[str, bytes] = {}
    service_accounts: dict[str, bytes] = {}
    seen: set[str] = set()
    total_uncompressed = 0

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for info in archive.infolist():
                archive_name = normalize_zip_member_name(info)
                if info.is_dir():
                    continue
                if archive_name in seen:
                    raise HTTPException(status_code=400, detail=f"Duplicate ZIP entry: {archive_name}")
                seen.add(archive_name)
                if info.flag_bits & 0x1:
                    raise HTTPException(status_code=400, detail=f"Encrypted ZIP entries are not supported: {archive_name}")
                if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                    raise HTTPException(status_code=400, detail=f"Symlinks are not allowed in runtime backups: {archive_name}")
                if not runtime_import_member_is_allowed(archive_name):
                    raise HTTPException(status_code=400, detail=f"Unsupported file in runtime backup: {archive_name}")

                total_uncompressed += info.file_size
                if total_uncompressed > RUNTIME_CONFIG_IMPORT_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="Runtime backup is too large after extraction.")

                if archive_name == "manifest.json":
                    continue

                content = archive.read(info)
                validate_runtime_import_content(archive_name, content)
                if archive_name in runtime_config_import_targets():
                    config_files[archive_name] = content
                elif archive_name.startswith("oci/keys/"):
                    oci_keys[archive_name] = content
                elif archive_name.startswith("rclone/service-accounts/"):
                    service_accounts[archive_name] = content
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid ZIP archive.") from exc

    if not (config_files or oci_keys or service_accounts):
        raise HTTPException(status_code=400, detail="No supported runtime config files were found in the ZIP archive.")

    return config_files, oci_keys, service_accounts


def write_bytes_atomically(path: Path, content: bytes, mode: int = 0o600) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_path = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(file_descriptor, "wb") as temp_file:
            temp_file.write(content)
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def runtime_secret_destination(root: Path, archive_name: str, default_name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(PurePosixPath(archive_name).name, default_name)
    target = (root / safe_name).resolve()
    if root.resolve() not in target.parents:
        raise HTTPException(status_code=400, detail=f"Unsafe runtime backup filename: {archive_name}")
    return target


def rewrite_oci_key_file_paths(imported_keys: dict[str, Path]) -> bool:
    if not imported_keys or not os.path.isfile(CONFIG_PATH):
        return False

    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH)
    changed = False
    for section in parser.sections():
        key_file = parser.get(section, "key_file", fallback="")
        if not key_file:
            continue
        expected_archive_name = (
            f"oci/keys/{sanitize_filename(section, 'profile')}_"
            f"{sanitize_filename(os.path.basename(key_file), 'api_key.pem')}"
        )
        imported_path = imported_keys.get(expected_archive_name)
        if imported_path:
            parser.set(section, "key_file", str(imported_path))
            changed = True

    if changed:
        write_ini_atomically(parser, CONFIG_PATH)
    return changed


def rewrite_rclone_service_account_paths(imported_accounts: dict[str, Path]) -> bool:
    if not imported_accounts or not os.path.isfile(RCLONE_CONF):
        return False

    parser = configparser.ConfigParser()
    parser.read(RCLONE_CONF)
    changed = False
    for section in parser.sections():
        service_account_file = parser.get(section, "service_account_file", fallback="")
        if not service_account_file:
            continue
        expected_archive_name = (
            f"rclone/service-accounts/{sanitize_filename(section, 'remote')}_"
            f"{sanitize_filename(os.path.basename(service_account_file), 'service_account.json')}"
        )
        imported_path = imported_accounts.get(expected_archive_name)
        if imported_path:
            parser.set(section, "service_account_file", str(imported_path))
            changed = True

    if changed:
        write_ini_atomically(parser, RCLONE_CONF)
    return changed


def save_pre_restore_runtime_backup() -> Path:
    RUNTIME_RESTORE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RUNTIME_RESTORE_BACKUP_DIR, 0o700)
    archive_buffer, timestamp = build_runtime_config_archive()
    backup_path = RUNTIME_RESTORE_BACKUP_DIR / f"pre-restore-{timestamp}.zip"
    with open(backup_path, "wb") as backup_file:
        backup_file.write(archive_buffer.getvalue())
    os.chmod(backup_path, 0o600)
    return backup_path


def restore_runtime_config_archive(payload: bytes) -> dict:
    config_files, oci_keys, service_accounts = parse_runtime_import_zip(payload)
    pre_restore_backup = save_pre_restore_runtime_backup()
    targets = runtime_config_import_targets()
    restored: list[dict[str, str]] = []
    warnings: list[str] = []
    imported_key_paths: dict[str, Path] = {}
    imported_account_paths: dict[str, Path] = {}

    with CONFIG_LOCK:
        for archive_name in ("runtime/.oci-migrator.env", "oci/config"):
            content = config_files.get(archive_name)
            if content is None:
                continue
            write_bytes_atomically(targets[archive_name], content)
            restored.append({"name": archive_name, "target": str(targets[archive_name])})
            if archive_name == "runtime/.oci-migrator.env":
                with _CONFIG_CACHE_LOCK:
                    _CONFIG_CACHE["mtime"] = "__not_loaded__"

        key_root = Path(OCI_DIR).expanduser() / "keys"
        for archive_name, content in sorted(oci_keys.items()):
            target = runtime_secret_destination(key_root, archive_name, "api_key.pem")
            write_bytes_atomically(target, content)
            imported_key_paths[archive_name] = target
            restored.append({"name": archive_name, "target": str(target)})

        if rewrite_oci_key_file_paths(imported_key_paths):
            restored.append({"name": "oci/config key_file paths", "target": CONFIG_PATH})

    with RCLONE_LOCK:
        content = config_files.get("rclone/rclone.conf")
        if content is not None:
            write_bytes_atomically(targets["rclone/rclone.conf"], content)
            restored.append({"name": "rclone/rclone.conf", "target": str(targets["rclone/rclone.conf"])})

        account_root = Path(RCLONE_CONF).expanduser().parent / "service-accounts"
        for archive_name, content in sorted(service_accounts.items()):
            target = runtime_secret_destination(account_root, archive_name, "service_account.json")
            write_bytes_atomically(target, content)
            imported_account_paths[archive_name] = target
            restored.append({"name": archive_name, "target": str(target)})

        if rewrite_rclone_service_account_paths(imported_account_paths):
            restored.append({"name": "rclone/rclone.conf service_account_file paths", "target": RCLONE_CONF})

    with JOBS_LOCK:
        content = config_files.get("oci/jobs.json")
        if content is not None:
            write_bytes_atomically(targets["oci/jobs.json"], content)
            restored.append({"name": "oci/jobs.json", "target": str(targets["oci/jobs.json"])})

    content = config_files.get("oci/job_history.json")
    if content is not None:
        with locked_history_file():
            write_bytes_atomically(targets["oci/job_history.json"], content)
        restored.append({"name": "oci/job_history.json", "target": str(targets["oci/job_history.json"])})

    try:
        settings = current_job_log_settings()
        apply_job_log_rotation_settings(settings["max_size"], settings["retention_days"])
    except Exception as exc:
        warnings.append(f"Job log rotation settings were restored but could not be applied automatically: {truncate_text(str(exc), 300)}")

    with SESSION_LOCK:
        REVOKED_SESSIONS.clear()

    new_config = get_runtime_config()
    session_payload = {}
    if new_config.get("admin_password_hash"):
        session_payload = {
            "token": create_session_token(),
            "token_type": "bearer",
            "username": str(new_config.get("admin_username", "admin")),
            "expires_in": int(new_config.get("session_ttl_seconds", 43200)),
        }

    return {
        "message": "Runtime config restored.",
        "restored": restored,
        "restored_count": len(restored),
        "pre_restore_backup": str(pre_restore_backup),
        "warnings": warnings,
        **session_payload,
    }


def sync_oci_to_rclone(profile_name, region, storage_compartment_ocid):
    try:
        config = oci.config.from_file(CONFIG_PATH, profile_name)
        os_client = oci.object_storage.ObjectStorageClient(config)
        namespace = os_client.get_namespace().data
    except Exception as e:
        logger.warning("Failed to look up namespace for profile '%s': %s", profile_name, e)
        namespace = "ERROR_FETCHING_NAMESPACE"

    r_parser = configparser.ConfigParser()
    if os.path.exists(RCLONE_CONF):
        r_parser.read(RCLONE_CONF)
    
    section = f"{profile_name}_rclone"
    if not r_parser.has_section(section):
        r_parser.add_section(section)
    
    r_parser.set(section, 'type', 'oracleobjectstorage')
    r_parser.set(section, 'provider', 'user_principal_auth')
    r_parser.set(section, 'namespace', namespace)
    r_parser.set(section, 'compartment', storage_compartment_ocid)
    r_parser.set(section, 'region', region)
    r_parser.set(section, 'config_file', CONFIG_PATH)
    r_parser.set(section, 'config_profile', profile_name)

    with RCLONE_LOCK:
        write_ini_atomically(r_parser, RCLONE_CONF)


# --- 0. Admin Auth ---
@app.post("/auth/login")
async def login(data: LoginRequest):
    config = get_runtime_config()
    admin_username = str(config.get("admin_username", "admin"))
    admin_password_hash = str(config.get("admin_password_hash", ""))

    if not admin_password_hash:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin password is not configured. Run install.sh with --admin-password or --prompt-admin-password.",
        )

    if data.username != admin_username or not verify_password(data.password, admin_password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password.")

    token = create_session_token()
    return {
        "token": token,
        "token_type": "bearer",
        "username": admin_username,
        "expires_in": int(config.get("session_ttl_seconds", 43200)),
    }


@app.post("/auth/logout")
async def logout(authorization: Optional[str] = Header(default=None, alias="Authorization")):
    token = bearer_token_from_header(authorization)
    if token:
        invalidate_session_token(token)
    return {"message": "Logged out"}


@app.post("/auth/change-password")
async def change_password(data: ChangePasswordRequest):
    if len(data.new_password) < 12:
        raise HTTPException(status_code=400, detail="New password must be at least 12 characters.")

    config = get_runtime_config()
    admin_password_hash = str(config.get("admin_password_hash", ""))
    if not admin_password_hash or not verify_password(data.current_password, admin_password_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Current password is incorrect.")

    new_hash = hash_password(data.new_password)
    _write_env_values(
        ENV_FILE_PATH,
        {
            "OCI_MIGRATOR_ADMIN_USERNAME": str(config.get("admin_username", "admin")),
            "OCI_MIGRATOR_ADMIN_PASSWORD_HASH": new_hash,
        },
    )

    with SESSION_LOCK:
        REVOKED_SESSIONS.clear()

    return {
        "message": "Admin password changed.",
        "token": create_session_token(),
        "token_type": "bearer",
        "username": str(config.get("admin_username", "admin")),
        "expires_in": int(config.get("session_ttl_seconds", 43200)),
    }


# --- 0.5. Operations ---
def build_health_payload() -> dict:
    runtime_config = get_runtime_config()
    admin_password_configured = bool(runtime_config.get("admin_password_hash"))
    env_file_exists = os.path.isfile(ENV_FILE_PATH)
    oci_config_exists = os.path.isfile(CONFIG_PATH)
    rclone_config_exists = os.path.isfile(RCLONE_CONF)
    rclone_installed = bool(shutil.which("rclone"))
    frontend_build_exists = (FRONTEND_DIST_DIR / "index.html").is_file()
    job_log_dir_exists = JOB_LOG_DIR.is_dir()
    upgrade_helper_exists = UPGRADE_HELPER.is_file()
    uninstall_helper_exists = UNINSTALL_HELPER.is_file()
    local_disk = current_local_disk_settings()
    expected_timezone, configured_ntp_servers = configured_time_settings()
    configured_timezone = timedatectl_value("Timezone")
    ntp_synchronized = timedatectl_value("NTPSynchronized")
    ntp_service_active = timedatectl_value("NTP")

    checks = {
        "admin_password": health_check_item(
            "ok" if admin_password_configured else "error",
            "Admin password hash is configured." if admin_password_configured else "Admin password hash is missing.",
        ),
        "env_file": health_check_item(
            "ok" if env_file_exists else "warn",
            "Runtime env file exists." if env_file_exists else "Runtime env file not found; environment variables may still be used.",
        ),
        "oci_config": health_check_item(
            "ok" if oci_config_exists else "warn",
            "OCI config exists." if oci_config_exists else "No OCI config found yet.",
        ),
        "rclone_config": health_check_item(
            "ok" if rclone_config_exists else "warn",
            "rclone config exists." if rclone_config_exists else "No rclone config found yet.",
        ),
        "rclone_binary": health_check_item(
            "ok" if rclone_installed else "error",
            "rclone is installed." if rclone_installed else "rclone command is not available.",
        ),
        "frontend_build": health_check_item(
            "ok" if frontend_build_exists else "warn",
            "Frontend build is present." if frontend_build_exists else "Frontend build not found. Run npm run build.",
        ),
        "job_log_dir": health_check_item(
            "ok" if job_log_dir_exists else "warn",
            f"Job log directory exists: {JOB_LOG_DIR}" if job_log_dir_exists else f"Job log directory does not exist yet: {JOB_LOG_DIR}",
        ),
        "local_disk": health_check_item(
            local_disk.get("status", "warn"),
            local_disk.get("message", "Local disk usage is unavailable."),
        ),
        "upgrade_helper": health_check_item(
            "ok" if upgrade_helper_exists else "warn",
            f"Upgrade helper is installed: {UPGRADE_HELPER}" if upgrade_helper_exists else f"Upgrade helper is not installed: {UPGRADE_HELPER}",
        ),
        "uninstall_helper": health_check_item(
            "ok" if uninstall_helper_exists else "warn",
            f"Uninstall helper is installed: {UNINSTALL_HELPER}" if uninstall_helper_exists else f"Uninstall helper is not installed: {UNINSTALL_HELPER}",
        ),
        "timezone": health_check_item(
            "ok" if configured_timezone == expected_timezone else "warn",
            f"Server timezone is {configured_timezone}." if configured_timezone == expected_timezone else f"Expected timezone {expected_timezone}, current timezone {configured_timezone or 'unknown'}.",
        ),
        "time_sync": health_check_item(
            "ok" if ntp_synchronized == "yes" else "warn",
            "NTP is synchronized." if ntp_synchronized == "yes" else f"NTP is not synchronized yet. Configured servers: {configured_ntp_servers}",
        ),
        "ntp_service": health_check_item(
            "ok" if ntp_service_active == "yes" else "warn",
            "NTP service is enabled." if ntp_service_active == "yes" else "NTP service is not enabled according to timedatectl.",
        ),
    }

    try:
        import redis

        redis_client = redis.Redis.from_url(
            redis_url_from_runtime(),
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        redis_client.ping()
        checks["redis"] = health_check_item("ok", "Redis is reachable.")
    except Exception as exc:
        checks["redis"] = health_check_item("error", f"Redis is not reachable: {truncate_text(str(exc), 300)}")

    states = [check["status"] for check in checks.values()]
    overall_status = "error" if "error" in states else "warn" if "warn" in states else "ok"

    return {
        "status": overall_status,
        "service": "oci-migrator",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "server_time_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "timezone": configured_timezone or expected_timezone,
        "checks": checks,
    }


@app.get("/health")
async def health():
    return build_health_payload()


def backup_run_timestamp(run: dict) -> str:
    return str(run.get("finished_at") or run.get("updated_at") or run.get("started_at") or run.get("created_at") or "")


def backup_monitoring_summary() -> dict:
    jobs = [job for job in load_jobs() if job.get("is_active", True)]
    runs = [run for run in list_job_runs(300) if run.get("kind") == "data_sync"]
    latest_by_job: dict[str, dict] = {}
    successes: list[dict] = []
    failures: list[dict] = []

    for run in runs:
        job_name = str(run.get("job_name") or "").strip()
        status_value = str(run.get("status") or "").lower()
        if job_name and job_name not in latest_by_job:
            latest_by_job[job_name] = run
        if status_value == "success":
            successes.append(run)
        elif status_value in {"failed", "timeout"}:
            failures.append(run)

    job_items = []
    failed_jobs = []
    never_run_jobs = []
    running_jobs = []
    warning_jobs = []

    for job in jobs:
        job_name = str(job.get("name") or "").strip()
        latest = latest_by_job.get(job_name)
        if latest:
            last_status = str(latest.get("status") or "unknown").lower()
            last_run_at = backup_run_timestamp(latest)
            item = {
                "name": job_name,
                "last_status": last_status,
                "last_run_at": last_run_at,
                "last_success_at": "",
                "last_failure_at": "",
                "message": latest.get("details") or latest.get("error") or "",
                "run_id": latest.get("id", ""),
            }

            latest_success = next((run for run in runs if run.get("job_name") == job_name and run.get("status") == "success"), None)
            latest_failure = next((run for run in runs if run.get("job_name") == job_name and str(run.get("status", "")).lower() in {"failed", "timeout"}), None)
            if latest_success:
                item["last_success_at"] = backup_run_timestamp(latest_success)
            if latest_failure:
                item["last_failure_at"] = backup_run_timestamp(latest_failure)
        else:
            last_status = "never_run"
            item = {
                "name": job_name,
                "last_status": last_status,
                "last_run_at": "",
                "last_success_at": "",
                "last_failure_at": "",
                "message": "No run history found for this active backup job.",
                "run_id": "",
            }

        if last_status in {"failed", "timeout"}:
            failed_jobs.append(item)
        elif last_status == "warning":
            warning_jobs.append(item)
        elif last_status == "never_run":
            never_run_jobs.append(item)
        elif last_status in {"running", "queued", "retrying"}:
            running_jobs.append(item)

        job_items.append(item)

    backup_status = "ok"
    if failed_jobs:
        backup_status = "error"
    elif warning_jobs:
        backup_status = "warn"
    elif never_run_jobs:
        backup_status = "warn"

    last_success = max((backup_run_timestamp(run) for run in successes), default="")
    last_failure = max((backup_run_timestamp(run) for run in failures), default="")

    return {
        "status": backup_status,
        "jobs_total": len(jobs),
        "jobs_failed": len(failed_jobs),
        "jobs_warning": len(warning_jobs),
        "jobs_never_run": len(never_run_jobs),
        "jobs_running": len(running_jobs),
        "last_success_at": last_success,
        "last_failure_at": last_failure,
        "failed_jobs": failed_jobs,
        "warning_jobs": warning_jobs,
        "never_run_jobs": never_run_jobs,
        "running_jobs": running_jobs,
        "jobs": job_items,
    }


def build_monitoring_status() -> dict:
    health_payload = build_health_payload()
    checks = health_payload.get("checks", {})
    worker_state = systemd_unit_state(f"{SERVICE_PREFIX}-worker.service")
    scheduler_state = systemd_unit_state(f"{SERVICE_PREFIX}-scheduler.timer")
    backup_summary = backup_monitoring_summary()

    services = {
        "api": {"status": "ok", "message": "API is responding."},
        "worker": worker_state,
        "scheduler": scheduler_state,
        "redis": checks.get("redis", health_check_item("warn", "Redis status is unavailable.")),
        "rclone": checks.get("rclone_binary", health_check_item("warn", "rclone status is unavailable.")),
        "local_disk": checks.get("local_disk", health_check_item("warn", "Local disk status is unavailable.")),
        "ntp": health_check_item(
            worst_status(
                checks.get("time_sync", {}).get("status", "warn"),
                checks.get("ntp_service", {}).get("status", "warn"),
            ),
            f"{checks.get('time_sync', {}).get('message', '')} {checks.get('ntp_service', {}).get('message', '')}".strip(),
        ),
        "timezone": checks.get("timezone", health_check_item("warn", "Timezone status is unavailable.")),
    }

    service_status = worst_status(*(item.get("status", "warn") for item in services.values()))
    overall_status = worst_status(health_payload.get("status", "warn"), service_status, backup_summary["status"])

    return {
        "status": overall_status,
        "service": "oci-migrator",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "service_prefix": SERVICE_PREFIX,
        "services": services,
        "health": {
            "status": health_payload.get("status", "warn"),
            "checks": checks,
        },
        "backups": backup_summary,
    }


@app.get("/monitoring/status")
async def monitoring_status():
    return build_monitoring_status()


def prometheus_metrics_payload(status_payload: dict) -> str:
    lines = [
        "# HELP oci_migrator_up Whether the OCI Migrator API is responding.",
        "# TYPE oci_migrator_up gauge",
        "oci_migrator_up 1",
        "# HELP oci_migrator_component_ok Component health status, 1 means ok.",
        "# TYPE oci_migrator_component_ok gauge",
    ]

    services = status_payload.get("services", {})
    for component, item in services.items():
        value = 1 if item.get("status") == "ok" else 0
        lines.append(f'oci_migrator_component_ok{{component="{prometheus_escape_label(component)}"}} {value}')

    backups = status_payload.get("backups", {})
    local_disk = current_local_disk_settings()
    lines.extend(
        [
            "# HELP oci_migrator_backup_jobs Number of active backup jobs.",
            "# TYPE oci_migrator_backup_jobs gauge",
            f"oci_migrator_backup_jobs {int(backups.get('jobs_total', 0))}",
            "# HELP oci_migrator_backup_jobs_failed Number of active backup jobs whose latest run failed.",
            "# TYPE oci_migrator_backup_jobs_failed gauge",
            f"oci_migrator_backup_jobs_failed {int(backups.get('jobs_failed', 0))}",
            "# HELP oci_migrator_backup_jobs_warning Number of active backup jobs whose latest run completed with warnings.",
            "# TYPE oci_migrator_backup_jobs_warning gauge",
            f"oci_migrator_backup_jobs_warning {int(backups.get('jobs_warning', 0))}",
            "# HELP oci_migrator_backup_jobs_never_run Number of active backup jobs without run history.",
            "# TYPE oci_migrator_backup_jobs_never_run gauge",
            f"oci_migrator_backup_jobs_never_run {int(backups.get('jobs_never_run', 0))}",
            "# HELP oci_migrator_backup_jobs_running Number of active backup jobs currently queued or running.",
            "# TYPE oci_migrator_backup_jobs_running gauge",
            f"oci_migrator_backup_jobs_running {int(backups.get('jobs_running', 0))}",
            "# HELP oci_migrator_local_disk_used_percent Local data disk used percent.",
            "# TYPE oci_migrator_local_disk_used_percent gauge",
            f"oci_migrator_local_disk_used_percent {float(local_disk.get('used_percent', 0))}",
            "# HELP oci_migrator_local_disk_free_bytes Local data disk free bytes.",
            "# TYPE oci_migrator_local_disk_free_bytes gauge",
            f"oci_migrator_local_disk_free_bytes {int(local_disk.get('free_bytes', 0))}",
            "# HELP oci_migrator_backup_last_success_timestamp Unix timestamp for the latest successful backup run.",
            "# TYPE oci_migrator_backup_last_success_timestamp gauge",
            f"oci_migrator_backup_last_success_timestamp {iso_to_unix_seconds(backups.get('last_success_at', '')):.0f}",
            "# HELP oci_migrator_backup_last_failure_timestamp Unix timestamp for the latest failed backup run.",
            "# TYPE oci_migrator_backup_last_failure_timestamp gauge",
            f"oci_migrator_backup_last_failure_timestamp {iso_to_unix_seconds(backups.get('last_failure_at', '')):.0f}",
            "# HELP oci_migrator_backup_job_last_run_timestamp Unix timestamp for each active backup job's latest run.",
            "# TYPE oci_migrator_backup_job_last_run_timestamp gauge",
        ]
    )

    for job in backups.get("jobs", []):
        job_label = prometheus_escape_label(job.get("name", ""))
        status_label = prometheus_escape_label(job.get("last_status", "unknown"))
        lines.append(
            f'oci_migrator_backup_job_last_run_timestamp{{job="{job_label}",status="{status_label}"}} '
            f"{iso_to_unix_seconds(job.get('last_run_at', '')):.0f}"
        )

    lines.append("# HELP oci_migrator_backup_job_last_status Last status for each active backup job as a one-hot gauge.")
    lines.append("# TYPE oci_migrator_backup_job_last_status gauge")
    for job in backups.get("jobs", []):
        job_label = prometheus_escape_label(job.get("name", ""))
        current_status = str(job.get("last_status") or "unknown").lower()
        for status_value in ("success", "warning", "failed", "timeout", "running", "queued", "never_run", "unknown"):
            value = 1 if current_status == status_value else 0
            lines.append(
                f'oci_migrator_backup_job_last_status{{job="{job_label}",status="{status_value}"}} {value}'
            )

    lines.append("")
    return "\n".join(lines)


@app.get("/metrics")
async def prometheus_metrics():
    return Response(
        content=prometheus_metrics_payload(build_monitoring_status()),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/job-history")
async def job_history(limit: int = Query(default=100, ge=1, le=300)):
    return {"runs": list_job_runs(limit)}


@app.get("/job-log-settings")
async def get_job_log_settings():
    return current_job_log_settings()


@app.get("/local-disk-settings")
async def get_local_disk_settings():
    return current_local_disk_settings()


@app.get("/time-settings")
async def get_time_settings():
    return current_time_settings()


@app.get("/network-settings")
async def get_network_settings():
    return current_network_settings()


@app.get("/rclone-default-settings")
async def get_rclone_default_settings():
    return current_rclone_default_settings()


@app.get("/notification-settings")
async def notification_settings():
    return get_notification_settings()


@app.put("/notification-settings")
async def update_notification_settings(settings: NotificationSettingsRequest):
    normalized = normalize_notification_settings(settings)
    env_values = {
        "OCI_MIGRATOR_SYSLOG_ENABLED": "true" if normalized["enabled"] else "false",
        "OCI_MIGRATOR_SYSLOG_HOST": normalized["host"],
        "OCI_MIGRATOR_SYSLOG_PORT": str(normalized["port"]),
        "OCI_MIGRATOR_SYSLOG_PROTOCOL": normalized["protocol"],
        "OCI_MIGRATOR_SYSLOG_FACILITY": normalized["facility"],
        "OCI_MIGRATOR_SYSLOG_EVENTS": normalized["events"],
    }
    _write_env_values(ENV_FILE_PATH, env_values)
    os.environ.update(env_values)
    return get_notification_settings()


@app.post("/notification-settings/test")
async def test_notification_settings(settings: NotificationSettingsRequest):
    normalized = normalize_notification_settings(settings, require_host=True)
    result = send_test_notification(normalized)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error") or "Syslog test failed.")
    return {**result, **get_notification_settings()}


@app.put("/rclone-default-settings")
async def update_rclone_default_settings(settings: RcloneDefaultSettingsRequest):
    bwlimit = validate_rclone_bwlimit(settings.bwlimit)
    tpslimit = validate_rclone_tpslimit(settings.tpslimit)
    _write_env_values(
        ENV_FILE_PATH,
        {
            "OCI_MIGRATOR_DEFAULT_BWLIMIT": bwlimit,
            "OCI_MIGRATOR_DEFAULT_TPSLIMIT": "" if tpslimit is None else f"{tpslimit:g}",
        },
    )
    os.environ["OCI_MIGRATOR_DEFAULT_BWLIMIT"] = bwlimit
    os.environ["OCI_MIGRATOR_DEFAULT_TPSLIMIT"] = "" if tpslimit is None else f"{tpslimit:g}"
    return current_rclone_default_settings()


@app.put("/time-settings")
async def update_time_settings(settings: TimeSettingsRequest):
    timezone_value = normalize_time_zone(settings.timezone)
    ntp_servers = normalize_ntp_servers(settings.ntp_servers)

    with TIME_SETTINGS_LOCK:
        apply_time_sync_settings(timezone_value, ntp_servers)
        _write_env_values(
            ENV_FILE_PATH,
            {
                "OCI_MIGRATOR_TIMEZONE": timezone_value,
                "OCI_MIGRATOR_NTP_SERVERS": ntp_servers.replace(" ", ","),
                "OCI_MIGRATOR_TIME_SYNC_HELPER": str(TIME_SYNC_HELPER),
            },
        )
        os.environ["OCI_MIGRATOR_TIMEZONE"] = timezone_value
        os.environ["OCI_MIGRATOR_NTP_SERVERS"] = ntp_servers

    return current_time_settings()


@app.put("/network-settings")
async def update_network_settings(settings: NetworkSettingsRequest):
    normalized = normalize_network_settings(settings)
    command = [
        "stage",
        "--mode",
        normalized["mode"],
        "--interface",
        normalized["interface"],
    ]
    if normalized["mode"] == "static":
        command.extend(
            [
                "--address",
                normalized["address"],
                "--gateway",
                normalized["gateway"],
                "--dns",
                normalized["dns_servers"],
            ]
        )
    with NETWORK_SETTINGS_LOCK:
        return run_network_helper(command, timeout=30)


@app.post("/network-settings/confirm")
async def confirm_network_settings():
    with NETWORK_SETTINGS_LOCK:
        return run_network_helper(["confirm"], timeout=30)


@app.post("/network-settings/rollback")
async def rollback_network_settings():
    with NETWORK_SETTINGS_LOCK:
        return run_network_helper(["rollback"], timeout=45)


@app.put("/job-log-settings")
async def update_job_log_settings(settings: JobLogSettingsRequest):
    max_size = normalize_job_log_max_size(settings.max_size)
    retention_days = validate_job_log_retention_days(settings.retention_days)

    with JOB_LOG_SETTINGS_LOCK:
        apply_job_log_rotation_settings(max_size, retention_days)
        _write_env_values(
            ENV_FILE_PATH,
            {
                "OCI_MIGRATOR_JOB_LOG_MAX_SIZE": max_size,
                "OCI_MIGRATOR_JOB_LOG_RETENTION_DAYS": str(retention_days),
            },
        )

    return current_job_log_settings()


@app.put("/local-disk-settings")
async def update_local_disk_settings(settings: LocalDiskSettingsRequest):
    warning_percent, critical_percent = validate_local_disk_thresholds(
        settings.warning_percent,
        settings.critical_percent,
    )
    _write_env_values(
        ENV_FILE_PATH,
        {
            "OCI_MIGRATOR_LOCAL_DISK_WARNING_PERCENT": str(warning_percent),
            "OCI_MIGRATOR_LOCAL_DISK_CRITICAL_PERCENT": str(critical_percent),
        },
    )
    os.environ["OCI_MIGRATOR_LOCAL_DISK_WARNING_PERCENT"] = str(warning_percent)
    os.environ["OCI_MIGRATOR_LOCAL_DISK_CRITICAL_PERCENT"] = str(critical_percent)
    return current_local_disk_settings()


@app.get("/upgrade/status")
async def upgrade_status():
    return upgrade_status_payload()


@app.post("/upgrade/check")
async def check_for_upgrade():
    return latest_git_info()


@app.post("/upgrade/start")
async def start_upgrade():
    with UPGRADE_LOCK:
        current_status = read_upgrade_status_file()
        if current_status.get("status") == "running" and upgrade_process_is_running():
            raise HTTPException(status_code=409, detail="Upgrade is already running.")

        command = upgrade_helper_command()
        try:
            UPGRADE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(UPGRADE_LOG_FILE, "ab") as log_handle:
                subprocess.Popen(
                    command,
                    cwd=str(PROJECT_ROOT),
                    stdout=log_handle,
                    stderr=log_handle,
                    start_new_session=True,
                )
        except Exception as exc:
            raise_operation_error(
                500,
                "Start upgrade",
                exc,
                "Check that install.sh installed /usr/local/sbin/oci-migrator-upgrade and sudoers access for the service user.",
            )

    return {
        **upgrade_status_payload(),
        "status": "running",
        "message": "Upgrade started. The service may restart during installation.",
    }


@app.get("/upgrade/log")
async def upgrade_log(lines: int = Query(default=600, ge=20, le=2000)):
    if not UPGRADE_LOG_FILE.exists():
        return {
            "log": "No upgrade log yet.",
            "exists": False,
            "log_file": str(UPGRADE_LOG_FILE),
        }

    return {
        "log": tail_file(UPGRADE_LOG_FILE, max_lines=lines),
        "exists": True,
        "log_file": str(UPGRADE_LOG_FILE),
    }


@app.get("/system/uninstall")
async def uninstall_info():
    return {
        "helper_installed": UNINSTALL_HELPER.is_file(),
        "local_data_root": str(LOCAL_DATA_ROOT),
        "project_will_be_removed": True,
        "runtime_config_will_be_preserved": True,
        "cloud_data_will_be_preserved": True,
    }


@app.post("/system/uninstall", status_code=status.HTTP_202_ACCEPTED)
async def schedule_uninstall(data: UninstallRequest):
    if data.confirmation != "UNINSTALL":
        raise HTTPException(status_code=400, detail="Type UNINSTALL exactly to confirm.")

    config = get_runtime_config()
    admin_password_hash = str(config.get("admin_password_hash", ""))
    if not admin_password_hash or not verify_password(data.current_password, admin_password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect.")

    try:
        result = subprocess.run(
            uninstall_helper_command(data.purge_local_backups),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(truncate_text(result.stderr or result.stdout or f"helper exited with code {result.returncode}"))
    except HTTPException:
        raise
    except subprocess.TimeoutExpired as exc:
        raise_operation_error(504, "Schedule uninstall", exc, "The uninstall helper did not respond in time.")
    except Exception as exc:
        raise_operation_error(
            500,
            "Schedule uninstall",
            exc,
            "Check that install.sh installed the controlled uninstall helper and sudoers access.",
        )

    return {
        "status": "scheduled",
        "message": "Uninstall scheduled. This console will become unavailable shortly.",
        "purge_local_backups": data.purge_local_backups,
        "local_data_root": str(LOCAL_DATA_ROOT),
    }


@app.get("/job-history/{run_id}")
async def job_history_item(run_id: str):
    run = get_job_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Job run not found.")
    return run


def log_path_for_run(run: dict) -> Path | None:
    configured_path = resolve_readable_log_path(str(run.get("log_file", "")))
    if configured_path:
        return configured_path

    job_name = str(run.get("job_name") or run.get("kind") or "default")
    run_id = str(run.get("id") or "")
    fallback_path = job_log_path(job_name, run_id)
    if fallback_path.exists():
        return fallback_path

    legacy_path = legacy_job_log_path(job_name)
    if legacy_path.exists():
        return legacy_path

    return configured_path or fallback_path


def job_run_log_payload(run: dict, max_lines: int = 500) -> dict:
    path = log_path_for_run(run)
    if not path or not path.exists():
        return {
            "log": "Waiting for job to start reporting...",
            "exists": False,
            "log_file": str(path) if path else "",
        }

    return {
        "log": tail_file(path, max_lines=max_lines, humanize_json=True),
        "exists": True,
        "log_file": str(path),
    }


@app.get("/job-history/{run_id}/log")
async def job_history_log(run_id: str, lines: int = Query(default=500, ge=20, le=2000)):
    run = get_job_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Job run not found.")
    return job_run_log_payload(run, max_lines=lines)


@app.get("/job-history/{run_id}/log/download")
async def download_job_history_log(run_id: str):
    run = get_job_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Job run not found.")

    path = log_path_for_run(run)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Job log file not found yet.")

    file_name = f"{normalize_job_name(str(run.get('job_name') or run.get('kind') or 'job'))}_{normalize_job_name(run_id)}.log"
    return FileResponse(path, media_type="text/plain", filename=file_name)


@app.get("/runtime-config/export")
async def export_runtime_config():
    archive_buffer, timestamp = build_runtime_config_archive()
    headers = {"Content-Disposition": f'attachment; filename="oci-migrator-runtime-{timestamp}.zip"'}
    return StreamingResponse(archive_buffer, media_type="application/zip", headers=headers)


@app.post("/runtime-config/import")
async def import_runtime_config(file: UploadFile = File(...)):
    safe_name = sanitize_filename(file.filename or "runtime-config.zip", "runtime-config.zip")
    if not safe_name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Upload a ZIP file created by Runtime Config Backup.")

    payload = await file.read(RUNTIME_CONFIG_IMPORT_MAX_BYTES + 1)
    await file.close()
    if len(payload) > RUNTIME_CONFIG_IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Runtime backup ZIP is too large.")

    try:
        return restore_runtime_config_archive(payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise_operation_error(
            500,
            "Import runtime config",
            exc,
            "A pre-restore backup is created before files are replaced. Check service permissions and the uploaded ZIP.",
        )

# --- 1. OCI Profile Management ---
@app.post("/upload-key")
async def upload_key(file: UploadFile = File(...)):
    safe_name = sanitize_filename(file.filename, "uploaded_api_key.pem")
    file_path = os.path.join(OCI_DIR, safe_name)
    # Do not log any file contents. We only log minimal metadata.
    logger.info("Uploading OCI API key file: name=%s size=%s", safe_name, file.size)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    os.chmod(file_path, 0o600)
    return {"status": "secured", "file_name": safe_name, "saved_path": file_path}

@app.post("/save-config")
async def save_config(data: ConfigSchema):
    parser = configparser.ConfigParser()
    with CONFIG_LOCK:
        if os.path.exists(CONFIG_PATH):
            parser.read(CONFIG_PATH)
        if not parser.has_section(data.profile_name):
            parser.add_section(data.profile_name)

        existing_key_path = parser.get(data.profile_name, "key_file", fallback="")
        if data.key_file_name:
            key_path = os.path.join(OCI_DIR, sanitize_filename(data.key_file_name, "uploaded_api_key.pem"))
        else:
            key_path = existing_key_path

        if not key_path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="key_file_name is required when creating a new profile or rotating a key.",
            )

        storage_comp = data.storage_compartment_ocid if data.storage_compartment_ocid else data.compartment_ocid

        parser.set(data.profile_name, 'user', data.user_ocid)
        parser.set(data.profile_name, 'fingerprint', data.fingerprint)
        parser.set(data.profile_name, 'tenancy', data.tenancy_ocid)
        parser.set(data.profile_name, 'region', data.region)
        parser.set(data.profile_name, 'key_file', key_path)
        parser.set(data.profile_name, 'compartment', data.compartment_ocid)
        parser.set(data.profile_name, 'storage_compartment', storage_comp)

        write_ini_atomically(parser, CONFIG_PATH)

    storage_comp = data.storage_compartment_ocid if data.storage_compartment_ocid else data.compartment_ocid
    sync_oci_to_rclone(data.profile_name, data.region, storage_comp)
    return {"message": "Profile and Rclone bridge saved", "profile": data.profile_name}

@app.get("/list-profiles")
async def list_profiles():
    if not os.path.exists(CONFIG_PATH): return {"profiles": []}
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH)
    return {"profiles": parser.sections()}

@app.get("/get-profile/{profile_name}")
async def get_profile(profile_name: str):
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH)
    if not parser.has_section(profile_name):
        raise HTTPException(status_code=404, detail="Profile not found")
    section = parser[profile_name]
    return {
        "profileName": profile_name,
        "userOcid": section.get("user", ""),
        "tenancyOcid": section.get("tenancy", ""),
        "fingerprint": section.get("fingerprint", ""),
        "region": section.get("region", ""),
        "compartmentOcid": section.get("compartment", ""),
        "storageCompartmentOcid": section.get("storage_compartment", section.get("compartment", ""))
    }


@app.delete("/delete-profile/{profile_name}")
async def delete_profile(profile_name: str):
    parser = configparser.ConfigParser()
    with CONFIG_LOCK:
        if not os.path.exists(CONFIG_PATH):
            raise HTTPException(status_code=404, detail="Profile not found")

        parser.read(CONFIG_PATH)
        if not parser.has_section(profile_name):
            raise HTTPException(status_code=404, detail="Profile not found")

        key_path = parser.get(profile_name, "key_file", fallback="")
        key_is_shared = any(
            section != profile_name and parser.get(section, "key_file", fallback="") == key_path
            for section in parser.sections()
        )
        parser.remove_section(profile_name)
        write_ini_atomically(parser, CONFIG_PATH)

    if key_path and key_path.startswith(OCI_DIR) and os.path.exists(key_path) and not key_is_shared:
        os.remove(key_path)

    if os.path.exists(RCLONE_CONF):
        rclone_parser = configparser.ConfigParser()
        with RCLONE_LOCK:
            rclone_parser.read(RCLONE_CONF)
            rclone_section = f"{profile_name}_rclone"
            if rclone_parser.has_section(rclone_section):
                rclone_parser.remove_section(rclone_section)
                write_ini_atomically(rclone_parser, RCLONE_CONF)

    return {"message": f"Profile '{profile_name}' deleted"}

# --- 2. Job & Schedule Management (JSON Store) ---
@app.post("/save-job")
async def save_job(job: DataSyncJob):
    metadata_tags = normalize_metadata_tags(job.metadata_tags)
    rclone_limits = validate_rclone_limits(job)
    lifecycle_policy = normalize_lifecycle_policy(job.lifecycle_policy, job.name)
    local_retention = validate_local_retention_config(job.local_retention)
    validate_destination_bucket(job.dest_profile, job.dest_bucket)
    jobs_snapshot = load_jobs()
    validate_local_retention_usage(job.name, job.previous_name, job.source_remote, local_retention, jobs_snapshot)
    existing_job = next((j for j in jobs_snapshot if j.get("name") == job.name), None)
    existing_lifecycle_enabled = bool((existing_job or {}).get("lifecycle_policy", {}).get("enabled"))
    existing_destination_changed = bool(
        existing_job
        and (
            existing_job.get("dest_profile") != job.dest_profile
            or destination_bucket_name(existing_job.get("dest_bucket", "")) != destination_bucket_name(job.dest_bucket)
        )
    )
    lifecycle_rule_count = 0
    try:
        if existing_lifecycle_enabled and existing_destination_changed:
            remove_job_lifecycle_policy(
                job.name,
                existing_job.get("dest_profile", ""),
                existing_job.get("dest_bucket", ""),
            )
        if lifecycle_policy.get("enabled") or existing_lifecycle_enabled:
            lifecycle_rule_count = apply_job_lifecycle_policy(
                job.name,
                job.dest_profile,
                job.dest_bucket,
                lifecycle_policy,
            )
    except HTTPException:
        raise
    except Exception as e:
        raise_operation_error(
            500,
            "Apply OCI lifecycle policy",
            e,
            "Check Object Storage permissions for lifecycle policy management on the destination bucket.",
        )

    with JOBS_LOCK:
        jobs = load_jobs()
        job_dict = job.model_dump()
        job_dict.pop("previous_name", None)
        job_dict["metadata_tags"] = metadata_tags
        job_dict.update(rclone_limits)
        job_dict["lifecycle_policy"] = lifecycle_policy
        job_dict["local_retention"] = local_retention
        existing = next((i for i, j in enumerate(jobs) if j['name'] == job.name), None)

        if existing is not None:
            jobs[existing] = job_dict
        else:
            jobs.append(job_dict)

        write_jobs_atomically(jobs)

    schedule_state = "ready for scheduling" if job.schedule.frequency != "none" else "saved for manual runs"
    lifecycle_state = (
        f" OCI lifecycle rules applied: {lifecycle_rule_count}."
        if lifecycle_policy.get("enabled")
        else " OCI lifecycle rules disabled for this job."
    )
    return {"message": f"Job '{job.name}' {schedule_state}.{lifecycle_state}"}

@app.get("/list-jobs")
async def list_jobs():
    return load_jobs()

@app.delete("/delete-job/{job_name}")
async def delete_job(job_name: str):
    with JOBS_LOCK:
        existing_jobs = load_jobs()
        job_to_delete = next((j for j in existing_jobs if j['name'] == job_name), None)
        jobs = [j for j in existing_jobs if j['name'] != job_name]
        write_jobs_atomically(jobs)
    if job_to_delete:
        try:
            remove_job_lifecycle_policy(
                job_name,
                job_to_delete.get("dest_profile", ""),
                job_to_delete.get("dest_bucket", ""),
            )
        except Exception as exc:
            logger.warning("Unable to remove lifecycle rules for deleted job '%s': %s", job_name, exc)
    return {"message": "Job deleted"}

# --- 3. Live Logs ---
@app.get("/job-log/{job_name}")
async def get_job_log(job_name: str):
    latest_run = next(
        (
            run
            for run in list_job_runs(300)
            if run.get("kind") == "data_sync" and run.get("job_name") == job_name
        ),
        None,
    )
    if latest_run:
        return job_run_log_payload(latest_run, max_lines=500)

    legacy_path = legacy_job_log_path(job_name)
    if not legacy_path.exists():
        return {"log": "Waiting for job to start reporting...", "exists": False, "log_file": str(legacy_path)}

    return {"log": tail_file(legacy_path, max_lines=500, humanize_json=True), "exists": True, "log_file": str(legacy_path)}

# --- 4. Rclone Remotes & Buckets ---
@app.get("/list-remotes")
async def list_remotes():
    if not os.path.exists(RCLONE_CONF):
        return {"remotes": [], "remote_details": []}
    parser = configparser.ConfigParser()
    parser.read(RCLONE_CONF)
    remote_details = []
    for section in parser.sections():
        remote_details.append(
            {
                "name": section,
                "type": parser.get(section, "type", fallback=""),
                "local_mode": parser.get(section, "oci_migrator_local_mode", fallback=""),
                "local_path": parser.get(section, "oci_migrator_local_path", fallback=""),
                "share_name": parser.get(section, "oci_migrator_share_name", fallback=""),
                "share_access": parser.get(section, "oci_migrator_share_access", fallback=""),
                "nfs_share_name": parser.get(section, "oci_migrator_nfs_share_name", fallback=""),
                "nfs_clients": parser.get(section, "oci_migrator_nfs_clients", fallback=""),
            }
        )
    return {"remotes": parser.sections(), "remote_details": remote_details}

@app.get("/list-remote-buckets/{remote_name}")
async def list_remote_buckets(remote_name: str):
    try:
        parser = configparser.ConfigParser()
        if os.path.exists(RCLONE_CONF):
            parser.read(RCLONE_CONF)

        if not parser.has_section(remote_name):
            raise HTTPException(status_code=404, detail="Remote not found.")

        if parser.get(remote_name, "type", fallback="") == "local":
            local_path_raw = parser.get(remote_name, "oci_migrator_local_path", fallback="")
            if not local_path_raw:
                raise HTTPException(
                    status_code=400,
                    detail="Local remote is missing its managed path. Recreate the remote.",
                )
            local_path = Path(local_path_raw).expanduser().resolve()
            return {
                "remote_type": "local",
                "base_path": str(local_path),
                "buckets": list_local_source_entries(local_path),
            }

        command = ["rclone", "lsf", f"{remote_name}:", "--max-depth", "1"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(truncate_text(result.stderr or result.stdout or f"rclone exited with code {result.returncode}"))
        buckets = [line.replace('/', '').strip() for line in result.stdout.split('\n') if line.strip()]
        return {"buckets": buckets}
    except HTTPException:
        raise
    except subprocess.TimeoutExpired as e:
        raise_operation_error(504, "List remote buckets", e, "Check that the rclone remote is reachable.")
    except Exception as e:
        raise_operation_error(500, "List remote buckets", e, "Check rclone credentials and remote name.")

@app.post("/start-data-sync-manual")
async def start_sync_manual(job: DataSyncJob):
    validate_destination_bucket(job.dest_profile, job.dest_bucket)
    run_id = str(uuid.uuid4())
    safe_job_name = normalize_job_name(job.name)
    destination = f"{job.dest_profile}_rclone:{job.dest_bucket}"
    metadata_tags = normalize_metadata_tags(job.metadata_tags)
    rclone_limits = validate_rclone_limits(job)
    local_retention = validate_local_retention_config(job.local_retention)
    upsert_job_run(
        {
            "id": run_id,
            "kind": "data_sync",
            "job_name": job.name,
            "status": "queued",
            "trigger": "manual",
            "source": job.source_remote,
            "destination": destination,
            "metadata_tags": metadata_tags,
            "local_retention": local_retention,
            "details": "Queued for worker.",
            "log_file": str(job_log_path(safe_job_name, run_id)),
        }
    )
    try:
        task = rclone_sync_task.apply_async(
            args=[
                job.source_remote,
                job.dest_profile,
                job.dest_bucket,
                job.sync_mode,
                job.transfers,
                job.checkers,
                job.buffer_size,
                safe_job_name,
                run_id,
                "manual",
                metadata_tags,
                local_retention,
                rclone_limits["bwlimit"],
                rclone_limits["tpslimit"],
            ],
            task_id=run_id,
        )
    except Exception as e:
        upsert_job_run({"id": run_id, "status": "failed", "details": "Unable to queue worker task.", "error": str(e)})
        raise_operation_error(500, "Start data sync job", e, "Check that Redis and the Celery worker are running.")

    return {"task_id": task.id, "run_id": run_id, "status": "queued"}

# NYTT: Spara Big 5 Remotes (AWS, Azure, GCP, Local)
@app.post("/save-remote")
async def save_remote(
    request: Request,
    name: str = Form(...),
    provider: str = Form(...),
    access_key: str = Form(""),
    secret_key: str = Form(""),
    region: str = Form(""),
    account_name: str = Form(""),
    account_key: str = Form(""),
    gcp_object_acl: str = Form(""),
    gcp_bucket_acl: str = Form(""),
    gcp_location: str = Form(""),
    local_mode: str = Form("server_folder"),
    local_folder_name: str = Form(""),
    local_mount_path: str = Form(""),
    local_share_access: str = Form("none"),
    local_share_name: str = Form(""),
    local_share_username: str = Form(""),
    local_share_password: str = Form(""),
    local_nfs_enabled: str = Form("false"),
    local_nfs_clients: str = Form(""),
    gcp_file: Optional[UploadFile] = File(None)
):
    parser = configparser.ConfigParser()
    saved_local_path = None
    saved_share = None
    saved_nfs_share = None
    try:
        with RCLONE_LOCK:
            if os.path.exists(RCLONE_CONF):
                parser.read(RCLONE_CONF)

            previous_share_name = parser.get(name, 'oci_migrator_share_name', fallback='') if parser.has_section(name) else ''
            previous_nfs_share_name = parser.get(name, 'oci_migrator_nfs_share_name', fallback='') if parser.has_section(name) else ''

            if not parser.has_section(name):
                parser.add_section(name)

            for option in (
                'oci_migrator_local_mode',
                'oci_migrator_local_path',
                'oci_migrator_local_display_name',
                'oci_migrator_share_access',
                'oci_migrator_share_name',
                'oci_migrator_share_username',
                'oci_migrator_nfs_enabled',
                'oci_migrator_nfs_share_name',
                'oci_migrator_nfs_clients',
            ):
                parser.remove_option(name, option)

            if provider == 's3':
                parser.set(name, 'type', 's3')
                parser.set(name, 'provider', 'AWS')
                parser.set(name, 'access_key_id', access_key)
                parser.set(name, 'secret_access_key', secret_key)
                parser.set(name, 'region', region)
            elif provider == 'azureblob':
                parser.set(name, 'type', 'azureblob')
                parser.set(name, 'account', account_name)
                parser.set(name, 'key', account_key)
            elif provider == 'google cloud storage':
                parser.set(name, 'type', 'google cloud storage')
                parser.set(name, 'object_acl', gcp_object_acl)
                parser.set(name, 'bucket_acl', gcp_bucket_acl)
                parser.set(name, 'location', gcp_location)
                if gcp_file:
                    file_name = sanitize_filename(gcp_file.filename, f"{name}_service_account.json")
                    file_path = os.path.join(OCI_DIR, file_name)
                    with open(file_path, "wb") as buffer:
                        shutil.copyfileobj(gcp_file.file, buffer)
                    os.chmod(file_path, 0o600)
                    parser.set(name, 'service_account_file', file_path)
            elif provider == 'local':
                if local_mode not in {"server_folder", "mounted_share"}:
                    raise HTTPException(status_code=400, detail="Unsupported local source type.")

                if local_mode == "server_folder":
                    local_path = create_server_local_folder(local_folder_name or name)
                    display_name = normalize_local_folder_name(local_folder_name or name)
                else:
                    local_path = validate_external_mount_path(local_mount_path)
                    display_name = local_path.name or str(local_path)

                share_access = local_share_access.strip().lower() or "none"
                nfs_enabled = parse_form_bool(local_nfs_enabled)
                if local_mode != "server_folder" and (share_access != "none" or nfs_enabled):
                    raise HTTPException(status_code=400, detail="Managed sharing is only supported for server local folders.")

                if share_access not in {"none", "everyone", "user"}:
                    raise HTTPException(status_code=400, detail="Unsupported SMB share access mode.")

                share_name = ""
                if share_access != "none" or nfs_enabled:
                    share_name = normalize_smb_share_name(local_share_name or display_name)
                    for section in parser.sections():
                        used_share_names = {
                            parser.get(section, 'oci_migrator_share_name', fallback=''),
                            parser.get(section, 'oci_migrator_nfs_share_name', fallback=''),
                        }
                        if section != name and share_name in used_share_names:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Share name is already used by remote '{section}'.",
                            )

                if share_access != "none":
                    share_username = validate_smb_username(local_share_username) if share_access == "user" else ""
                    helper_result = enable_local_share(
                        local_path,
                        share_name,
                        share_access,
                        username=share_username,
                        password=local_share_password,
                    )
                    host = share_host_from_request(request)
                    saved_share = {
                        "name": share_name,
                        "access": share_access,
                        "username": share_username,
                        "unc_path": f"\\\\{host}\\{share_name}",
                        "smb_url": f"smb://{host}/{share_name}",
                        "port": helper_result.get("port", 445),
                    }

                if nfs_enabled:
                    nfs_clients = validate_nfs_clients(local_nfs_clients)
                    helper_result = enable_local_nfs_share(local_path, share_name, nfs_clients)
                    host = share_host_from_request(request)
                    nfs_path = helper_result.get("path", str(local_path))
                    saved_nfs_share = {
                        "name": share_name,
                        "clients": nfs_clients,
                        "path": nfs_path,
                        "mount": f"{host}:{nfs_path}",
                        "mount_command": f"sudo mount -t nfs4 {host}:{nfs_path} /mnt/{share_name}",
                        "port": helper_result.get("port", 2049),
                    }

                parser.set(name, 'type', 'local')
                parser.set(name, 'oci_migrator_local_mode', local_mode)
                parser.set(name, 'oci_migrator_local_path', str(local_path))
                parser.set(name, 'oci_migrator_local_display_name', display_name)
                if saved_share:
                    parser.set(name, 'oci_migrator_share_access', saved_share["access"])
                    parser.set(name, 'oci_migrator_share_name', saved_share["name"])
                    if saved_share["username"]:
                        parser.set(name, 'oci_migrator_share_username', saved_share["username"])
                if saved_nfs_share:
                    parser.set(name, 'oci_migrator_nfs_enabled', 'true')
                    parser.set(name, 'oci_migrator_nfs_share_name', saved_nfs_share["name"])
                    parser.set(name, 'oci_migrator_nfs_clients', saved_nfs_share["clients"])
                saved_local_path = str(local_path)
            else:
                raise HTTPException(status_code=400, detail="Unsupported remote provider")

            current_share_name = saved_share["name"] if saved_share else ""
            if previous_share_name and previous_share_name != current_share_name:
                disable_local_share(previous_share_name)
            current_nfs_share_name = saved_nfs_share["name"] if saved_nfs_share else ""
            if previous_nfs_share_name and previous_nfs_share_name != current_nfs_share_name:
                disable_local_nfs_share(previous_nfs_share_name)

            write_ini_atomically(parser, RCLONE_CONF)
    except HTTPException:
        raise
    except Exception as e:
        raise_operation_error(500, "Save rclone remote", e, "Check that the rclone config directory is writable.")
        
    response = {"message": "Remote saved successfully"}
    if saved_local_path:
        response["local_path"] = saved_local_path
    if saved_share:
        response["share"] = saved_share
    if saved_nfs_share:
        response["nfs_share"] = saved_nfs_share
    return response

# NYTT: Ta bort Remote
@app.delete("/delete-remote/{remote_name}")
async def delete_remote(remote_name: str):
    parser = configparser.ConfigParser()
    try:
        with RCLONE_LOCK:
            if os.path.exists(RCLONE_CONF):
                parser.read(RCLONE_CONF)

            if parser.has_section(remote_name):
                share_name = parser.get(remote_name, 'oci_migrator_share_name', fallback='')
                if share_name:
                    disable_local_share(share_name)
                nfs_share_name = parser.get(remote_name, 'oci_migrator_nfs_share_name', fallback='')
                if nfs_share_name:
                    disable_local_nfs_share(nfs_share_name)
                parser.remove_section(remote_name)
                write_ini_atomically(parser, RCLONE_CONF)
    except HTTPException:
        raise
    except Exception as e:
        raise_operation_error(500, "Delete rclone remote", e, "Check that the rclone config file is writable.")
    return {"message": "Remote deleted"}

# --- 5. OCI Explorer (VMs & Buckets) ---
@app.get("/list-vms/{profile}")
async def list_vms(profile: str):
    try:
        config = oci.config.from_file(CONFIG_PATH, profile)
        compute = oci.core.ComputeClient(config)
        network = oci.core.VirtualNetworkClient(config)
        block_storage = oci.core.BlockstorageClient(config)
        comp_id = config.get("compartment", config.get("tenancy"))
        res = oci.pagination.list_call_get_all_results(
            compute.list_instances,
            compartment_id=comp_id,
        )
        image_cache = {}
        boot_volume_cache = {}
        data_volume_cache = {}
        vm_list = []

        for instance in res.data:
            if instance.lifecycle_state == "TERMINATED":
                continue

            image_id = getattr(instance, "image_id", "")
            if image_id and image_id not in image_cache:
                try:
                    image = compute.get_image(image_id).data
                    image_cache[image_id] = " ".join(
                        value
                        for value in [
                            getattr(image, "operating_system", ""),
                            getattr(image, "operating_system_version", ""),
                        ]
                        if value
                    ).strip() or getattr(image, "display_name", "")
                except Exception as exc:
                    logger.info("Unable to resolve image metadata for %s: %s", image_id, exc)
                    image_cache[image_id] = ""

            private_ips = []
            public_ips = []
            try:
                attachments = oci.pagination.list_call_get_all_results(
                    compute.list_vnic_attachments,
                    compartment_id=getattr(instance, "compartment_id", comp_id) or comp_id,
                    instance_id=instance.id,
                ).data
                for attachment in attachments:
                    try:
                        vnic = network.get_vnic(attachment.vnic_id).data
                    except Exception as exc:
                        logger.info("Unable to resolve VNIC %s: %s", attachment.vnic_id, exc)
                        continue
                    if getattr(vnic, "private_ip", None):
                        private_ips.append(vnic.private_ip)
                    if getattr(vnic, "public_ip", None):
                        public_ips.append(vnic.public_ip)
            except Exception as exc:
                logger.info("Unable to resolve VNIC attachments for %s: %s", instance.id, exc)

            instance_compartment = getattr(instance, "compartment_id", comp_id) or comp_id
            boot_volumes = []
            data_volumes = []
            volume_scan_warnings = []
            try:
                boot_attachments = oci.pagination.list_call_get_all_results(
                    compute.list_boot_volume_attachments,
                    availability_domain=instance.availability_domain,
                    compartment_id=instance_compartment,
                    instance_id=instance.id,
                ).data
                for attachment in boot_attachments:
                    boot_volume_id = getattr(attachment, "boot_volume_id", "")
                    volume = boot_volume_cache.get(boot_volume_id)
                    if boot_volume_id and volume is None:
                        volume = block_storage.get_boot_volume(boot_volume_id).data
                        boot_volume_cache[boot_volume_id] = volume
                    boot_volumes.append(
                        {
                            "id": boot_volume_id,
                            "name": getattr(volume, "display_name", "") or "Boot volume",
                            "size_gb": getattr(volume, "size_in_gbs", None),
                            "state": getattr(volume, "lifecycle_state", "") or getattr(attachment, "lifecycle_state", ""),
                        }
                    )
            except Exception as exc:
                logger.info("Unable to resolve boot volume for %s: %s", instance.id, exc)
                volume_scan_warnings.append("Boot volume details could not be read.")

            try:
                volume_attachments = oci.pagination.list_call_get_all_results(
                    compute.list_volume_attachments,
                    compartment_id=instance_compartment,
                    instance_id=instance.id,
                ).data
                for attachment in volume_attachments:
                    volume_id = getattr(attachment, "volume_id", "")
                    volume = data_volume_cache.get(volume_id)
                    if volume_id and volume is None:
                        volume = block_storage.get_volume(volume_id).data
                        data_volume_cache[volume_id] = volume
                    data_volumes.append(
                        {
                            "id": volume_id,
                            "name": getattr(volume, "display_name", "") or getattr(attachment, "display_name", "") or "Data volume",
                            "size_gb": getattr(volume, "size_in_gbs", None),
                            "state": getattr(volume, "lifecycle_state", "") or getattr(attachment, "lifecycle_state", ""),
                            "attachment_type": getattr(attachment, "attachment_type", ""),
                            "device": getattr(attachment, "device", ""),
                            "is_read_only": bool(getattr(attachment, "is_read_only", False)),
                        }
                    )
            except Exception as exc:
                logger.info("Unable to resolve data volumes for %s: %s", instance.id, exc)
                volume_scan_warnings.append("Attached data volume details could not be read.")

            shape_config = getattr(instance, "shape_config", None)
            vm_list.append(
                {
                    "id": instance.id,
                    "name": instance.display_name,
                    "state": instance.lifecycle_state,
                    "os": image_cache.get(image_id) or "Unknown",
                    "shape": getattr(instance, "shape", "") or "Unknown",
                    "ocpus": getattr(shape_config, "ocpus", None),
                    "memory_gb": getattr(shape_config, "memory_in_gbs", None),
                    "private_ip": ", ".join(private_ips),
                    "public_ip": ", ".join(public_ips),
                    "boot_volume": boot_volumes[0] if boot_volumes else None,
                    "data_volumes": data_volumes,
                    "volume_scan_status": "partial" if volume_scan_warnings else "ok",
                    "volume_scan_warnings": volume_scan_warnings,
                }
            )

        return vm_list
    except Exception as e:
        raise_operation_error(500, "List VMs", e, "Check the OCI profile, compartment OCID, region, and API key.")

@app.get("/list-buckets/{profile}")
async def list_buckets(profile: str):
    try:
        config = oci.config.from_file(CONFIG_PATH, profile)
        os_client = oci.object_storage.ObjectStorageClient(config)
        ns = os_client.get_namespace().data
        comp = config.get("storage_compartment", config.get("compartment"))
        buckets = os_client.list_buckets(ns, comp).data
        return [{"name": b.name} for b in buckets]
    except Exception as e:
        raise_operation_error(500, "List buckets", e, "Check storage compartment access for this OCI profile.")


@app.get("/bucket-protection")
async def bucket_protection(profile_name: str = Query(...), bucket_name: str = Query(...)):
    try:
        bucket_name = destination_bucket_name(bucket_name)
        _, os_client, namespace = object_storage_context(profile_name)
        bucket = get_bucket_with_auto_tiering(os_client, namespace, bucket_name)
        lifecycle_rules = get_lifecycle_rules(os_client, namespace, bucket_name)
        retention_rules = []
        try:
            retention_collection = os_client.list_retention_rules(namespace, bucket_name).data
            retention_rules = list(getattr(retention_collection, "items", None) or [])
        except oci.exceptions.ServiceError as exc:
            if exc.status not in (401, 403, 404):
                raise
            logger.info("Unable to list retention rules for bucket '%s': %s", bucket_name, exc)

        versioning = getattr(bucket, "versioning", "") or "Disabled"
        storage_tier = getattr(bucket, "storage_tier", "") or "Standard"
        auto_tiering = normalized_bucket_auto_tiering(getattr(bucket, "auto_tiering", ""))
        has_infrequent_access_lifecycle_rule = any(is_infrequent_access_lifecycle_rule(rule) for rule in lifecycle_rules)
        retention_rule_details = [retention_rule_summary(rule) for rule in retention_rules]
        return {
            "profile_name": profile_name,
            "bucket_name": bucket_name,
            "storage_tier": storage_tier,
            "auto_tiering": auto_tiering,
            "auto_tiering_enabled": auto_tiering == "InfrequentAccess",
            "has_infrequent_access_lifecycle_rule": has_infrequent_access_lifecycle_rule,
            "versioning": versioning,
            "versioning_enabled": str(versioning).lower() == "enabled",
            "versioning_suspended": str(versioning).lower() == "suspended",
            "lifecycle_rule_count": len(lifecycle_rules),
            "managed_lifecycle_rule_count": len(
                [
                    rule
                    for rule in lifecycle_rules
                    if str(getattr(rule, "name", "")).startswith("oci-migrator-")
                ]
            ),
            "retention_rule_count": len(retention_rule_details),
            "retention_rules": retention_rule_details,
            "can_enable_versioning": str(versioning).lower() != "enabled" and not retention_rule_details,
            "can_suspend_versioning": str(versioning).lower() == "enabled",
            "can_enable_auto_tiering": not has_infrequent_access_lifecycle_rule,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise_operation_error(
            500,
            "Read bucket protection",
            e,
            "Check Object Storage permissions for bucket metadata, lifecycle policies, and retention rules.",
        )


@app.post("/bucket-versioning")
async def update_bucket_versioning(req: BucketVersioningReq):
    try:
        bucket_name = destination_bucket_name(req.bucket_name)
        versioning = normalize_update_versioning(req.versioning)
        _, os_client, namespace = object_storage_context(req.profile_name)
        if versioning == "Enabled":
            retention_collection = os_client.list_retention_rules(namespace, bucket_name).data
            retention_rules = list(getattr(retention_collection, "items", None) or [])
        else:
            retention_rules = []
        if versioning == "Enabled" and retention_rules:
            raise HTTPException(
                status_code=400,
                detail="Object Versioning cannot be enabled when OCI retention rules are active on the bucket.",
            )

        details = oci.object_storage.models.UpdateBucketDetails(versioning=versioning)
        os_client.update_bucket(namespace, bucket_name, details)
        return {"message": f"Object Versioning set to {versioning} on bucket '{bucket_name}'.", "versioning": versioning}
    except HTTPException:
        raise
    except Exception as e:
        raise_operation_error(
            500,
            "Update bucket versioning",
            e,
            "Check Object Storage bucket update permissions and ensure no OCI retention rule blocks enabling versioning.",
        )


@app.post("/bucket-versioning/enable")
async def enable_bucket_versioning(req: BucketProtectionReq):
    return await update_bucket_versioning(
        BucketVersioningReq(profile_name=req.profile_name, bucket_name=req.bucket_name, versioning="Enabled")
    )


@app.post("/bucket-auto-tiering")
async def update_bucket_auto_tiering(req: BucketAutoTieringReq):
    try:
        bucket_name = destination_bucket_name(req.bucket_name)
        auto_tiering = normalize_auto_tiering(req.auto_tiering)
        _, os_client, namespace = object_storage_context(req.profile_name)
        lifecycle_rules = get_lifecycle_rules(os_client, namespace, bucket_name)
        if auto_tiering == "InfrequentAccess" and any(is_infrequent_access_lifecycle_rule(rule) for rule in lifecycle_rules):
            raise HTTPException(
                status_code=400,
                detail="OCI does not allow Auto-Tiering when a lifecycle rule moves objects to Infrequent Access.",
            )

        details = oci.object_storage.models.UpdateBucketDetails(auto_tiering=auto_tiering)
        os_client.update_bucket(namespace, bucket_name, details)
        return {"message": f"Auto-Tiering set to {auto_tiering} on bucket '{bucket_name}'.", "auto_tiering": auto_tiering}
    except HTTPException:
        raise
    except Exception as e:
        raise_operation_error(
            500,
            "Update bucket Auto-Tiering",
            e,
            "Check Object Storage bucket update permissions and lifecycle policy conflicts.",
        )


@app.get("/bucket-lifecycle-policy")
async def get_bucket_lifecycle_policy(profile_name: str = Query(...), bucket_name: str = Query(...)):
    try:
        bucket_name = destination_bucket_name(bucket_name)
        _, os_client, namespace = object_storage_context(profile_name)
        rules = get_lifecycle_rules(os_client, namespace, bucket_name)
        return {
            "profile_name": profile_name,
            "bucket_name": bucket_name,
            "lifecycle_policy": managed_lifecycle_policy_from_rules(BUCKET_SETTINGS_LIFECYCLE_KEY, rules),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise_operation_error(
            500,
            "Read bucket lifecycle policy",
            e,
            "Check Object Storage permissions for lifecycle policy management.",
        )


@app.put("/bucket-lifecycle-policy")
async def update_bucket_lifecycle_policy(req: BucketLifecyclePolicyReq):
    try:
        lifecycle_policy = normalize_lifecycle_policy(req.lifecycle_policy, BUCKET_SETTINGS_LIFECYCLE_KEY)
        rule_count = apply_job_lifecycle_policy(
            BUCKET_SETTINGS_LIFECYCLE_KEY,
            req.profile_name,
            req.bucket_name,
            lifecycle_policy,
        )
        return {
            "message": f"Bucket lifecycle policy updated. Managed rules: {rule_count}.",
            "lifecycle_policy": lifecycle_policy,
            "managed_rule_count": rule_count,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise_operation_error(
            500,
            "Update bucket lifecycle policy",
            e,
            "Check Object Storage permissions and Auto-Tiering conflicts.",
        )


@app.get("/list-objects/{profile_name}/{bucket_name}")
async def list_objects(profile_name: str, bucket_name: str):
    try:
        config = oci.config.from_file(CONFIG_PATH, profile_name)
        storage_client = oci.object_storage.ObjectStorageClient(config)
        namespace = storage_client.get_namespace().data
        objects = storage_client.list_objects(namespace, bucket_name, fields='size,timeCreated').data
        object_list = [
            {
                "name": obj.name, 
                "size": obj.size if obj.size is not None else 0,
                "created": obj.time_created.isoformat() if obj.time_created else ""
            } 
            for obj in objects.objects
        ]
        return object_list
    except Exception as e:
        raise_operation_error(500, "List objects", e, "Check bucket name and object storage permissions.")

# Skapa Bucket
@app.post("/create-bucket")
async def create_bucket(req: CreateBucketReq):
    try:
        storage_tier = normalize_storage_tier(req.storage_tier)
        auto_tiering = normalize_auto_tiering(req.auto_tiering)
        versioning = normalize_versioning(req.versioning)
        if storage_tier == "Archive" and auto_tiering == "InfrequentAccess":
            raise HTTPException(status_code=400, detail="Auto-Tiering is only supported for Standard buckets.")

        config = oci.config.from_file(CONFIG_PATH, req.profile_name)
        os_client = oci.object_storage.ObjectStorageClient(config)
        namespace = os_client.get_namespace().data
        comp_id = config.get("storage_compartment", config.get("compartment"))
        
        details = oci.object_storage.models.CreateBucketDetails(
            name=req.bucket_name,
            compartment_id=comp_id,
            storage_tier=storage_tier,
            versioning=versioning,
            auto_tiering=auto_tiering,
        )
        os_client.create_bucket(namespace, details)
        return {"message": f"Bucket '{req.bucket_name}' created"}
    except HTTPException:
        raise
    except Exception as e:
        raise_operation_error(500, "Create bucket", e, "Check that the bucket name is unique and the profile has permission.")

# Skapa Mapp
@app.post("/create-folder")
async def create_folder(req: CreateFolderReq):
    try:
        config = oci.config.from_file(CONFIG_PATH, req.profile_name)
        os_client = oci.object_storage.ObjectStorageClient(config)
        namespace = os_client.get_namespace().data
        
        folder_path = req.folder_name if req.folder_name.endswith('/') else f"{req.folder_name}/"
        os_client.put_object(namespace, req.bucket_name, folder_path, b"")
        
        return {"message": f"Folder '{folder_path}' created"}
    except Exception as e:
        raise_operation_error(500, "Create folder", e, "Check bucket write permission.")

# Ta bort fil/objekt
@app.delete("/delete-object/{profile_name}/{bucket_name}/{object_name:path}")
async def delete_object(profile_name: str, bucket_name: str, object_name: str):
    try:
        config = oci.config.from_file(CONFIG_PATH, profile_name)
        os_client = oci.object_storage.ObjectStorageClient(config)
        namespace = os_client.get_namespace().data
        
        os_client.delete_object(namespace, bucket_name, object_name)
        return {"message": "Object deleted"}
    except Exception as e:
        raise_operation_error(500, "Delete object", e, "Check object path and delete permission.")

# --- 6. VM Migration Tasks & Progress ---
@app.post("/start-bulk-migration")
async def start_bulk_migration(job: BulkMigrationJob):
    tasks = []
    try:
        config = oci.config.from_file(CONFIG_PATH, job.dest_profile)
        dest_comp = config.get("compartment", config.get("tenancy"))

        for vm_id in job.vm_ids:
            run_id = str(uuid.uuid4())
            upsert_job_run(
                {
                    "id": run_id,
                    "kind": "vm_migration",
                    "job_name": f"VM migration {vm_id}",
                    "status": "queued",
                    "trigger": "manual",
                    "source_profile": job.source_profile,
                    "dest_profile": job.dest_profile,
                    "dest_bucket": job.bucket_name,
                    "vm_id": vm_id,
                    "details": "Queued for worker.",
                }
            )
            try:
                task = migrate_single_vm.apply_async(
                    args=[job.source_profile, job.dest_profile, vm_id, dest_comp, job.bucket_name],
                    task_id=run_id,
                )
            except Exception as e:
                upsert_job_run(
                    {
                        "id": run_id,
                        "status": "failed",
                        "details": "Unable to queue worker task.",
                        "error": str(e),
                    }
                )
                raise
            tasks.append({"vm_id": vm_id, "task_id": task.id, "run_id": run_id})
        
        return {"message": f"Started migration for {len(job.vm_ids)} VMs", "tasks": tasks}
    except HTTPException:
        raise
    except Exception as e:
        raise_operation_error(500, "Start VM migration", e, "Check Redis, Celery worker, and the destination profile.")

@app.get("/migration-status/{task_id}")
async def get_migration_status(task_id: str):
    task_result = AsyncResult(task_id)
    history_run = get_job_run(task_id)
    response = {"task_id": task_id, "status": task_result.status}
    
    if task_result.state == 'PROGRESS':
        response["details"] = task_result.info.get("step", "Processing...")
    elif task_result.state == 'SUCCESS':
        response["details"] = task_result.get()
    elif task_result.state == 'FAILURE':
        response["details"] = str(task_result.info)
    elif history_run:
        response["status"] = history_status_for_api(history_run)
        response["details"] = history_run.get("details") or history_run.get("error") or response["status"]
        
    return response


# --- 7. Frontend Static App ---
if (FRONTEND_DIST_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST_DIR / "assets")), name="frontend-assets")


def frontend_file_response(relative_path: str = "index.html") -> FileResponse:
    target = (FRONTEND_DIST_DIR / relative_path).resolve()
    if FRONTEND_DIST_DIR not in target.parents and target != FRONTEND_DIST_DIR:
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Frontend build not found. Run npm run build.")
    return FileResponse(target)


@app.get("/", include_in_schema=False)
async def serve_frontend_root():
    return frontend_file_response()


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    if full_path in {"index.html", "vite.svg", "favicon.ico", "favicon.svg"} or full_path.startswith("assets/"):
        try:
            return frontend_file_response(full_path)
        except HTTPException:
            pass
    return frontend_file_response()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
