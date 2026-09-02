import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

import main
import worker


def response(data):
    return SimpleNamespace(data=data)


class VmMigrationValidationTests(unittest.TestCase):
    def test_restarts_source_vms_by_default(self):
        job = main.BulkMigrationJob(
            vm_ids=["vm-1"],
            source_profile="SOURCE",
            dest_profile="TARGET",
            bucket_name="images",
        )

        self.assertTrue(job.restart_source_vms)

    def test_rejects_cross_region_data_volume_migration(self):
        job = main.BulkMigrationJob(
            vm_ids=["vm-1"],
            source_profile="SOURCE",
            dest_profile="TARGET",
            bucket_name="images",
            data_volume_ids={"vm-1": ["volume-1"]},
        )
        with patch.object(
            main.oci.config,
            "from_file",
            side_effect=[{"region": "eu-stockholm-1"}, {"region": "eu-frankfurt-1"}],
        ), self.assertRaises(HTTPException) as raised:
            main.validate_vm_migration_request(job)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("same OCI region", raised.exception.detail)

    def test_rejects_volume_that_is_no_longer_attached(self):
        job = main.BulkMigrationJob(
            vm_ids=["vm-1"],
            source_profile="SOURCE",
            dest_profile="TARGET",
            bucket_name="images",
            data_volume_ids={"vm-1": ["volume-missing"]},
        )
        compute = Mock()
        compute.get_instance.return_value = response(SimpleNamespace(compartment_id="source-compartment"))
        attachments = response([SimpleNamespace(volume_id="volume-attached", lifecycle_state="ATTACHED")])
        configs = [
            {"region": "eu-stockholm-1", "tenancy": "source-tenancy"},
            {"region": "eu-stockholm-1", "tenancy": "target-tenancy"},
        ]

        with patch.object(main.oci.config, "from_file", side_effect=configs), \
                patch.object(main.oci.core, "ComputeClient", return_value=compute), \
                patch.object(main.oci.pagination, "list_call_get_all_results", return_value=attachments), \
                patch.object(main, "validate_destination_bucket"):
            with self.assertRaises(HTTPException) as raised:
                main.validate_vm_migration_request(job)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("no longer attached", raised.exception.detail)

    def test_restore_requires_destination_availability_domain(self):
        job = main.BulkMigrationJob(
            vm_ids=["vm-1"],
            source_profile="SOURCE",
            dest_profile="TARGET",
            bucket_name="images",
            data_volume_ids={"vm-1": ["volume-1"]},
            data_volume_method="restore",
        )
        configs = [
            {"region": "eu-stockholm-1", "tenancy": "source-tenancy"},
            {"region": "eu-stockholm-1", "tenancy": "target-tenancy"},
        ]
        with patch.object(main.oci.config, "from_file", side_effect=configs), \
                self.assertRaises(HTTPException) as raised:
            main.validate_vm_migration_request(job)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("availability domain", raised.exception.detail)

    def test_accepts_attached_volume_clone(self):
        job = main.BulkMigrationJob(
            vm_ids=["vm-1"],
            source_profile="SOURCE",
            dest_profile="TARGET",
            bucket_name="images",
            data_volume_ids={"vm-1": ["volume-1"]},
        )
        compute = Mock()
        compute.get_instance.return_value = response(SimpleNamespace(compartment_id="source-compartment"))
        attachments = response([SimpleNamespace(volume_id="volume-1", lifecycle_state="ATTACHED")])
        configs = [
            {"region": "eu-stockholm-1", "tenancy": "source-tenancy"},
            {"region": "eu-stockholm-1", "tenancy": "target-tenancy"},
        ]

        with patch.object(main.oci.config, "from_file", side_effect=configs), \
                patch.object(main.oci.core, "ComputeClient", return_value=compute), \
                patch.object(main.oci.pagination, "list_call_get_all_results", return_value=attachments), \
                patch.object(main, "validate_destination_bucket") as validate_bucket:
            _source, _destination, selections = main.validate_vm_migration_request(job)

        self.assertEqual(selections, {"vm-1": ["volume-1"]})
        validate_bucket.assert_called_once_with("TARGET", "images")


class VmMigrationQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_keep_stopped_policy_is_applied_to_every_vm(self):
        job = main.BulkMigrationJob(
            vm_ids=["vm-1", "vm-2"],
            source_profile="SOURCE",
            dest_profile="TARGET",
            bucket_name="images",
            restart_source_vms=False,
        )
        queued_tasks = [SimpleNamespace(id="run-1"), SimpleNamespace(id="run-2")]

        with patch.object(
            main,
            "validate_vm_migration_request",
            return_value=({}, {"compartment": "target-compartment"}, {"vm-1": [], "vm-2": []}),
        ), patch.object(
            main.migrate_single_vm,
            "apply_async",
            side_effect=queued_tasks,
        ) as apply_async, patch.object(main, "upsert_job_run") as upsert_job_run:
            result = await main.start_bulk_migration(job)

        self.assertEqual(len(result["tasks"]), 2)
        self.assertEqual(apply_async.call_count, 2)
        for queued_call in apply_async.call_args_list:
            self.assertFalse(queued_call.kwargs["args"][-1])
        queued_runs = [call.args[0] for call in upsert_job_run.call_args_list]
        self.assertEqual([run["restart_source_vm"] for run in queued_runs], [False, False])


