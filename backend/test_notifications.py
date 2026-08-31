import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import notifications
import job_store


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temporary_directory.name, "notification-status.json")
        self.state_patch = patch.object(notifications, "DEFAULT_STATE_FILE", self.state_file)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.temporary_directory.cleanup()

    def test_validates_enabled_server(self):
        with self.assertRaisesRegex(ValueError, "Syslog server is required"):
            notifications.validate_notification_settings(
                {
                    "enabled": True,
                    "host": "",
                    "port": 514,
                    "protocol": "udp",
                    "facility": "local0",
                    "events": "failures_recovery",
                }
            )

    def test_sends_structured_udp_message_and_records_status(self):
        client = MagicMock()
        client.__enter__.return_value = client
        with patch.object(
            notifications.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("127.0.0.1", 5514))],
        ), patch.object(notifications.socket, "socket", return_value=client):
            result = notifications.send_syslog_event(
                "backup.failed",
                {"job": "Nightly", "run_id": "run-1", "errors": 2},
                severity=3,
                settings={
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 5514,
                    "protocol": "udp",
                    "facility": "local0",
                    "events": "failures_recovery",
                },
            )

        self.assertTrue(result["ok"])
        payload = client.sendto.call_args.args[0].decode("utf-8")
        self.assertIn("event=backup.failed", payload)
        self.assertIn('job="Nightly"', payload)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["last_event"], "backup.failed")

    def test_tcp_message_connects_and_uses_line_framing(self):
        client = MagicMock()
        client.__enter__.return_value = client
        address = ("127.0.0.1", 6514)
        with patch.object(
            notifications.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", address)],
        ), patch.object(notifications.socket, "socket", return_value=client):
            result = notifications.send_test_notification(
                {
                    "enabled": False,
                    "host": "127.0.0.1",
                    "port": 6514,
                    "protocol": "tcp",
                    "facility": "local0",
                    "events": "failures_recovery",
                }
            )

        self.assertTrue(result["ok"])
        client.connect.assert_called_once_with(address)
        self.assertTrue(client.sendall.call_args.args[0].endswith(b"\n"))

    @patch.object(notifications, "send_syslog_event")
    @patch.object(notifications, "get_notification_settings")
    def test_success_after_failure_is_recovery(self, get_settings, send_event):
        get_settings.return_value = {
            "enabled": True,
            "host": "syslog.internal",
            "port": 514,
            "protocol": "udp",
            "facility": "local0",
            "events": "failures_recovery",
        }
        send_event.return_value = {"ok": True}

        notifications.notify_backup_run(
            {"id": "run-2", "kind": "data_sync", "job_name": "Nightly", "status": "success"},
            "failed",
        )

        self.assertEqual(send_event.call_args.args[0], "backup.recovered")


class JobStoreNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.history_patch = patch.object(job_store, "JOB_HISTORY_FILE", str(root / "history.json"))
        self.lock_patch = patch.object(job_store, "JOB_HISTORY_LOCK_FILE", str(root / "history.json.lock"))
        self.history_patch.start()
        self.lock_patch.start()

    def tearDown(self):
        self.lock_patch.stop()
        self.history_patch.stop()
        self.temporary_directory.cleanup()

    @patch.object(notifications, "notify_backup_run")
    def test_final_result_notifies_once_and_includes_previous_job_status(self, notify):
        job_store.upsert_job_run(
            {"id": "run-1", "kind": "data_sync", "job_name": "Nightly", "status": "queued"}
        )
        job_store.update_job_run("run-1", status="failed", details="Destination unavailable")
        job_store.update_job_run("run-1", status="failed", details="Destination still unavailable")
        job_store.upsert_job_run(
            {"id": "run-2", "kind": "data_sync", "job_name": "Nightly", "status": "queued"}
        )
        job_store.update_job_run("run-2", status="success", details="Backup succeeded")

        self.assertEqual(notify.call_count, 2)
        current_run, previous_status = notify.call_args.args
        self.assertEqual(current_run["id"], "run-2")
        self.assertEqual(previous_status, "failed")


if __name__ == "__main__":
    unittest.main()
