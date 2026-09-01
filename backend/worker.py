import json
import hashlib
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from celery import Celery
from celery.signals import worker_ready
import oci
from job_logs import ensure_job_log_dir, job_log_path, summarize_rclone_json_log, tail_file
from job_store import get_job_run, list_job_runs, update_job_run

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


LOCAL_DATA_ROOT = Path(os.getenv("OCI_MIGRATOR_LOCAL_DATA_ROOT", "/var/lib/oci-migrator/local")).resolve()

celery_app = Celery('tasks', broker=redis_url(), backend=redis_url())


def recover_interrupted_data_sync_runs() -> int:
    recovered = 0
    finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for run in list_job_runs(300):
        if run.get("kind") != "data_sync" or run.get("status") != "running":
            continue
        update_job_run(
            run["id"],
            status="failed",
            details="Backup interrupted because the worker restarted.",
            error="The worker restarted before this backup completed. Run the job again.",
            finished_at=finished_at,
        )
        recovered += 1
    return recovered


@worker_ready.connect
def recover_interrupted_runs_on_worker_start(**_kwargs):
    recovered = recover_interrupted_data_sync_runs()
    if recovered:
        logger.warning("Marked %s interrupted backup run(s) as failed after worker start.", recovered)

# --- HELPERS ---
def get_config(profile):
    return oci.config.from_file(os.path.expanduser("~/.oci/config"), profile)


def get_client(ctype, profile):
    cfg = get_config(profile)
    return getattr(oci.core, f"{ctype}Client")(cfg) if hasattr(oci.core, f"{ctype}Client") else getattr(oci.object_storage, f"{ctype}Client")(cfg)


def migration_retry_token(run_id, operation, resource_id=""):
    value = f"{run_id}:{operation}:{resource_id}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def migration_resource_name(prefix, instance_name, volume_name, run_id):
    raw_name = f"{prefix}-{instance_name}-{volume_name}-{run_id[:8]}"
    return raw_name[:255]


def begin_data_volume_migrations(
    run_id,
    source_block,
    destination_block,
    destination_compartment,
    instance_name,
    volume_ids,
    method,
):
    pending = []
    for volume_id in volume_ids:
        source_volume = source_block.get_volume(volume_id).data
        volume_name = getattr(source_volume, "display_name", "") or volume_id.rsplit(".", 1)[-1]
        target_name = migration_resource_name("MIGR", instance_name, volume_name, run_id)
        item = {
            "source_volume_id": volume_id,
            "source_volume_name": volume_name,
            "size_gb": getattr(source_volume, "size_in_gbs", None),
            "method": method,
            "target_volume_id": "",
            "backup_id": "",
            "status": "capturing",
        }

        if method == "clone":
            response = destination_block.create_volume(
                oci.core.models.CreateVolumeDetails(
                    compartment_id=destination_compartment,
                    display_name=target_name,
                    source_details=oci.core.models.VolumeSourceFromVolumeDetails(id=volume_id),
                ),
                opc_retry_token=migration_retry_token(run_id, "clone-volume", volume_id),
            )
            item["target_volume_id"] = response.data.id
        else:
            response = source_block.create_volume_backup(
                oci.core.models.CreateVolumeBackupDetails(
                    volume_id=volume_id,
                    display_name=migration_resource_name("MIGR-BACKUP", instance_name, volume_name, run_id),
                    type="FULL",
                ),
                opc_retry_token=migration_retry_token(run_id, "backup-volume", volume_id),
            )
            item["backup_id"] = response.data.id

        pending.append(item)
    return pending


