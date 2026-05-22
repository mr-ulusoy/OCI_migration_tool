import configparser
import base64
import hashlib
import hmac
import io
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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import List, Optional
from job_logs import JOB_LOG_DIR, job_log_path, legacy_job_log_path, resolve_readable_log_path, tail_file
from job_store import JOB_HISTORY_FILE, get_job_run, list_job_runs, locked_history_file, upsert_job_run
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
JOB_LOGROTATE_FILE = Path(os.getenv("OCI_MIGRATOR_JOB_LOGROTATE_FILE", "/etc/logrotate.d/migrator-job-logs"))
UPGRADE_HELPER = Path(os.getenv("OCI_MIGRATOR_UPGRADE_HELPER", "/usr/local/sbin/oci-migrator-upgrade")).resolve()
UPGRADE_STATUS_FILE = Path(
    os.getenv("OCI_MIGRATOR_UPGRADE_STATUS_FILE", "/var/lib/oci-migrator/upgrade/status.json")
).resolve()
UPGRADE_LOG_FILE = Path(os.getenv("OCI_MIGRATOR_UPGRADE_LOG_FILE", "/var/log/oci-migrator/upgrade.log")).resolve()
UPGRADE_LOCK_DIR = UPGRADE_STATUS_FILE.parent / "upgrade.lock"
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


class LifecyclePolicyConfig(BaseModel):
    enabled: bool = False
    prefix: str = ""
    infrequent_access_after_days: Optional[int] = None
    archive_after_days: Optional[int] = None
    delete_after_days: Optional[int] = None
    previous_versions_delete_after_days: Optional[int] = None


class DataSyncJob(BaseModel):
    name: str
    source_remote: str
    dest_profile: str
    dest_bucket: str
    sync_mode: str = "copy"
    transfers: int = 4
    checkers: int = 8
    buffer_size: str = "16M"
    is_active: bool = True
    metadata_tags: List[MetadataTag] = Field(default_factory=list)
    lifecycle_policy: LifecyclePolicyConfig = Field(default_factory=LifecyclePolicyConfig)
    schedule: ScheduleSchema

class BulkMigrationJob(BaseModel):
    vm_ids: List[str]
    source_profile: str
    dest_profile: str
    bucket_name: str


class JobLogSettingsRequest(BaseModel):
    max_size: str
    retention_days: int


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


def share_host_from_request(request: Request) -> str:
    host = request.url.hostname or request.headers.get("host", "server").split(":", 1)[0]
    return host.strip("[]") or "server"


