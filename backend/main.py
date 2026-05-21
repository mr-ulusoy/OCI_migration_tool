import configparser
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import oci
import uvicorn
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional
from job_store import JOB_HISTORY_FILE, get_job_run, list_job_runs, upsert_job_run
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
SESSIONS: dict[str, float] = {}


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


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


def create_session_token() -> str:
    config = get_runtime_config()
    ttl = int(config.get("session_ttl_seconds", 43200))
    token = secrets.token_urlsafe(48)
    expires_at = time.time() + max(ttl, 300)

    with SESSION_LOCK:
        now = time.time()
        expired_tokens = [session for session, expiry in SESSIONS.items() if expiry <= now]
        for session in expired_tokens:
            SESSIONS.pop(session, None)
        SESSIONS[token] = expires_at

    return token


def invalidate_session_token(token: str) -> None:
    with SESSION_LOCK:
        SESSIONS.pop(token, None)


def session_token_is_valid(token: str) -> bool:
    with SESSION_LOCK:
        expires_at = SESSIONS.get(token)
        if not expires_at:
            return False
        if expires_at <= time.time():
            SESSIONS.pop(token, None)
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
LOCAL_SHARE_HELPER = Path(os.getenv("OCI_MIGRATOR_LOCAL_SHARE_HELPER", "/usr/local/sbin/oci-migrator-local-share")).resolve()
try:
    LOCAL_SHARE_TIMEOUT_SECONDS = int(os.getenv("OCI_MIGRATOR_LOCAL_SHARE_TIMEOUT_SECONDS", "300"))
except ValueError:
    LOCAL_SHARE_TIMEOUT_SECONDS = 300

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
    schedule: ScheduleSchema

class BulkMigrationJob(BaseModel):
    vm_ids: List[str]
    source_profile: str
    dest_profile: str
    bucket_name: str

# NYA SCHEMAS FÖR STORAGE EXPLORER
class CreateBucketReq(BaseModel):
    profile_name: str
    bucket_name: str

class CreateFolderReq(BaseModel):
    profile_name: str
    bucket_name: str
    folder_name: str

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


def read_runtime_env() -> dict[str, str]:
    return _read_env_file(ENV_FILE_PATH) if os.path.exists(ENV_FILE_PATH) else {}


def redis_url_from_runtime() -> str:
    runtime_env = read_runtime_env()
    return runtime_env.get("OCI_MIGRATOR_REDIS_URL") or os.getenv(
        "OCI_MIGRATOR_REDIS_URL", "redis://localhost:6379/0"
    )


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
        SESSIONS.clear()

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
        "checks": checks,
    }


@app.get("/job-history")
async def job_history(limit: int = Query(default=100, ge=1, le=300)):
    return {"runs": list_job_runs(limit)}


@app.get("/job-history/{run_id}")
async def job_history_item(run_id: str):
    run = get_job_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Job run not found.")
    return run


@app.get("/runtime-config/export")
async def export_runtime_config():
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
    headers = {"Content-Disposition": f'attachment; filename="oci-migrator-runtime-{timestamp}.zip"'}
    return StreamingResponse(archive_buffer, media_type="application/zip", headers=headers)

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
    with JOBS_LOCK:
        jobs = load_jobs()
        job_dict = job.dict()
        existing = next((i for i, j in enumerate(jobs) if j['name'] == job.name), None)

        if existing is not None:
            jobs[existing] = job_dict
        else:
            jobs.append(job_dict)

        write_jobs_atomically(jobs)

    schedule_state = "ready for scheduling" if job.schedule.frequency != "none" else "saved for manual runs"
    return {"message": f"Job '{job.name}' {schedule_state}"}

@app.get("/list-jobs")
async def list_jobs():
    return load_jobs()

@app.delete("/delete-job/{job_name}")
async def delete_job(job_name: str):
    with JOBS_LOCK:
        jobs = [j for j in load_jobs() if j['name'] != job_name]
        write_jobs_atomically(jobs)
    return {"message": "Job deleted"}

# --- 3. Live Logs ---
@app.get("/job-log/{job_name}")
async def get_job_log(job_name: str):
    log_file = f"/tmp/rclone_{normalize_job_name(job_name)}.log"
    if not os.path.exists(log_file):
        return {"log": "Waiting for Rclone to start reporting..."}
    
    try:
        with open(log_file, "r") as f:
            lines = f.readlines()
            last_lines = lines[-15:] if len(lines) > 15 else lines
            return {"log": "".join(last_lines)}
    except Exception as e:
        return {"log": f"Error reading log: {str(e)}"}

# --- 4. Rclone Remotes & Buckets ---
@app.get("/list-remotes")
async def list_remotes():
    if not os.path.exists(RCLONE_CONF): return {"remotes": []}
    parser = configparser.ConfigParser()
    parser.read(RCLONE_CONF)
    return {"remotes": parser.sections()}

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
    upsert_job_run(
        {
            "id": run_id,
            "kind": "data_sync",
            "job_name": job.name,
            "status": "queued",
            "trigger": "manual",
            "source": job.source_remote,
            "destination": destination,
            "details": "Queued for worker.",
            "log_file": f"/tmp/rclone_{safe_job_name}.log",
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
        comp_id = config.get("compartment", config.get("tenancy"))
        res = compute.list_instances(compartment_id=comp_id)
        return [{"id": i.id, "name": i.display_name, "state": i.lifecycle_state} for i in res.data if i.lifecycle_state != "TERMINATED"]
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
        config = oci.config.from_file(CONFIG_PATH, req.profile_name)
        os_client = oci.object_storage.ObjectStorageClient(config)
        namespace = os_client.get_namespace().data
        comp_id = config.get("storage_compartment", config.get("compartment"))
        
        details = oci.object_storage.models.CreateBucketDetails(
            name=req.bucket_name,
            compartment_id=comp_id
        )
        os_client.create_bucket(namespace, details)
        return {"message": f"Bucket '{req.bucket_name}' created"}
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
    if full_path in {"index.html", "vite.svg", "favicon.ico"} or full_path.startswith("assets/"):
        try:
            return frontend_file_response(full_path)
        except HTTPException:
            pass
    return frontend_file_response()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
