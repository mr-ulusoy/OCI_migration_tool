import fcntl
import json
import os
import re
import socket
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ENV_FILE_PATH = Path(
    os.path.expanduser(os.getenv("OCI_MIGRATOR_ENV_FILE", "~/.oci-migrator.env"))
)
DEFAULT_STATE_FILE = Path(
    os.path.expanduser(
        os.getenv(
            "OCI_MIGRATOR_NOTIFICATION_STATE_FILE",
            "~/.oci/notification_status.json",
        )
    )
)
FACILITY_CODES = {
    "user": 1,
    "daemon": 3,
    "local0": 16,
    "local1": 17,
    "local2": 18,
    "local3": 19,
    "local4": 20,
    "local5": 21,
    "local6": 22,
    "local7": 23,
}
PROTOCOLS = {"udp", "tcp"}
EVENT_MODES = {"failures_recovery", "failures_only", "all_runs"}
FINAL_BACKUP_STATUSES = {"success", "warning", "failed", "timeout"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with ENV_FILE_PATH.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return values


def _runtime_value(values: dict[str, str], key: str, default: str = "") -> str:
    return values[key] if key in values else os.getenv(key, default)


def get_notification_settings() -> dict[str, Any]:
    values = _read_env_file()
    enabled = _runtime_value(values, "OCI_MIGRATOR_SYSLOG_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    try:
        port = int(_runtime_value(values, "OCI_MIGRATOR_SYSLOG_PORT", "514"))
    except ValueError:
        port = 514

    protocol = _runtime_value(values, "OCI_MIGRATOR_SYSLOG_PROTOCOL", "udp").lower()
    facility = _runtime_value(values, "OCI_MIGRATOR_SYSLOG_FACILITY", "local0").lower()
    events = _runtime_value(
        values,
        "OCI_MIGRATOR_SYSLOG_EVENTS",
        "failures_recovery",
    ).lower()
    settings = {
        "enabled": enabled,
        "host": _runtime_value(values, "OCI_MIGRATOR_SYSLOG_HOST", "").strip(),
        "port": port if 1 <= port <= 65535 else 514,
        "protocol": protocol if protocol in PROTOCOLS else "udp",
        "facility": facility if facility in FACILITY_CODES else "local0",
        "events": events if events in EVENT_MODES else "failures_recovery",
    }
    settings.update(read_notification_status())
    return settings


def validate_notification_settings(settings: dict[str, Any], require_host: bool = False) -> dict[str, Any]:
    host = str(settings.get("host") or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if (bool(settings.get("enabled")) or require_host) and not host:
        raise ValueError("Syslog server is required.")
    if host and (
        len(host) > 253
        or any(character in host for character in "\r\n\0 /\\")
        or not re.fullmatch(r"[A-Za-z0-9_.:-]+", host)
    ):
        raise ValueError("Syslog server must be a valid hostname or IP address.")

    try:
        port = int(settings.get("port", 514))
    except (TypeError, ValueError) as exc:
        raise ValueError("Syslog port must be a number.") from exc
    if port < 1 or port > 65535:
        raise ValueError("Syslog port must be between 1 and 65535.")

    protocol = str(settings.get("protocol") or "udp").strip().lower()
    facility = str(settings.get("facility") or "local0").strip().lower()
    events = str(settings.get("events") or "failures_recovery").strip().lower()
    if protocol not in PROTOCOLS:
        raise ValueError("Syslog protocol must be UDP or TCP.")
    if facility not in FACILITY_CODES:
        raise ValueError("Unsupported syslog facility.")
    if events not in EVENT_MODES:
        raise ValueError("Unsupported notification event selection.")

    return {
        "enabled": bool(settings.get("enabled")),
        "host": host,
        "port": port,
        "protocol": protocol,
        "facility": facility,
        "events": events,
    }


def _state_file() -> Path:
    values = _read_env_file()
    configured = _runtime_value(
        values,
        "OCI_MIGRATOR_NOTIFICATION_STATE_FILE",
        str(DEFAULT_STATE_FILE),
    )
    return Path(os.path.expanduser(configured)).resolve()


@contextmanager
def _locked_state():
    state_file = _state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = Path(f"{state_file}.lock")
    with lock_file.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield state_file
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_status_file(state_file: Path) -> dict[str, str]:
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def read_notification_status() -> dict[str, str]:
    with _locked_state() as state_file:
        state = _read_status_file(state_file)
    return {
        "last_sent_at": str(state.get("last_sent_at") or ""),
        "last_event": str(state.get("last_event") or ""),
        "last_error_at": str(state.get("last_error_at") or ""),
        "last_error": str(state.get("last_error") or ""),
    }


def _record_delivery(event: str, error: str = "") -> None:
    with _locked_state() as state_file:
        state = _read_status_file(state_file)
        if error:
            state["last_error_at"] = _utc_now()
            state["last_error"] = error[:500]
        else:
            state["last_sent_at"] = _utc_now()
            state["last_event"] = event

        descriptor, temporary_path = tempfile.mkstemp(dir=state_file.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2)
                handle.write("\n")
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, state_file)
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)


def _quote(value: Any, maximum: int = 500) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))[:maximum]
    return f'"{text.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'


