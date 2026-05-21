import os
import re
from pathlib import Path

JOB_LOG_DIR = Path(os.getenv("OCI_MIGRATOR_JOB_LOG_DIR", "/var/log/oci-migrator/jobs")).expanduser().resolve()
LEGACY_TMP_DIR = Path("/tmp").resolve()


def normalize_job_name(job_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", (job_name or "").strip()) or "default"


def normalize_run_id(run_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", (run_id or "").strip()) or "unknown"


def ensure_job_log_dir() -> None:
    JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)


def job_log_path(job_name: str, run_id: str) -> Path:
    safe_job_name = normalize_job_name(job_name)
    safe_run_id = normalize_run_id(run_id)
    return JOB_LOG_DIR / f"rclone_{safe_job_name}_{safe_run_id}.log"


def legacy_job_log_path(job_name: str) -> Path:
    return LEGACY_TMP_DIR / f"rclone_{normalize_job_name(job_name)}.log"


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_readable_log_path(raw_path: str) -> Path | None:
    if not raw_path:
        return None

    path = Path(raw_path).expanduser().resolve()
    if path.suffix != ".log" or not path.name.startswith("rclone_"):
        return None

    if path_is_under(path, JOB_LOG_DIR) or path.parent == LEGACY_TMP_DIR:
        return path

    return None


def tail_file(path: Path, max_lines: int = 500) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-max_lines:]).strip()
    except OSError:
        return ""