def local_share_helper_command() -> list[str]:
    if not LOCAL_SHARE_HELPER.is_file():
        raise HTTPException(
            status_code=503,
            detail="Local SMB share helper is not installed. Rerun ./install.sh and try again.",
        )

    if os.geteuid() == 0:
        return [str(LOCAL_SHARE_HELPER)]

    sudo_path = shutil.which("sudo")
    if not sudo_path:
        raise HTTPException(
            status_code=503,
            detail="sudo is required for local SMB share setup. Rerun ./install.sh and try again.",
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
            "Configure local SMB share",
            exc,
            "Samba installation/configuration took too long. Check apt, systemd, and firewall status on the server.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise_operation_error(
            500,
            "Configure local SMB share",
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


def normalize_lifecycle_policy(policy: LifecyclePolicyConfig | dict | None) -> dict:
    if policy is None:
        policy = LifecyclePolicyConfig()
    if isinstance(policy, LifecyclePolicyConfig):
        policy_data = policy.model_dump()
    else:
        policy_data = dict(policy)

    normalized = {
        "enabled": bool(policy_data.get("enabled", False)),
        "prefix": normalize_lifecycle_prefix(policy_data.get("prefix", "")),
        "infrequent_access_after_days": normalize_lifecycle_days(
            policy_data.get("infrequent_access_after_days"),
            "Move to Infrequent Access after",
        ),
        "archive_after_days": normalize_lifecycle_days(policy_data.get("archive_after_days"), "Archive after"),
        "delete_after_days": normalize_lifecycle_days(policy_data.get("delete_after_days"), "Delete after"),
        "previous_versions_delete_after_days": normalize_lifecycle_days(
            policy_data.get("previous_versions_delete_after_days"),
            "Delete previous versions after",
        ),
    }

    if normalized["enabled"]:
        if not any(
            normalized[key]
            for key in (
                "infrequent_access_after_days",
                "archive_after_days",
                "delete_after_days",
                "previous_versions_delete_after_days",
            )
        ):
            raise HTTPException(
                status_code=400,
                detail="Enable at least one lifecycle action: Infrequent Access, Archive, delete, or delete previous versions.",
            )
        if (
            normalized["infrequent_access_after_days"]
            and normalized["archive_after_days"]
            and normalized["archive_after_days"] <= normalized["infrequent_access_after_days"]
        ):
            raise HTTPException(status_code=400, detail="Archive after days must be greater than Infrequent Access after days.")
        if (
            normalized["infrequent_access_after_days"]
            and normalized["delete_after_days"]
            and normalized["delete_after_days"] <= normalized["infrequent_access_after_days"]
        ):
            raise HTTPException(status_code=400, detail="Delete after days must be greater than Infrequent Access after days.")
        if (
            normalized["archive_after_days"]
            and normalized["delete_after_days"]
            and normalized["delete_after_days"] <= normalized["archive_after_days"]
        ):
            raise HTTPException(status_code=400, detail="Delete after days must be greater than archive after days.")

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


def build_lifecycle_rule(name: str, target: str, action: str, days: int, prefix: str):
    object_filter = None
    if prefix:
        object_filter = oci.object_storage.models.ObjectNameFilter(
            inclusion_prefixes=[prefix],
            inclusion_patterns=[],
            exclusion_patterns=[],
        )
    return oci.object_storage.models.ObjectLifecycleRule(
        name=name,
        target=target,
        action=action,
        time_amount=days,
        time_unit=oci.object_storage.models.ObjectLifecycleRule.TIME_UNIT_DAYS,
        is_enabled=True,
        object_name_filter=object_filter,
    )


def desired_lifecycle_rules(job_name: str, policy: dict) -> list:
    prefix = policy.get("prefix", "")
    safe_job_name = normalize_job_name(job_name).lower()
    rules = []
    if policy.get("infrequent_access_after_days"):
        rules.append(
            build_lifecycle_rule(
                f"oci-migrator-{safe_job_name}-infrequent-access",
                "objects",
                "INFREQUENT_ACCESS",
                policy["infrequent_access_after_days"],
                prefix,
            )
        )
    if policy.get("archive_after_days"):
        rules.append(
            build_lifecycle_rule(
                f"oci-migrator-{safe_job_name}-archive",
                "objects",
                "ARCHIVE",
                policy["archive_after_days"],
                prefix,
            )
        )
    if policy.get("delete_after_days"):
        rules.append(
            build_lifecycle_rule(
                f"oci-migrator-{safe_job_name}-delete",
                "objects",
                "DELETE",
                policy["delete_after_days"],
                prefix,
            )
        )
    if policy.get("previous_versions_delete_after_days"):
        rules.append(
            build_lifecycle_rule(
                f"oci-migrator-{safe_job_name}-delete-previous",
                "previous-object-versions",
                "DELETE",
                policy["previous_versions_delete_after_days"],
                prefix,
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
    if policy.get("enabled") and policy.get("infrequent_access_after_days"):
        auto_tiering = str(getattr(bucket, "auto_tiering", "") or "")
        if auto_tiering == "InfrequentAccess":
            raise HTTPException(
                status_code=400,
                detail="OCI does not allow an Infrequent Access lifecycle rule while Auto-Tiering is enabled on the bucket.",
            )
    managed_names = lifecycle_managed_rule_names(job_name)
    existing_rules = [
        rule
        for rule in get_lifecycle_rules(client, namespace, bucket_name)
        if getattr(rule, "name", "") not in managed_names
    ]
    new_rules = desired_lifecycle_rules(job_name, policy) if policy.get("enabled") else []
    put_lifecycle_rules(client, namespace, bucket_name, [*existing_rules, *new_rules])
    return len(new_rules)


def remove_job_lifecycle_policy(job_name: str, profile_name: str, destination: str) -> None:
    bucket_name = destination_bucket_name(destination)
    _, client, namespace = object_storage_context(profile_name)
    managed_names = lifecycle_managed_rule_names(job_name)
    existing_rules = get_lifecycle_rules(client, namespace, bucket_name)
    next_rules = [rule for rule in existing_rules if getattr(rule, "name", "") not in managed_names]
    if len(next_rules) != len(existing_rules):
        put_lifecycle_rules(client, namespace, bucket_name, next_rules)


def managed_lifecycle_policy_from_rules(managed_key: str, rules: list) -> dict:
    managed_names = lifecycle_managed_rule_names(managed_key)
    policy = {
        "enabled": False,
        "prefix": "",
        "infrequent_access_after_days": None,
        "archive_after_days": None,
        "delete_after_days": None,
        "previous_versions_delete_after_days": None,
    }
    action_fields = {
        f"oci-migrator-{normalize_job_name(managed_key).lower()}-infrequent-access": "infrequent_access_after_days",
        f"oci-migrator-{normalize_job_name(managed_key).lower()}-archive": "archive_after_days",
        f"oci-migrator-{normalize_job_name(managed_key).lower()}-delete": "delete_after_days",
        f"oci-migrator-{normalize_job_name(managed_key).lower()}-delete-previous": "previous_versions_delete_after_days",
    }

    for rule in rules:
        rule_name = getattr(rule, "name", "")
        if rule_name not in managed_names:
            continue
        policy["enabled"] = True
        policy[action_fields.get(rule_name, "")] = getattr(rule, "time_amount", None)
        object_filter = getattr(rule, "object_name_filter", None)
        prefixes = getattr(object_filter, "inclusion_prefixes", None) if object_filter else None
        if prefixes and not policy["prefix"]:
            policy["prefix"] = prefixes[0]

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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect.")

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
@app.get("/health")
async def health():
    runtime_config = get_runtime_config()
    admin_password_configured = bool(runtime_config.get("admin_password_hash"))
    env_file_exists = os.path.isfile(ENV_FILE_PATH)
    oci_config_exists = os.path.isfile(CONFIG_PATH)
    rclone_config_exists = os.path.isfile(RCLONE_CONF)
    rclone_installed = bool(shutil.which("rclone"))
    frontend_build_exists = (FRONTEND_DIST_DIR / "index.html").is_file()
    job_log_dir_exists = JOB_LOG_DIR.is_dir()
    upgrade_helper_exists = UPGRADE_HELPER.is_file()
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
        "upgrade_helper": health_check_item(
            "ok" if upgrade_helper_exists else "warn",
            f"Upgrade helper is installed: {UPGRADE_HELPER}" if upgrade_helper_exists else f"Upgrade helper is not installed: {UPGRADE_HELPER}",
        ),
        "timezone": health_check_item(
            "ok" if configured_timezone == EXPECTED_TIMEZONE else "warn",
            f"Server timezone is {configured_timezone}." if configured_timezone == EXPECTED_TIMEZONE else f"Expected timezone {EXPECTED_TIMEZONE}, current timezone {configured_timezone or 'unknown'}.",
        ),
        "time_sync": health_check_item(
            "ok" if ntp_synchronized == "yes" else "warn",
            "NTP is synchronized." if ntp_synchronized == "yes" else f"NTP is not synchronized yet. Configured servers: {NTP_SERVERS}",
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
        "timezone": configured_timezone or EXPECTED_TIMEZONE,
        "checks": checks,
    }


@app.get("/job-history")
async def job_history(limit: int = Query(default=100, ge=1, le=300)):
    return {"runs": list_job_runs(limit)}


@app.get("/job-log-settings")
async def get_job_log_settings():
    return current_job_log_settings()


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
        "log": tail_file(path, max_lines=max_lines),
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
    lifecycle_policy = normalize_lifecycle_policy(job.lifecycle_policy)
    existing_job = next((j for j in load_jobs() if j.get("name") == job.name), None)
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
        job_dict["metadata_tags"] = metadata_tags
        job_dict["lifecycle_policy"] = lifecycle_policy
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

    return {"log": tail_file(legacy_path, max_lines=500), "exists": True, "log_file": str(legacy_path)}

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
    run_id = str(uuid.uuid4())
    safe_job_name = normalize_job_name(job.name)
    destination = f"{job.dest_profile}_rclone:{job.dest_bucket}"
    metadata_tags = normalize_metadata_tags(job.metadata_tags)
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
    gcp_file: Optional[UploadFile] = File(None)
):
    parser = configparser.ConfigParser()
    saved_local_path = None
    saved_share = None
    try:
        with RCLONE_LOCK:
            if os.path.exists(RCLONE_CONF):
                parser.read(RCLONE_CONF)

            previous_share_name = parser.get(name, 'oci_migrator_share_name', fallback='') if parser.has_section(name) else ''

            if not parser.has_section(name):
                parser.add_section(name)

            for option in (
                'oci_migrator_local_mode',
                'oci_migrator_local_path',
                'oci_migrator_local_display_name',
                'oci_migrator_share_access',
                'oci_migrator_share_name',
                'oci_migrator_share_username',
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
                if local_mode != "server_folder" and share_access != "none":
                    raise HTTPException(status_code=400, detail="SMB sharing is only supported for server local folders.")

                if share_access != "none":
                    if share_access not in {"everyone", "user"}:
                        raise HTTPException(status_code=400, detail="Unsupported SMB share access mode.")

                    share_name = normalize_smb_share_name(local_share_name or display_name)
                    for section in parser.sections():
                        if section != name and parser.get(section, 'oci_migrator_share_name', fallback='') == share_name:
                            raise HTTPException(
                                status_code=400,
                                detail=f"SMB share name is already used by remote '{section}'.",
                            )

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

                parser.set(name, 'type', 'local')
                parser.set(name, 'oci_migrator_local_mode', local_mode)
                parser.set(name, 'oci_migrator_local_path', str(local_path))
                parser.set(name, 'oci_migrator_local_display_name', display_name)
                if saved_share:
                    parser.set(name, 'oci_migrator_share_access', saved_share["access"])
                    parser.set(name, 'oci_migrator_share_name', saved_share["name"])
                    if saved_share["username"]:
                        parser.set(name, 'oci_migrator_share_username', saved_share["username"])
                saved_local_path = str(local_path)
            else:
                raise HTTPException(status_code=400, detail="Unsupported remote provider")

            current_share_name = saved_share["name"] if saved_share else ""
            if previous_share_name and previous_share_name != current_share_name:
                disable_local_share(previous_share_name)

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
        comp_id = config.get("compartment", config.get("tenancy"))
        res = compute.list_instances(compartment_id=comp_id)
        image_cache = {}
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
                attachments = compute.list_vnic_attachments(
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
        auto_tiering = getattr(bucket, "auto_tiering", "") or "Disabled"
        retention_rule_details = [retention_rule_summary(rule) for rule in retention_rules]
        return {
            "profile_name": profile_name,
            "bucket_name": bucket_name,
            "storage_tier": storage_tier,
            "auto_tiering": auto_tiering,
            "auto_tiering_enabled": auto_tiering == "InfrequentAccess",
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
            "can_enable_auto_tiering": not any(
                getattr(rule, "action", "") == "INFREQUENT_ACCESS" for rule in lifecycle_rules
            ),
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
        if auto_tiering == "InfrequentAccess" and any(
            getattr(rule, "action", "") == "INFREQUENT_ACCESS" for rule in lifecycle_rules
        ):
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
        lifecycle_policy = normalize_lifecycle_policy(req.lifecycle_policy)
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
