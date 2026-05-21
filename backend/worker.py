import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta
from celery import Celery
import oci

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
    try:
        self.update_state(state='PROGRESS', meta={'step': '1/6: Connecting to OCI & Fetching VM...'})
        c_src = get_client("Compute", src_p)
        inst = c_src.get_instance(vm_id).data
        
        if inst.lifecycle_state != 'STOPPED':
            self.update_state(state='PROGRESS', meta={'step': f'2/6: Soft-stopping VM ({inst.display_name})...'})
            c_src.instance_action(vm_id, "SOFTSTOP")
            oci.wait_until(c_src, c_src.get_instance(vm_id), 'lifecycle_state', 'STOPPED', max_wait_seconds=600)

        self.update_state(state='PROGRESS', meta={'step': '3/6: Creating Custom Image from Boot Volume...'})
        img_name = f"migr-{inst.display_name}-{int(time.time())}"
        img = c_src.create_image(oci.core.models.CreateImageDetails(compartment_id=inst.compartment_id, instance_id=vm_id, display_name=img_name)).data
        oci.wait_until(c_src, c_src.get_image(img.id), 'lifecycle_state', 'AVAILABLE', max_wait_seconds=3600)
        
        self.update_state(state='PROGRESS', meta={'step': 'Turning source VM back on...'})
        c_src.instance_action(vm_id, "START")

        self.update_state(state='PROGRESS', meta={'step': '4/6: Building Cross-Tenant Bridge (PAR)...'})
        # Cross-tenant Export via PAR
        os_dst = get_client("ObjectStorage", dst_p)
        ns_dst = os_dst.get_namespace().data
        par = os_dst.create_preauthenticated_request(ns_dst, bucket, oci.object_storage.models.CreatePreauthenticatedRequestDetails(
            name="MigrWrite", access_type="ObjectReadWrite", object_name=f"{img_name}.oci", 
            time_expires=datetime.utcnow() + timedelta(hours=48))).data
        
        par_url = f"https://objectstorage.{os_dst.base_client.config['region']}.oraclecloud.com{par.access_uri}"
        
        self.update_state(state='PROGRESS', meta={'step': '5/6: Exporting Image to Destination Bucket (This takes time)...'})
        exp = c_src.export_image(img.id, {"destinationType": "objectStorageUri", "destinationUri": par_url, "exportFormat": "OCI"})
        
        oci.wait_until(oci.work_requests.WorkRequestClient(c_src.base_client.config), 
                       oci.work_requests.WorkRequestClient(c_src.base_client.config).get_work_request(exp.headers["opc-work-request-id"]), 
                       'status', 'SUCCEEDED', max_wait_seconds=7200)

        self.update_state(state='PROGRESS', meta={'step': '6/6: Importing Image into Destination Region...'})
        get_client("Compute", dst_p).create_image(oci.core.models.CreateImageDetails(
            compartment_id=dst_comp, display_name=f"IMP-{img_name}",
            image_source_details=oci.core.models.ImageSourceViaObjectStorageTupleDetails(
                source_type="objectStorageTuple", namespace_name=ns_dst, bucket_name=bucket, object_name=f"{img_name}.oci")))
        
        return f"Success! {inst.display_name} migrated."
    except Exception as e:
        logger.exception("VM migration failed for vm_id=%s", vm_id)
        self.update_state(state='FAILURE', meta={'step': f"Error: {str(e)}"})
        raise self.retry(exc=e, countdown=60)

# --- TASK 2: Rclone Sync ---
@celery_app.task(name="worker.rclone_sync_task")
def rclone_sync_task(source, dest_profile, dest_bucket, mode="copy", transfers=4, checkers=8, buffer_size="16M", job_name="default"):
    dest = f"{dest_profile}_rclone:{dest_bucket}"
    log_file = f"/tmp/rclone_{job_name}.log"
    
    # Rensa gammal logg om den finns för att börja om på ny kula
    if os.path.exists(log_file):
        os.remove(log_file)

    cmd = [
        "rclone", mode, source, dest,
        "--transfers", str(transfers), 
        "--checkers", str(checkers), 
        "--buffer-size", buffer_size,
        "--log-file", log_file,          # Skriver loggen till fil för backend-läsning
        "--log-level", "INFO",
        "--stats", "2s",                 # Uppdaterar hastighet/procent varannan sekund
        "--stats-one-line",
        "--fast-list", 
        "--retries", "10", 
        "--use-mmap"
    ]
    
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

        return {"status": "timeout", "code": None, "timeout_seconds": timeout}

    return {
        "status": "success" if process.returncode == 0 else "failed",
        "code": process.returncode,
    }