def complete_data_volume_migrations(
    run_id,
    source_block,
    destination_block,
    destination_compartment,
    destination_availability_domain,
    instance_name,
    pending,
    progress_callback=None,
):
    completed = []
    total = len(pending)
    for index, item in enumerate(pending, start=1):
        volume_name = item["source_volume_name"]
        if progress_callback:
            progress_callback(f"Migrating data volume {index}/{total}: {volume_name}...")

        if item["method"] == "restore":
            backup_id = item["backup_id"]
            oci.wait_until(
                source_block,
                source_block.get_volume_backup(backup_id),
                "lifecycle_state",
                "AVAILABLE",
                max_wait_seconds=21600,
            )
            target_name = migration_resource_name("MIGR", instance_name, volume_name, run_id)
            response = destination_block.create_volume(
                oci.core.models.CreateVolumeDetails(
                    availability_domain=destination_availability_domain,
                    compartment_id=destination_compartment,
                    display_name=target_name,
                    source_details=oci.core.models.VolumeSourceFromVolumeBackupDetails(id=backup_id),
                ),
                opc_retry_token=migration_retry_token(run_id, "restore-volume", item["source_volume_id"]),
            )
            item["target_volume_id"] = response.data.id

        target_volume_id = item["target_volume_id"]
        target_volume = oci.wait_until(
            destination_block,
            destination_block.get_volume(target_volume_id),
            "lifecycle_state",
            "AVAILABLE",
            max_wait_seconds=21600,
        ).data
        item["status"] = "available"
        item["target_availability_domain"] = getattr(target_volume, "availability_domain", "")
        completed.append(item)
    return completed

