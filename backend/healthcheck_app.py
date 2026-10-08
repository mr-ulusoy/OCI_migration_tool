"""Mount RichardORCL's Health Check UI in the authenticated console.

The original browser application, JSON format and storage helpers are retained.
This adapter replaces its HTTP handler with the console's authenticated routes.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from threading import Lock
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response


ROOT = Path(__file__).resolve().parents[1] / "vendor" / "oci-healthcheck"
DATA_DIR = Path(os.getenv("OCI_MIGRATOR_HEALTHCHECK_DIR", "~/.oci/healthcheck")).expanduser()
spec = importlib.util.spec_from_file_location("bundled_healthcheck", ROOT / "server.py")
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)
STORE_LOCK = Lock()
router = APIRouter(prefix="/oci-healthcheck")
MAX_BODY_BYTES = 2 * 1024 * 1024


def prepare_store():
    """Update pristine bundled checklists, preserving customer edits and new lists."""
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    definitions = DATA_DIR / "healthcheck"
    definitions.mkdir(exist_ok=True, mode=0o700)
    upstream.HEALTHCHECK_DIR = definitions
    upstream.FEEDBACK_FILE = DATA_DIR / "feedback.json"
    manifest_file = DATA_DIR / "bundled.json"
    previous = json.loads(manifest_file.read_text()) if manifest_file.exists() else {}
    current = {}
    for source in (ROOT / "healthcheck").glob("*.json"):
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        target = definitions / source.name
        if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() == previous.get(source.name):
            if not target.exists() or json.loads(target.read_bytes()) != json.loads(content):
                upstream.write_json_atomic(target, json.loads(content))
            # The writer normalizes whitespace, so record the installed bytes.
            current[source.name] = hashlib.sha256(target.read_bytes()).hexdigest()
        else:
            current[source.name] = previous.get(source.name, digest)
    if current != previous:
        upstream.write_json_atomic(manifest_file, current)


def definition_path(check_id):
    path = upstream.healthcheck_file(check_id)
    if path is None:
        raise HTTPException(404, "Invalid health check id")
    return path


async def read_json(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "Health check request is too large")
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(value, dict):
        raise HTTPException(400, "Expected a JSON object")
    return value


@router.get("/")
def index():
    return FileResponse(ROOT / "index.html", headers={"Cache-Control": "no-store"})


@router.get("/api/healthchecks")
def list_checks():
    with STORE_LOCK:
        prepare_store()
        return upstream.list_healthchecks()


@router.get("/healthcheck/{filename}")
def get_check(filename: str):
    if not filename.endswith(".json"):
        raise HTTPException(404, "Not found")
    with STORE_LOCK:
        prepare_store()
        path = definition_path(filename[:-5])
        if not path.is_file():
            raise HTTPException(404, "Unknown health check")
        return json.loads(path.read_text())


@router.post("/api/editor/verify", status_code=204)
def verify_editor():
    # Console middleware has already verified the administrator session.
    return Response(status_code=204)


@router.post("/api/checklist/{check_id}", status_code=204)
async def save_check(check_id: str, request: Request):
    data = await read_json(request)
    if not isinstance(data.get("categories"), list):
        raise HTTPException(400, "Invalid checklist JSON")
    with STORE_LOCK:
        prepare_store()
        upstream.write_json_atomic(definition_path(check_id), data)
    return Response(status_code=204)


@router.api_route("/api/feedback/{check_id}", methods=["GET", "POST", "DELETE"])
async def feedback(check_id: str, request: Request):
    data = await read_json(request) if request.method != "GET" else {}
    with STORE_LOCK:
        prepare_store()
        if not definition_path(check_id).is_file():
            raise HTTPException(404, "Unknown health check")
        stored = upstream.load_feedback()
        entries = stored.get(check_id, {})
        if request.method == "GET":
            return entries
        item_id = data.get("itemId")
        if not isinstance(item_id, str) or not item_id.strip():
            raise HTTPException(400, "Missing itemId")
        item_id = item_id.strip()
        if request.method == "POST":
            text = data.get("text")
            valid, error = upstream.validate_user_text(text)
            if not valid or not text.strip():
                raise HTTPException(400, error or "Missing feedback text")
            entries.setdefault(item_id, []).append({
                "id": uuid.uuid4().hex, "text": text.strip(),
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
        else:
            old = entries.get(item_id, [])
            remaining = [entry for entry in old if entry["id"] != data.get("id")]
            if len(old) == len(remaining):
                raise HTTPException(404, "Feedback entry not found")
            if remaining:
                entries[item_id] = remaining
            else:
                entries.pop(item_id, None)
        if entries:
            stored[check_id] = entries
        else:
            stored.pop(check_id, None)
        upstream.write_json_atomic(upstream.FEEDBACK_FILE, stored)
    return Response(status_code=204)


@router.get("/{kind}/{filename}")
def static_asset(kind: str, filename: str):
    allowed = {("css", "styles.css"), ("js", "app.js"), ("js", "console-bridge.js")}
    if (kind, filename) not in allowed:
        raise HTTPException(404, "Not found")
    return FileResponse(ROOT / kind / filename, headers={"Cache-Control": "no-store"})
