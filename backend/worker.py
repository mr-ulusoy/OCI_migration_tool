import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from celery import Celery
import oci
from job_logs import ensure_job_log_dir, job_log_path, tail_file
from job_store import update_job_run

logging.basicConfig(level=os.getenv("OCI_MIGRATOR_LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


def redis_url() -> str:
    return os.getenv("OCI_MIGRATOR_REDIS_URL", "redis://localhost:6379/0")


def rclone_timeout_seconds() -> int:
    """Max runtime for a single rclone task.

    Prevents a hung rclone process from occupying a Celery worker slot indefinitely.
    """
    try:
        return int(os.getenv("OCI_MIGRATOR_RCLONE_TIMEOUT_SECONDS", "7200"))  # default 2h
    except ValueError:
        return 7200


celery_app = Celery('tasks', broker=redis_url(), backend=redis_url())

# --- HELPERS ---
def get_client(ctype, profile):
    cfg = oci.config.from_file(os.path.expanduser("~/.oci/config"), profile)
    return getattr(oci.core, f"{ctype}Client")(cfg) if hasattr(oci.core, f"{ctype}Client") else getattr(oci.object_storage, f"{ctype}Client")(cfg)

# --- TASK 1: VM Migration ---
@celery_app.task(bind=True, max_retries=3)
def migrate_single_vm(self, src_p, dst_p, vm_id, dst_comp, bucket):
    run_id = self.request.id

    def set_progress(step: str) -> None:
        self.update_state(state='PROGRESS', meta={'step': step})
        update_job_run(run_id, status="running", details=step)

    try:
        set_progress('1/6: Connecting to OCI & Fetching VM...')
        c_src = get_client("Compute", src_p)
        inst = c_src.get_instance(vm_id).data
        update_job_run(run_id, job_name=f"VM migration {inst.display_name}", vm_name=inst.display_name)
        
        if inst.lifecycle_state != 'STOPPED':
            set_progress(f'2/6: Soft-stopping VM ({inst.display_name})...')
            c_src.instance_action(vm_id, "SOFTSTOP")
            oci.wait_until(c_src, c_src.get_instance(vm_id), 'lifecycle_state', 'STOPPED', max_wait_seconds=600)

        set_progress('3/6: Creating Custom Image from Boot Volume...')
        img_name = f"migr-{inst.display_name}-{int(time.time())}"
        img = c_src.create_image(oci.core.models.CreateImageDetails(compartment_id=inst.compartment_id, instance_id=vm_id, display_name=img_name)).data
        oci.wait_until(c_src, c_src.get_image(img.id), 'lifecycle_state', 'AVAILABLE', max_wait_seconds=3600)
        
        set_progress('Turning source VM back on...')
        c_src.instance_action(vm_id, "START")

        set_progress('4/6: Building Cross-Tenant Bridge (PAR)...')
        # Cross-tenant Export via PAR
        os_dst = get_client("ObjectStorage", dst_p)
        ns_dst = os_dst.get_namespace().data
        par = os_dst.create_preauthenticated_request(ns_dst, bucket, oci.object_storage.models.CreatePreauthenticatedRequestDetails(
            name="MigrWrite", access_type="ObjectReadWrite", object_name=f"{img_name}.oci", 
            time_expires=datetime.utcnow() + timedelta(hours=48))).data
        
        par_url = f"https://objectstorage.{os_dst.base_client.config['region']}.oraclecloud.com{par.access_uri}"
        
        set_progress('5/6: Exporting Image to Destination Bucket (This takes time)...')
        exp = c_src.export_image(img.id, {"destinationType": "objectStorageUri", "destinationUri": par_url, "exportFormat": "OCI"})
        
        oci.wait_until(oci.work_requests.WorkRequestClient(c_src.base_client.config), 
                       oci.work_requests.WorkRequestClient(c_src.base_client.config).get_work_request(exp.headers["opc-work-request-id"]), 
                       'status', 'SUCCEEDED', max_wait_seconds=7200)

        set_progress('6/6: Importing Image into Destination Region...')
        get_client("Compute", dst_p).create_image(oci.core.models.CreateImageDetails(
            compartment_id=dst_comp, display_name=f"IMP-{img_name}",
            image_source_details=oci.core.models.ImageSourceViaObjectStorageTupleDetails(
                source_type="objectStorageTuple", namespace_name=ns_dst, bucket_name=bucket, object_name=f"{img_name}.oci")))
        
        update_job_run(run_id, status="success", details=f"Success! {inst.display_name} migrated.", finished_at=datetime.utcnow().isoformat() + "Z")
        return f"Success! {inst.display_name} migrated."
    except Exception as e:
        logger.exception("VM migration failed for vm_id=%s", vm_id)
        retrying = self.request.retries < self.max_retries
        update_job_run(
            run_id,
            status="retrying" if retrying else "failed",
            details=f"Retrying after error: {str(e)}" if retrying else f"Error: {str(e)}",
            error=str(e),
            finished_at=None if retrying else datetime.utcnow().isoformat() + "Z",
        )
        self.update_state(state='FAILURE', meta={'step': f"Error: {str(e)}"})
        raise self.retry(exc=e, countdown=60)

# --- TASK 2: Rclone Sync ---
def normalize_metadata_tags(metadata_tags=None):
    normalized_tags = []
    seen_keys = set()

    def normalize_metadata_key(raw_key):
        key = str(raw_key or "").strip().lower()
        suffix = key[len("opc-meta-"):] if key.startswith("opc-meta-") else key
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,118}", suffix or ""):
            return ""
        return f"opc-meta-{suffix}"

    for tag in metadata_tags or []:
        if not isinstance(tag, dict):
            continue
        key = normalize_metadata_key(tag.get("key", ""))
        value = str(tag.get("value", "")).strip()
        if not key or not value or key in seen_keys:
            continue
        seen_keys.add(key)
        normalized_tags.append({"key": key, "value": value})
    return normalized_tags


@celery_app.task(bind=True, name="worker.rclone_sync_task")
def rclone_sync_task(self, source, dest_profile, dest_bucket, mode="copy", transfers=4, checkers=8, buffer_size="16M", job_name="default", run_id=None, trigger="manual", metadata_tags=None):
    run_id = run_id or self.request.id
    dest = f"{dest_profile}_rclone:{dest_bucket}"
    metadata_tags = normalize_metadata_tags(metadata_tags)
    ensure_job_log_dir()
    log_file = job_log_path(job_name, run_id)
    update_job_run(
        run_id,
        kind="data_sync",
        status="running",
        trigger=trigger,
        source=source,
        destination=dest,
        metadata_tags=metadata_tags,
        details="Job is running.",
        log_file=str(log_file),
        started_at=datetime.utcnow().isoformat() + "Z",
    )
    
    # Rensa gammal logg om den finns för att börja om på ny kula
    if log_file.exists():
        log_file.unlink()
    log_file.touch(mode=0o640)
    os.chmod(log_file, 0o640)

    cmd = [
        "rclone", mode, source, dest,
        "--transfers", str(transfers), 
        "--checkers", str(checkers), 
        "--buffer-size", buffer_size,
        "--log-file", str(log_file),     # Skriver loggen till fil för backend-läsning
        "--log-level", "INFO",
        "--stats", "2s",                 # Uppdaterar hastighet/procent varannan sekund
        "--stats-one-line",
        "--fast-list", 
        "--retries", "10", 
        "--use-mmap"
    ]
    if metadata_tags:
        cmd.append("--metadata")
        for tag in metadata_tags:
            cmd.extend(["--metadata-set", f"{tag['key']}={tag['value']}"])
    
    timeout = rclone_timeout_seconds()

    # Kör processen. Loggning sköts direkt till filen via --log-file.
    # stdout/stderr discardas för att undvika att processen blockar pga full buffer.
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, text=True)

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error("rclone timed out after %ss (job=%s) - terminating", timeout, job_name)
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            logger.error("rclone did not terminate gracefully - killing (job=%s)", job_name)
            process.kill()
            process.wait()

        update_job_run(
            run_id,
            status="timeout",
            details=f"Timed out after {timeout} seconds.",
            error=f"Timeout after {timeout} seconds.",
            finished_at=datetime.utcnow().isoformat() + "Z",
        )
        return {"status": "timeout", "code": None, "timeout_seconds": timeout}

    status = "success" if process.returncode == 0 else "failed"
    log_tail = tail_file(log_file, max_lines=12)
    details = "Completed successfully." if process.returncode == 0 else f"Failed with exit code {process.returncode}."
    update_job_run(
        run_id,
        status=status,
        details=details,
        error="" if process.returncode == 0 else (log_tail or details),
        finished_at=datetime.utcnow().isoformat() + "Z",
    )

    return {
        "status": status,
        "code": process.returncode,
    }