# --- TASK 1: VM Migration ---
@celery_app.task(bind=True, max_retries=3)
def migrate_single_vm(
    self,
    src_p,
    dst_p,
    vm_id,
    dst_comp,
    bucket,
    data_volume_ids=None,
    data_volume_method="clone",
    destination_availability_domain="",
):
    run_id = self.request.id
    data_volume_ids = list(dict.fromkeys(data_volume_ids or []))
    source_was_running = False
    source_restarted = False
    c_src = None

    def set_progress(step: str) -> None:
        self.update_state(state='PROGRESS', meta={'step': step})
        update_job_run(run_id, status="running", details=step)

    try:
        set_progress('Connecting to OCI and fetching the source VM...')
        c_src = get_client("Compute", src_p)
        inst = c_src.get_instance(vm_id).data
        existing_run = get_job_run(run_id) or {}
        source_initial_state = existing_run.get("source_initial_state") or inst.lifecycle_state
        source_was_running = source_initial_state == "RUNNING"
        if inst.lifecycle_state not in {"RUNNING", "STOPPED"}:
            raise ValueError(
                f"VM {inst.display_name} must be RUNNING or STOPPED before migration; "
                f"current state is {inst.lifecycle_state}."
            )
        update_job_run(
            run_id,
            job_name=f"VM migration {inst.display_name}",
            vm_name=inst.display_name,
            source_initial_state=source_initial_state,
        )

        if inst.lifecycle_state == "RUNNING":
            set_progress(f'Soft-stopping source VM {inst.display_name}...')
            c_src.instance_action(vm_id, "SOFTSTOP")
            oci.wait_until(c_src, c_src.get_instance(vm_id), 'lifecycle_state', 'STOPPED', max_wait_seconds=600)

        set_progress('Creating a custom image from the boot volume...')
        img_name = migration_resource_name("MIGR", inst.display_name, "BOOT", run_id)
        img = c_src.create_image(
            oci.core.models.CreateImageDetails(
                compartment_id=inst.compartment_id,
                instance_id=vm_id,
                display_name=img_name,
            ),
            opc_retry_token=migration_retry_token(run_id, "create-boot-image", vm_id),
        ).data
        update_job_run(run_id, source_image_id=img.id)
        oci.wait_until(c_src, c_src.get_image(img.id), 'lifecycle_state', 'AVAILABLE', max_wait_seconds=3600)

        pending_data_volumes = []
        if data_volume_ids:
            set_progress(f'Capturing {len(data_volume_ids)} attached data volume(s)...')
            pending_data_volumes = begin_data_volume_migrations(
                run_id,
                get_client("Blockstorage", src_p),
                get_client("Blockstorage", dst_p),
                dst_comp,
                inst.display_name,
                data_volume_ids,
                data_volume_method,
            )
            update_job_run(run_id, data_volume_results=pending_data_volumes)

        if source_was_running:
            set_progress('Restarting the source VM after volume capture...')
            c_src.instance_action(vm_id, "START")
            oci.wait_until(c_src, c_src.get_instance(vm_id), 'lifecycle_state', 'RUNNING', max_wait_seconds=600)
            source_restarted = True

        completed_data_volumes = []
        if pending_data_volumes:
            source_block = get_client("Blockstorage", src_p)
            destination_block = get_client("Blockstorage", dst_p)
            completed_data_volumes = complete_data_volume_migrations(
                run_id,
                source_block,
                destination_block,
                dst_comp,
                destination_availability_domain,
                inst.display_name,
                pending_data_volumes,
                set_progress,
            )
            update_job_run(run_id, data_volume_results=completed_data_volumes)

        set_progress('Building the cross-tenant Object Storage bridge...')
        # Cross-tenant Export via PAR
        os_dst = get_client("ObjectStorage", dst_p)
        ns_dst = os_dst.get_namespace().data
        par = os_dst.create_preauthenticated_request(ns_dst, bucket, oci.object_storage.models.CreatePreauthenticatedRequestDetails(
            name="MigrWrite", access_type="ObjectReadWrite", object_name=f"{img_name}.oci", 
            time_expires=datetime.utcnow() + timedelta(hours=48))).data
        
        par_url = f"https://objectstorage.{os_dst.base_client.config['region']}.oraclecloud.com{par.access_uri}"
        
        set_progress('Exporting the boot image to the destination bucket...')
        exp = c_src.export_image(img.id, {"destinationType": "objectStorageUri", "destinationUri": par_url, "exportFormat": "OCI"})
        
        oci.wait_until(oci.work_requests.WorkRequestClient(c_src.base_client.config), 
                       oci.work_requests.WorkRequestClient(c_src.base_client.config).get_work_request(exp.headers["opc-work-request-id"]), 
                       'status', 'SUCCEEDED', max_wait_seconds=7200)

        set_progress('Importing the boot image into the destination tenancy...')
        c_dst = get_client("Compute", dst_p)
        imported_image = c_dst.create_image(
            oci.core.models.CreateImageDetails(
                compartment_id=dst_comp,
                display_name=f"IMP-{img_name}"[:255],
                image_source_details=oci.core.models.ImageSourceViaObjectStorageTupleDetails(
                    source_type="objectStorageTuple",
                    namespace_name=ns_dst,
                    bucket_name=bucket,
                    object_name=f"{img_name}.oci",
                ),
            ),
            opc_retry_token=migration_retry_token(run_id, "import-boot-image", vm_id),
        ).data
        update_job_run(run_id, target_image_id=imported_image.id)
        oci.wait_until(
            c_dst,
            c_dst.get_image(imported_image.id),
            "lifecycle_state",
            "AVAILABLE",
            max_wait_seconds=7200,
        )

        volume_summary = (
            f" {len(completed_data_volumes)} data volume(s) were created and are ready to attach."
            if completed_data_volumes
            else ""
        )
        details = f"Success! {inst.display_name} boot image migrated.{volume_summary}"
        update_job_run(
            run_id,
            status="success",
            details=details,
            data_volume_results=completed_data_volumes,
            finished_at=datetime.utcnow().isoformat() + "Z",
        )
        return details
    except Exception as e:
        logger.exception("VM migration failed for vm_id=%s", vm_id)
        if source_was_running and not source_restarted and c_src is not None:
            try:
                current_state = c_src.get_instance(vm_id).data.lifecycle_state
                if current_state == "STOPPED":
                    c_src.instance_action(vm_id, "START")
                    oci.wait_until(
                        c_src,
                        c_src.get_instance(vm_id),
                        "lifecycle_state",
                        "RUNNING",
                        max_wait_seconds=600,
                    )
                    source_restarted = True
            except Exception as restart_error:
                logger.exception("Failed to restart source VM after migration error for vm_id=%s", vm_id)
                update_job_run(run_id, source_restart_error=str(restart_error))
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


def format_bytes(size_bytes: int) -> str:
    size = float(max(size_bytes, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size_bytes} B"


def normalize_local_retention(local_retention=None):
    if not isinstance(local_retention, dict):
        return {"enabled": False, "delete_after_days": 30, "min_file_age_hours": 24}

    def safe_int(value, default, minimum, maximum):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    return {
        "enabled": bool(local_retention.get("enabled")),
        "delete_after_days": safe_int(local_retention.get("delete_after_days"), 30, 1, 3650),
        "min_file_age_hours": safe_int(local_retention.get("min_file_age_hours"), 24, 1, 720),
    }


def normalize_rclone_bwlimit(value=None):
    bwlimit = str(value or "").strip()
    if not bwlimit or bwlimit.lower() == "off":
        return bwlimit.lower()
    if any(char in bwlimit for char in "\r\n\0") or bwlimit.startswith("-"):
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)?[KkMmGgTtPp]?", bwlimit):
        return bwlimit
    return ""


