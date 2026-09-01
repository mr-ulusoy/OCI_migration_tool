import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

import main
import worker


def response(data):
    return SimpleNamespace(data=data)


class VmMigrationValidationTests(unittest.TestCase):
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
        self.assertEqual(completed[0]["status"], "available")


if __name__ == "__main__":
    unittest.main()