class VmMigrationPowerPolicyTests(unittest.TestCase):
    def test_restart_only_when_vm_started_running_and_policy_allows_it(self):
        self.assertTrue(worker.should_restart_source_vm(True, True))
        self.assertFalse(worker.should_restart_source_vm(True, False))
        self.assertFalse(worker.should_restart_source_vm(False, True))
        self.assertFalse(worker.should_restart_source_vm(True, True, source_restarted=True))

    def test_boot_image_export_par_is_write_only(self):
        details = worker.boot_image_export_par_details("MIGR-server-BOOT-run12345", "run12345678")

        self.assertEqual(details.access_type, "ObjectWrite")
        self.assertEqual(details.object_name, "MIGR-server-BOOT-run12345.oci")
        self.assertEqual(details.name, "MigrWrite-run12345")


class DataVolumeMigrationTests(unittest.TestCase):
    def test_clone_uses_source_volume_details_in_target_tenancy(self):
        source_block = Mock()
        destination_block = Mock()
        source_block.get_volume.return_value = response(
            SimpleNamespace(display_name="Application Data", size_in_gbs=512)
        )
        destination_block.create_volume.return_value = response(SimpleNamespace(id="target-volume"))

        pending = worker.begin_data_volume_migrations(
            "run-12345678",
            source_block,
            destination_block,
            "target-compartment",
            "server-1",
            ["source-volume"],
            "clone",
        )

        details = destination_block.create_volume.call_args.args[0]
        self.assertEqual(details.compartment_id, "target-compartment")
        self.assertEqual(details.source_details.id, "source-volume")
        self.assertEqual(pending[0]["target_volume_id"], "target-volume")
        self.assertTrue(pending[0]["target_volume_name"].startswith("MIGR-server-1-Application Data-"))
        source_block.create_volume_backup.assert_not_called()

    def test_restore_creates_full_backup_then_target_volume_in_selected_ad(self):
        source_block = Mock()
        destination_block = Mock()
        source_block.get_volume.return_value = response(
            SimpleNamespace(display_name="Database Data", size_in_gbs=1024)
        )
        source_block.create_volume_backup.return_value = response(SimpleNamespace(id="source-backup"))
        destination_block.create_volume.return_value = response(SimpleNamespace(id="target-volume"))
        destination_block.get_volume.return_value = response(SimpleNamespace(id="target-volume"))

        pending = worker.begin_data_volume_migrations(
            "run-12345678",
            source_block,
            destination_block,
            "target-compartment",
            "server-1",
            ["source-volume"],
            "restore",
        )
        backup_details = source_block.create_volume_backup.call_args.args[0]
        self.assertEqual(backup_details.type, "FULL")
        self.assertEqual(backup_details.volume_id, "source-volume")

        target_volume = SimpleNamespace(id="target-volume", availability_domain="target-ad-1")
        with patch.object(
            worker.oci,
            "wait_until",
            side_effect=[response(SimpleNamespace(id="source-backup")), response(target_volume)],
        ):
            completed = worker.complete_data_volume_migrations(
                "run-12345678",
                source_block,
                destination_block,
                "target-compartment",
                "target-ad-1",
                "server-1",
                pending,
            )

        details = destination_block.create_volume.call_args.args[0]
        self.assertEqual(details.availability_domain, "target-ad-1")
        self.assertEqual(details.source_details.id, "source-backup")
        self.assertEqual(completed[0]["target_volume_id"], "target-volume")
        self.assertTrue(completed[0]["target_volume_name"].startswith("MIGR-server-1-Database Data-"))
        self.assertEqual(completed[0]["status"], "available")


class VmMigrationStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_exposes_selected_and_created_data_volumes(self):
        task = SimpleNamespace(
            status="PROGRESS",
            state="PROGRESS",
            info={"step": "Migrating data volume 1/1: Database Data..."},
        )
        history_run = {
            "vm_id": "vm-1",
            "source_profile": "SOURCE",
            "dest_profile": "TARGET",
            "dest_bucket": "images",
            "data_volume_ids": ["source-volume"],
            "data_volume_method": "restore",
            "destination_availability_domain": "target-ad-1",
            "restart_source_vm": False,
            "source_initial_state": "RUNNING",
            "source_final_state": "STOPPED",
            "data_volume_results": [
                {
                    "source_volume_id": "source-volume",
                    "source_volume_name": "Database Data",
                    "target_volume_name": "MIGR-server-1-Database Data-run12345",
                    "target_volume_id": "target-volume",
                    "status": "available",
                }
            ],
        }

        with patch.object(main, "AsyncResult", return_value=task), \
                patch.object(main, "get_job_run", return_value=history_run):
            result = await main.get_migration_status("run-1")

        self.assertEqual(result["status"], "PROGRESS")
        self.assertEqual(result["migration"]["data_volume_ids"], ["source-volume"])
        self.assertEqual(result["migration"]["data_volume_method"], "restore")
        self.assertFalse(result["migration"]["restart_source_vm"])
        self.assertEqual(result["migration"]["source_final_state"], "STOPPED")
        self.assertEqual(
            result["migration"]["data_volume_results"][0]["target_volume_id"],
            "target-volume",
        )


if __name__ == "__main__":
    unittest.main()
