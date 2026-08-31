import os
import re
import json
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


def format_json_log_line(line: str) -> str:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(payload, dict):
        return line

    timestamp = str(payload.get("time") or "").strip()
    level = str(payload.get("level") or "").upper().strip()
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else None
    if stats:
        bytes_done = coerce_number(stats.get("bytes"), 0)
        total_bytes = coerce_number(stats.get("totalBytes"), 0)
        speed = coerce_number(stats.get("speed"), 0)
        errors = coerce_number(stats.get("errors"), 0)
        transfer_text = format_bytes(bytes_done)
        if total_bytes:
            transfer_text = f"{transfer_text} / {format_bytes(total_bytes)}"
        parts = [part for part in [timestamp, level or "INFO", f"stats {transfer_text}"] if part]
        if speed:
            parts.append(f"{format_bytes(speed)}/s")
        if errors:
            parts.append(f"errors {int(errors)}")
        return " | ".join(parts)

    message = str(payload.get("msg") or payload.get("message") or "").strip()
    object_name = str(payload.get("object") or payload.get("source") or "").strip()

    parts = []
    if timestamp:
        parts.append(timestamp)
    if level:
        parts.append(level)
    if object_name:
        parts.append(object_name)
    if message:
        parts.append(message)
    return " | ".join(parts) if parts else line


def tail_file(path: Path, max_lines: int = 500, humanize_json: bool = False) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-max_lines:]
            if humanize_json:
                lines = [format_json_log_line(line.strip()) + "\n" for line in lines if line.strip()]
            return "".join(lines).strip()
    except OSError:
        return ""


def coerce_number(value, default=0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce_int(value, default=0):
    return int(coerce_number(value, default))


def format_bytes(size_bytes) -> str:
    size = float(max(coerce_number(size_bytes, 0), 0))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size_bytes} B"


def summarize_rclone_json_log(path: Path, elapsed_seconds: float | None = None) -> dict:
    summary = {
        "bytes": 0,
        "total_bytes": 0,
        "files_transferred": 0,
        "total_transfers": 0,
        "checks": 0,
        "total_checks": 0,
        "deletes": 0,
        "errors": 0,
        "speed_bps": 0,
        "elapsed_seconds": round(max(coerce_number(elapsed_seconds, 0), 0), 1) if elapsed_seconds is not None else 0,
        "eta_seconds": None,
        "last_error": "",
        "last_warning": "",
        "last_object": "",
        "stats_seen": False,
    }

    if not path.exists():
        return summary

    completed_bytes = 0
    completed_files = 0
    deleted_objects = 0
    error_count = 0
    warning_count = 0
    last_stats = {}

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue

                level = str(payload.get("level") or "").lower()
                message = str(payload.get("msg") or payload.get("message") or "").strip()
                object_name = str(payload.get("object") or "").strip()
                if object_name:
                    summary["last_object"] = object_name

                if level == "error":
                    error_count += 1
                    if message:
                        summary["last_error"] = message
                elif level in {"warning", "warn"}:
                    warning_count += 1
                    if message:
                        summary["last_warning"] = message

                if "error" in message.lower() and message and not summary["last_error"]:
                    summary["last_error"] = message

                size = coerce_int(payload.get("size"), 0)
                message_lower = message.lower()
                if size and any(token in message_lower for token in ("copied", "transferred", "updated", "renamed")):
                    completed_files += 1
                    completed_bytes += size
                if "deleted" in message_lower:
                    deleted_objects += 1

                stats = payload.get("stats")
                if isinstance(stats, dict):
                    last_stats = stats
    except OSError:
        return summary

    if last_stats:
        summary["stats_seen"] = True
        summary["bytes"] = coerce_int(last_stats.get("bytes"), completed_bytes)
        summary["total_bytes"] = coerce_int(last_stats.get("totalBytes"), 0)
        summary["checks"] = coerce_int(last_stats.get("checks"), 0)
        summary["total_checks"] = coerce_int(last_stats.get("totalChecks"), 0)
        summary["total_transfers"] = coerce_int(last_stats.get("totalTransfers"), 0)
        summary["deletes"] = coerce_int(last_stats.get("deletes"), deleted_objects)
        summary["errors"] = max(coerce_int(last_stats.get("errors"), 0), error_count)
        summary["speed_bps"] = coerce_number(last_stats.get("speed"), 0)
        summary["elapsed_seconds"] = round(
            max(coerce_number(last_stats.get("elapsedTime"), 0), coerce_number(summary["elapsed_seconds"], 0)),
            1,
        )
        if summary["speed_bps"] <= 0 and summary["bytes"] > 0 and summary["elapsed_seconds"] > 0:
            summary["speed_bps"] = summary["bytes"] / summary["elapsed_seconds"]
        eta = last_stats.get("eta")
        summary["eta_seconds"] = None if eta is None else coerce_number(eta, 0)
        if not summary["last_error"]:
            summary["last_error"] = str(last_stats.get("lastError") or "").strip()
    else:
        summary["bytes"] = completed_bytes
        summary["deletes"] = deleted_objects
        summary["errors"] = error_count

    summary["files_transferred"] = completed_files or coerce_int(last_stats.get("transfers") if last_stats else 0, 0)
    summary["warnings"] = warning_count
    return summary
