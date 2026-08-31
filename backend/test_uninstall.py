import asyncio
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import main


class UninstallTests(unittest.TestCase):
    def request(self, **values):
        return main.UninstallRequest(
            current_password=values.get("current_password", "correct-password"),
            confirmation=values.get("confirmation", "UNINSTALL"),
            purge_local_backups=values.get("purge_local_backups", False),
        )

    def test_requires_exact_confirmation(self):
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(main.schedule_uninstall(self.request(confirmation="uninstall")))

        self.assertEqual(raised.exception.status_code, 400)

    def test_requires_current_admin_password(self):
        with patch.object(main, "get_runtime_config", return_value={"admin_password_hash": "hash"}), patch.object(
            main, "verify_password", return_value=False
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(main.schedule_uninstall(self.request()))

        self.assertEqual(raised.exception.status_code, 403)

    def test_schedules_project_uninstall_without_local_data_purge(self):
        completed = subprocess.CompletedProcess(["helper"], 0, stdout='{"status":"scheduled"}\n', stderr="")
        with patch.object(main, "get_runtime_config", return_value={"admin_password_hash": "hash"}), patch.object(
            main, "verify_password", return_value=True
        ), patch.object(main, "uninstall_helper_command", return_value=["helper", "schedule"]) as command, patch.object(
            main.subprocess, "run", return_value=completed
        ) as run:
            result = asyncio.run(main.schedule_uninstall(self.request()))

        command.assert_called_once_with(False)
        run.assert_called_once()
        self.assertEqual(result["status"], "scheduled")
        self.assertFalse(result["purge_local_backups"])

    def test_local_data_purge_is_only_a_helper_flag(self):
        completed = subprocess.CompletedProcess(["helper"], 0, stdout='{"status":"scheduled"}\n', stderr="")
        with patch.object(main, "get_runtime_config", return_value={"admin_password_hash": "hash"}), patch.object(
            main, "verify_password", return_value=True
        ), patch.object(
            main, "uninstall_helper_command", return_value=["helper", "schedule", "--purge-local-data"]
        ) as command, patch.object(main.subprocess, "run", return_value=completed):
            asyncio.run(main.schedule_uninstall(self.request(purge_local_backups=True)))

        command.assert_called_once_with(True)

    def test_helper_command_has_no_user_controlled_path(self):
        helper = Path("/usr/local/sbin/oci-migrator-uninstall")
        with patch.object(main, "UNINSTALL_HELPER", helper), patch.object(Path, "is_file", return_value=True), patch.object(
            main.os, "geteuid", return_value=0
        ):
            self.assertEqual(
                main.uninstall_helper_command(True),
                [str(helper), "schedule", "--purge-local-data"],
            )


if __name__ == "__main__":
    unittest.main()
