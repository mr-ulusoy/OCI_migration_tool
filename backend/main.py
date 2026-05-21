import configparser
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from threading import Lock

import oci
import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
from worker import migrate_single_vm, rclone_sync_task
from celery.result import AsyncResult 

logging.basicConfig(level=os.getenv("OCI_MIGRATOR_LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


def parse_allowed_origins() -> list[str]:
    raw_origins = os.getenv(
        "OCI_MIGRATOR_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )
    return [origin.strip() for origin in raw_origins.split(",") if origin.strip()]


ENV_FILE_PATH = os.path.expanduser(os.getenv("OCI_MIGRATOR_ENV_FILE", "~/.oci-migrator.env"))


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
    "mtime": None,
    "api_token": "",
    "allowed_origins": None,
}


def get_runtime_config() -> tuple[str, list[str]]:
    """Return (api_token, allowed_origins) reloaded when env file changes.

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
                or "http://localhost:5173,http://127.0.0.1:5173"
            )
            allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

            _CONFIG_CACHE["mtime"] = mtime
            _CONFIG_CACHE["api_token"] = api_token
            _CONFIG_CACHE["allowed_origins"] = allowed_origins

        return str(_CONFIG_CACHE["api_token"]), list(_CONFIG_CACHE["allowed_origins"])  # type: ignore[arg-type]
CONFIG_LOCK = Lock()
JOBS_LOCK = Lock()
RCLONE_LOCK = Lock()


def require_api_token(x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")) -> None:
    api_token, _ = get_runtime_config()

    if not api_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server API token is not configured.",
        )

    if x_api_token != api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token.",
        )


app = FastAPI(title="OCI Migration & Sync Engine", dependencies=[Depends(require_api_token)])


@app.middleware("http")
async def dynamic_cors_allowlist(request, call_next):
    """Dynamic CORS + allowlist enforcement.

    We implement CORS ourselves (including OPTIONS preflight) so that:
    - the allowlist can be changed without restarting the service
    - preflight requests behave consistently
    """
    origin = request.headers.get("origin")

    if origin:
        _, allowed_origins = get_runtime_config()
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
                "Access-Control-Allow-Headers": request_headers or "Content-Type, X-API-Token",
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
            }
            return JSONResponse(status_code=204, content=None, headers=headers)

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

# Säkerställ att mappar finns
os.makedirs(os.path.dirname(RCLONE_CONF), exist_ok=True)
os.makedirs(OCI_DIR, exist_ok=True)

# --- Schemas ---
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
        command = ["rclone", "lsf", f"{remote_name}:", "--max-depth", "1"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=result.stderr)
        buckets = [line.replace('/', '').strip() for line in result.stdout.split('\n') if line.strip()]
        return {"buckets": buckets}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/start-data-sync-manual")
async def start_sync_manual(job: DataSyncJob):
    task = rclone_sync_task.delay(
        job.source_remote, 
        job.dest_profile, 
        job.dest_bucket, 
        job.sync_mode,
        job.transfers,
        job.checkers,
        job.buffer_size,
        job.name.replace(' ', '_')
    )
    return {"task_id": task.id, "status": "queued"}

# NYTT: Spara Big 5 Remotes (AWS, Azure, GCP, Local)
@app.post("/save-remote")
async def save_remote(
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
    gcp_file: Optional[UploadFile] = File(None)
):
    parser = configparser.ConfigParser()
    with RCLONE_LOCK:
        if os.path.exists(RCLONE_CONF):
            parser.read(RCLONE_CONF)

        if not parser.has_section(name):
            parser.add_section(name)

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
            parser.set(name, 'type', 'local')
        else:
            raise HTTPException(status_code=400, detail="Unsupported remote provider")

        write_ini_atomically(parser, RCLONE_CONF)
        
    return {"message": "Remote saved successfully"}

# NYTT: Ta bort Remote
@app.delete("/delete-remote/{remote_name}")
async def delete_remote(remote_name: str):
    parser = configparser.ConfigParser()
    with RCLONE_LOCK:
        if os.path.exists(RCLONE_CONF):
            parser.read(RCLONE_CONF)

        if parser.has_section(remote_name):
            parser.remove_section(remote_name)
            write_ini_atomically(parser, RCLONE_CONF)
    return {"message": "Remote deleted"}

# --- 5. OCI Explorer (VMs & Buckets) ---
@app.get("/list-vms/{profile}")
async def list_vms(profile: str):
    config = oci.config.from_file(CONFIG_PATH, profile)
    compute = oci.core.ComputeClient(config)
    comp_id = config.get("compartment", config.get("tenancy"))
    res = compute.list_instances(compartment_id=comp_id)
    return [{"id": i.id, "name": i.display_name, "state": i.lifecycle_state} for i in res.data if i.lifecycle_state != "TERMINATED"]

@app.get("/list-buckets/{profile}")
async def list_buckets(profile: str):
    config = oci.config.from_file(CONFIG_PATH, profile)
    os_client = oci.object_storage.ObjectStorageClient(config)
    ns = os_client.get_namespace().data
    comp = config.get("storage_compartment", config.get("compartment"))
    buckets = os_client.list_buckets(ns, comp).data
    return [{"name": b.name} for b in buckets]

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
        raise HTTPException(status_code=500, detail=str(e))

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
        raise HTTPException(status_code=500, detail=str(e))

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
        raise HTTPException(status_code=500, detail=str(e))

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
        raise HTTPException(status_code=500, detail=str(e))

# --- 6. VM Migration Tasks & Progress ---
@app.post("/start-bulk-migration")
async def start_bulk_migration(job: BulkMigrationJob):
    tasks = []
    try:
        config = oci.config.from_file(CONFIG_PATH, job.dest_profile)
        dest_comp = config.get("compartment", config.get("tenancy"))

        for vm_id in job.vm_ids:
            task = migrate_single_vm.delay(
                job.source_profile, job.dest_profile, vm_id, dest_comp, job.bucket_name
            )
            tasks.append({"vm_id": vm_id, "task_id": task.id})
        
        return {"message": f"Started migration for {len(job.vm_ids)} VMs", "tasks": tasks}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/migration-status/{task_id}")
async def get_migration_status(task_id: str):
    task_result = AsyncResult(task_id)
    response = {"task_id": task_id, "status": task_result.status}
    
    if task_result.state == 'PROGRESS':
        response["details"] = task_result.info.get("step", "Processing...")
    elif task_result.state == 'SUCCESS':
        response["details"] = task_result.get()
    elif task_result.state == 'FAILURE':
        response["details"] = str(task_result.info)
        
    return response

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
