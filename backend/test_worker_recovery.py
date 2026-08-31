import unittest
from unittest.mock import patch

import worker


class WorkerRecoveryTests(unittest.TestCase):
    @patch.object(worker, "update_job_run")
    @patch.object(worker, "list_job_runs")
    def test_marks_only_running_data_sync_jobs_as_interrupted(self, list_runs, update_run):
        list_runs.return_value = [
            {"id": "running-backup", "kind": "data_sync", "status": "running"},
            {"id": "queued-backup", "kind": "data_sync", "status": "queued"},
            {"id": "running-vm", "kind": "vm_migration", "status": "running"},
            {"id": "finished-backup", "kind": "data_sync", "status": "success"},
        ]

        recovered = worker.recover_interrupted_data_sync_runs()

        self.assertEqual(recovered, 1)
        update_run.assert_called_once()
        args, kwargs = update_run.call_args
        self.assertEqual(args, ("running-backup",))
        self.assertEqual(kwargs["status"], "failed")
        self.assertIn("worker restarted", kwargs["details"])
        self.assertTrue(kwargs["finished_at"].endswith("Z"))


if __name__ == "__main__":
    unittest.main()
