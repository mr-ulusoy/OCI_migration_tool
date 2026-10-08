import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
import healthcheck_app as healthcheck


class HealthCheckIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / "data"
        for target, value in [
            ("DATA_DIR", self.data_dir),
        ]:
            patcher = patch.object(healthcheck, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        runtime = patch.object(main, "read_runtime_env", return_value={})
        runtime.start()
        self.addCleanup(runtime.stop)
        auth = patch.object(main, "request_is_authenticated", side_effect=lambda request: request.headers.get("Authorization") == "Bearer test-session")
        auth.start()
        self.addCleanup(auth.stop)
        self.client = TestClient(main.app)
        self.headers = {"Authorization": "Bearer test-session"}

    def test_only_static_shell_is_public(self):
        for path in ["", "css/styles.css", "js/app.js", "js/console-bridge.js"]:
            self.assertEqual(self.client.get("/oci-healthcheck/" + path).status_code, 200)
        for method, path in [
            ("GET", "api/healthchecks"), ("GET", "healthcheck/OCI-storage.json"),
            ("POST", "api/editor/verify"), ("POST", "api/checklist/new"),
            ("GET", "api/feedback/OCI-storage"), ("POST", "api/feedback/OCI-storage"),
            ("DELETE", "api/feedback/OCI-storage"),
        ]:
            self.assertEqual(self.client.request(method, "/oci-healthcheck/" + path).status_code, 401)

    def test_editor_and_definitions(self):
        base = "/oci-healthcheck/"
        checks = self.client.get(base + "api/healthchecks", headers=self.headers).json()
        self.assertGreaterEqual(len(checks), 2)
        self.assertEqual(self.client.post(base + "api/editor/verify", headers=self.headers).status_code, 204)
        check = self.client.get(base + checks[0]["file"], headers=self.headers).json()
        self.assertIsInstance(check["categories"], list)
        custom = {"title": "Customer checklist", "categories": []}
        self.assertEqual(self.client.post(base + "api/checklist/custom", headers=self.headers, json=custom).status_code, 204)
        self.assertEqual(self.client.get(base + "healthcheck/custom.json", headers=self.headers).json(), custom)
        self.assertEqual(self.client.post(base + "api/checklist/custom", headers=self.headers, json={}).status_code, 400)
        for path in ["js/server.py", "healthcheck/..json"]:
            self.assertEqual(self.client.get(base + path, headers=self.headers).status_code, 404)

    def test_preserves_customizations_and_updates_pristine_definitions(self):
        source = Path(self.temp.name) / "upstream"
        (source / "healthcheck").mkdir(parents=True)
        bundled = source / "healthcheck" / "example.json"
        bundled.write_text(json.dumps({"title": "v1", "categories": []}))
        with patch.object(healthcheck, "ROOT", source):
            healthcheck.prepare_store()
            target = self.data_dir / "healthcheck" / "example.json"
            bundled.write_text(json.dumps({"title": "v2", "categories": []}))
            healthcheck.prepare_store()
            self.assertEqual(json.loads(target.read_text())["title"], "v2")
            target.write_text(json.dumps({"title": "Customer edit", "categories": []}))
            bundled.write_text(json.dumps({"title": "v3", "categories": []}))
            healthcheck.prepare_store()
            self.assertEqual(json.loads(target.read_text())["title"], "Customer edit")

    def test_feedback_roundtrip(self):
        path = "/oci-healthcheck/api/feedback/OCI-storage"
        response = self.client.post(path, headers=self.headers, json={"itemId": "item-1", "text": "Review required"})
        self.assertEqual(response.status_code, 204)
        entry = self.client.get(path, headers=self.headers).json()["item-1"][0]
        self.assertEqual(entry["text"], "Review required")
        self.assertEqual(self.client.request("DELETE", path, headers=self.headers, json={"itemId": "item-1", "id": entry["id"]}).status_code, 204)
        self.assertEqual(self.client.get(path, headers=self.headers).json(), {})

    def test_payload_limit(self):
        response = self.client.post("/oci-healthcheck/api/checklist/custom", headers=self.headers, content=b"x" * (healthcheck.MAX_BODY_BYTES + 1))
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
