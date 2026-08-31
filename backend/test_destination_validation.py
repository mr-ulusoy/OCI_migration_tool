import unittest
from unittest.mock import Mock, patch

import oci
from fastapi import HTTPException

import main


class ValidateDestinationBucketTests(unittest.TestCase):
    def test_accepts_existing_bucket_and_ignores_object_prefix(self):
        client = Mock()
        with patch.object(main, "object_storage_context", return_value=({}, client, "namespace")):
            bucket_name = main.validate_destination_bucket("Lab", "existing-bucket/prefix")

        self.assertEqual(bucket_name, "existing-bucket")
        client.get_bucket.assert_called_once_with("namespace", "existing-bucket")

    def test_rejects_missing_bucket_with_actionable_message(self):
        client = Mock()
        client.get_bucket.side_effect = oci.exceptions.ServiceError(
            status=404,
            code="BucketNotFound",
            headers={"opc-request-id": "test-request"},
            message="The bucket does not exist.",
        )
        with patch.object(main, "object_storage_context", return_value=({}, client, "namespace")):
            with self.assertRaises(HTTPException) as raised:
                main.validate_destination_bucket("Lab", "missing-bucket/prefix")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("missing-bucket", raised.exception.detail["message"])
        self.assertIn("existing bucket", raised.exception.detail["hint"])

    def test_rejects_missing_profile(self):
        missing_profile = oci.exceptions.ConfigFileNotFound("missing config")
        with patch.object(main, "object_storage_context", side_effect=missing_profile):
            with self.assertRaises(HTTPException) as raised:
                main.validate_destination_bucket("Missing", "bucket")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Missing", raised.exception.detail["message"])


if __name__ == "__main__":
    unittest.main()
