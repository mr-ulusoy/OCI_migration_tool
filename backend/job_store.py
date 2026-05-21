import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import fcntl

OCI_DIR = os.path.expanduser(os.getenv("OCI_MIGRATOR_STATE_DIR", "~/.oci"))
JOB_HISTORY_FILE = os.path.expanduser(
    os.getenv("OCI_MIGRATOR_JOB_HISTORY_FILE", os.path.join(OCI_DIR, "job_history.json"))
)
JOB_HISTORY_LOCK_FILE = f"{JOB_HISTORY_FILE}.lock"

try:
    MAX_JOB_HISTORY_RUNS = int(os.getenv("OCI_MIGRATOR_MAX_JOB_HISTORY_RUNS", "300"))
except ValueError:
    MAX_JOB_HISTORY_RUNS = 300


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def locked_history_file():
    os.makedirs(os.path.dirname(JOB_HISTORY_FILE), exist_ok=True)
    lock_dir = os.path.dirname(JOB_HISTORY_LOCK_FILE)
    os.makedirs(lock_dir, exist_ok=True)
    with open(JOB_HISTORY_LOCK_FILE, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _empty_history() -> dict[str, Any]:
    return {"version": 1, "runs": []}


def read_job_history() -> dict[str, Any]:
    if not os.path.exists(JOB_HISTORY_FILE):
        return _empty_history()

    try:
        with open(JOB_HISTORY_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return _empty_history()

    if not isinstance(data, dict):
        return _empty_history()

    runs = data.get("runs", [])
    if not isinstance(runs, list):
        runs = []

    return {"version": data.get("version", 1), "runs": runs}


def write_job_history(history: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(JOB_HISTORY_FILE), exist_ok=True)
    file_descriptor, temp_path = tempfile.mkstemp(dir=os.path.dirname(JOB_HISTORY_FILE))
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temp_file:
            json.dump(history, temp_file, indent=2)
            temp_file.write("\n")
        os.replace(temp_path, JOB_HISTORY_FILE)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _sort_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        runs,
        key=lambda run: run.get("updated_at") or run.get("created_at") or "",
        reverse=True,
    )


def list_job_runs(limit: int = 100) -> list[dict[str, Any]]:
    with locked_history_file():
        history = read_job_history()
        return _sort_runs(history["runs"])[:limit]


def get_job_run(run_id: str) -> dict[str, Any] | None:
    with locked_history_file():
        history = read_job_history()
        return next((run for run in history["runs"] if run.get("id") == run_id), None)


def upsert_job_run(run: dict[str, Any]) -> dict[str, Any]:
    now = utc_now_iso()
    run_id = str(run.get("id") or "").strip()
    if not run_id:
        raise ValueError("Job run id is required")

    with locked_history_file():
        history = read_job_history()
        runs = [existing for existing in history["runs"] if isinstance(existing, dict)]
        existing = next((item for item in runs if item.get("id") == run_id), None)

        if existing:
            merged = {**existing, **run, "updated_at": now}
            if not merged.get("created_at"):
                merged["created_at"] = existing.get("created_at") or now
            runs = [merged if item.get("id") == run_id else item for item in runs]
        else:
            merged = {**run, "created_at": run.get("created_at") or now, "updated_at": now}
            runs.append(merged)

        history["runs"] = _sort_runs(runs)[:MAX_JOB_HISTORY_RUNS]
        write_job_history(history)
        return merged


def update_job_run(run_id: str, **updates: Any) -> dict[str, Any]:
    updates["id"] = run_id
    return upsert_job_run(updates)