def _syslog_payload(settings: dict[str, Any], event: str, severity: int, fields: dict[str, Any]) -> bytes:
    priority = FACILITY_CODES[settings["facility"]] * 8 + severity
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    hostname = re.sub(r"[^A-Za-z0-9_.-]", "-", socket.gethostname()) or "localhost"
    message_parts = [f"event={event}"]
    for key, value in fields.items():
        if value in (None, ""):
            continue
        safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(key))
        if isinstance(value, (int, float)):
            message_parts.append(f"{safe_key}={value}")
        else:
            message_parts.append(f"{safe_key}={_quote(value)}")
    message = " ".join(message_parts)
    return f"<{priority}>1 {timestamp} {hostname} oci-migrator {os.getpid()} {event} - {message}".encode(
        "utf-8",
        errors="replace",
    )[:4096]


def send_syslog_event(
    event: str,
    fields: dict[str, Any],
    *,
    severity: int = 5,
    settings: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    try:
        normalized = validate_notification_settings(
            settings or get_notification_settings(),
            require_host=force,
        )
    except ValueError as exc:
        error = str(exc)
        _record_delivery(event, error)
        return {"ok": False, "error": error}

    if not normalized["enabled"] and not force:
        return {"ok": False, "skipped": True, "reason": "Notifications are disabled."}

    payload = _syslog_payload(normalized, event, severity, fields)
    socket_type = socket.SOCK_DGRAM if normalized["protocol"] == "udp" else socket.SOCK_STREAM
    try:
        addresses = socket.getaddrinfo(
            normalized["host"],
            normalized["port"],
            type=socket_type,
        )
        if not addresses:
            raise OSError("Syslog server could not be resolved.")
        family, sock_type, protocol, _, address = addresses[0]
        with socket.socket(family, sock_type, protocol) as client:
            client.settimeout(3.0)
            if normalized["protocol"] == "udp":
                client.sendto(payload, address)
            else:
                client.connect(address)
                client.sendall(payload + b"\n")
        _record_delivery(event)
        return {"ok": True, "event": event, "sent_at": _utc_now()}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _record_delivery(event, error)
        return {"ok": False, "error": error}


def notify_backup_run(run: dict[str, Any], previous_job_status: str = "") -> dict[str, Any]:
    status = str(run.get("status") or "").lower()
    if run.get("kind") != "data_sync" or status not in FINAL_BACKUP_STATUSES:
        return {"ok": False, "skipped": True, "reason": "Not a final backup result."}

    settings = get_notification_settings()
    if not settings["enabled"]:
        return {"ok": False, "skipped": True, "reason": "Notifications are disabled."}

    mode = settings["events"]
    previous_status = str(previous_job_status or "").lower()
    if status == "success" and previous_status in {"failed", "timeout"}:
        event = "backup.recovered"
        severity = 5
    elif status in {"failed", "timeout"}:
        event = f"backup.{status}"
        severity = 3
    elif mode == "all_runs":
        event = f"backup.{status}"
        severity = 4 if status == "warning" else 6
    else:
        return {"ok": False, "skipped": True, "reason": "Event is outside the configured selection."}

    if mode == "failures_only" and event == "backup.recovered":
        return {"ok": False, "skipped": True, "reason": "Recovery notifications are disabled."}

    summary = run.get("rclone_summary") if isinstance(run.get("rclone_summary"), dict) else {}
    return send_syslog_event(
        event,
        {
            "job": run.get("job_name") or "unknown",
            "run_id": run.get("id") or "",
            "status": status,
            "trigger": run.get("trigger") or "",
            "errors": summary.get("errors", 0),
            "message": run.get("details") or status,
        },
        severity=severity,
        settings=settings,
    )


def send_test_notification(settings: dict[str, Any]) -> dict[str, Any]:
    return send_syslog_event(
        "notification.test",
        {"status": "ok", "message": "Cloud Migration Console syslog test message."},
        severity=5,
        settings=settings,
        force=True,
    )