def normalize_rclone_tpslimit(value=None):
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed > 10000:
        return None
    return parsed


def local_cleanup_source_path(source: str) -> Path | None:
    source_value = str(source or "")
    separator_index = source_value.find(":")
    if separator_index < 0:
        return None

    local_target = source_value[separator_index + 1 :]
    if not local_target.startswith("/"):
        return None

    source_path = Path(local_target).expanduser().resolve()
    try:
        source_path.relative_to(LOCAL_DATA_ROOT)
    except ValueError:
        return None

    return source_path if source_path.is_dir() else None


def run_local_retention_cleanup(source: str, local_retention=None, log_file: Path | None = None) -> dict:
    policy = normalize_local_retention(local_retention)
    if not policy["enabled"]:
        return {"enabled": False, "status": "skipped"}

    source_path = local_cleanup_source_path(source)
    if not source_path:
        return {
            "enabled": True,
            "status": "skipped",
            "reason": "Source is not a managed server local folder.",
        }

    now = time.time()
    cutoff_days = now - (policy["delete_after_days"] * 24 * 3600)
    cutoff_recent = now - (policy["min_file_age_hours"] * 3600)
    cutoff = min(cutoff_days, cutoff_recent)
    deleted_files = 0
    deleted_bytes = 0
    skipped_recent = 0
    errors = []

    def append_log(line: str) -> None:
        if not log_file:
            return
        try:
            with open(log_file, "a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
        except OSError:
            pass

    append_log("")
    append_log(
        "Local cleanup: enabled "
        f"(delete after {policy['delete_after_days']} days, "
        f"ignore modified in last {policy['min_file_age_hours']} hours)."
    )

    for root, _dirs, files in os.walk(source_path, topdown=False):
        root_path = Path(root)
        for file_name in files:
            file_path = root_path / file_name
            try:
                stat_result = file_path.stat()
                if stat_result.st_mtime > cutoff:
                    skipped_recent += 1
                    continue
                file_size = stat_result.st_size
                file_path.unlink()
                deleted_files += 1
                deleted_bytes += file_size
            except OSError as exc:
                if len(errors) < 5:
                    errors.append(f"{file_path}: {exc}")

        if root_path == source_path:
            continue
        try:
            root_path.rmdir()
        except OSError:
            pass

    result = {
        "enabled": True,
        "status": "success" if not errors else "warning",
        "source_path": str(source_path),
        "delete_after_days": policy["delete_after_days"],
        "min_file_age_hours": policy["min_file_age_hours"],
        "deleted_files": deleted_files,
        "deleted_bytes": deleted_bytes,
        "deleted_size": format_bytes(deleted_bytes),
        "skipped_recent": skipped_recent,
        "errors": errors,
    }

    append_log(
        "Local cleanup: "
        f"deleted {deleted_files} files ({format_bytes(deleted_bytes)}), "
        f"skipped {skipped_recent} recent files."
    )
    if errors:
        append_log("Local cleanup warnings:")
        for error in errors:
            append_log(f"  {error}")

    return result


@celery_app.task(bind=True, name="worker.rclone_sync_task")
def rclone_sync_task(self, source, dest_profile, dest_bucket, mode="copy", transfers=4, checkers=8, buffer_size="16M", job_name="default", run_id=None, trigger="manual", metadata_tags=None, local_retention=None, bwlimit="", tpslimit=None):
    run_id = run_id or self.request.id
    dest = f"{dest_profile}_rclone:{dest_bucket}"
    metadata_tags = normalize_metadata_tags(metadata_tags)
    local_retention = normalize_local_retention(local_retention)
    bwlimit = normalize_rclone_bwlimit(bwlimit)
    tpslimit = normalize_rclone_tpslimit(tpslimit)
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
        local_retention=local_retention,
        details="Job is running.",
        log_file=str(log_file),
        started_at=datetime.utcnow().isoformat() + "Z",
    )
    
    # Rensa gammal logg om den finns för att börja om på ny kula
    if log_file.exists():
        log_file.unlink()
    log_file.touch(mode=0o640)
    os.chmod(log_file, 0o640)

    destination_bucket = str(dest_bucket or "").strip().split("/", 1)[0]
    preflight_cmd = [
        "rclone",
        "lsd",
        f"{dest_profile}_rclone:{destination_bucket}",
        "--max-depth",
        "1",
        "--use-json-log",
        "--log-level",
        "ERROR",
        "--log-file",
        str(log_file),
    ]
    try:
        preflight = subprocess.run(
            preflight_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        update_job_run(
            run_id,
            status="failed",
            details="Destination bucket validation timed out.",
            error="OCI Object Storage did not respond within 60 seconds.",
            finished_at=datetime.utcnow().isoformat() + "Z",
        )
        return {"status": "failed", "code": None}

    if preflight.returncode != 0:
        log_tail = tail_file(log_file, max_lines=12, humanize_json=True)
        update_job_run(
            run_id,
            status="failed",
            details="Destination bucket validation failed.",
            error=log_tail or "The destination bucket does not exist or is not accessible.",
            finished_at=datetime.utcnow().isoformat() + "Z",
        )
        return {"status": "failed", "code": preflight.returncode}

    cmd = [
        "rclone", mode, source, dest,
        "--transfers", str(transfers), 
        "--checkers", str(checkers), 
        "--buffer-size", buffer_size,
        "--log-file", str(log_file),     # Skriver loggen till fil för backend-läsning
        "--log-level", "INFO",
        "--use-json-log",
        "--stats", "2s",                 # Uppdaterar hastighet/procent varannan sekund
        "--stats-one-line",
        "--fast-list", 
        "--retries", "10", 
        "--use-mmap"
    ]
    if bwlimit:
        cmd.extend(["--bwlimit", bwlimit])
    if tpslimit:
        cmd.extend(["--tpslimit", f"{tpslimit:g}"])
    if metadata_tags:
        cmd.append("--metadata")
        for tag in metadata_tags:
            cmd.extend(["--metadata-set", f"{tag['key']}={tag['value']}"])
    
    timeout = rclone_timeout_seconds()
    started_monotonic = time.monotonic()

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

        rclone_summary = summarize_rclone_json_log(
            log_file,
            elapsed_seconds=time.monotonic() - started_monotonic,
        )
        update_job_run(
            run_id,
            status="timeout",
            details=f"Timed out after {timeout} seconds.",
            error=f"Timeout after {timeout} seconds.",
            rclone_summary=rclone_summary,
            finished_at=datetime.utcnow().isoformat() + "Z",
        )
        return {"status": "timeout", "code": None, "timeout_seconds": timeout}

    status = "success" if process.returncode == 0 else "failed"
    rclone_summary = summarize_rclone_json_log(
        log_file,
        elapsed_seconds=time.monotonic() - started_monotonic,
    )
    log_tail = tail_file(log_file, max_lines=12, humanize_json=True)
    local_cleanup_result = {"enabled": local_retention.get("enabled", False), "status": "skipped"}
    if process.returncode == 0:
        try:
            local_cleanup_result = run_local_retention_cleanup(source, local_retention, log_file)
        except Exception as exc:
            logger.exception("Local cleanup failed after successful backup (job=%s)", job_name)
            local_cleanup_result = {
                "enabled": local_retention.get("enabled", False),
                "status": "warning",
                "reason": str(exc),
            }
        if local_cleanup_result.get("status") == "warning":
            status = "warning"

    if process.returncode == 0:
        if local_cleanup_result.get("enabled") and local_cleanup_result.get("status") == "success":
            details = (
                "Backup succeeded. Local cleanup deleted "
                f"{local_cleanup_result.get('deleted_files', 0)} files "
                f"({local_cleanup_result.get('deleted_size', '0 B')})."
            )
        elif local_cleanup_result.get("status") == "warning":
            details = "Backup succeeded. Local cleanup finished with warnings."
        else:
            details = "Backup succeeded."
    else:
        details = f"Failed with exit code {process.returncode}."

    update_job_run(
        run_id,
        status=status,
        details=details,
        error="" if process.returncode == 0 else (log_tail or details),
        rclone_summary=rclone_summary,
        local_cleanup=local_cleanup_result,
        finished_at=datetime.utcnow().isoformat() + "Z",
    )

    return {
        "status": status,
        "code": process.returncode,
    }
