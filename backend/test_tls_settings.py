import asyncio
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import main


class NormalizeTlsSettingsTests(unittest.TestCase):
    def normalize(self, **values):
        return main.normalize_tls_settings(main.TlsSettingsRequest(**values))

    def test_accepts_lets_encrypt_hostname_and_email(self):
        result = self.normalize(
            mode="letsencrypt",
            hostname="Migrator.Example.com.",
            email="cloud@example.com",
        )

        self.assertEqual(result["mode"], "letsencrypt")
        self.assertEqual(result["hostname"], "migrator.example.com")
        self.assertEqual(result["email"], "cloud@example.com")

    def test_accepts_external_tls_without_certificate_paths(self):
        result = self.normalize(mode="external", hostname="migrator.internal.example")

        self.assertEqual(result["mode"], "external")
        self.assertNotIn("cert_path", result)

    def test_accepts_custom_mode_with_hostname(self):
        result = self.normalize(mode="custom", hostname="migrator.example.com")

        self.assertEqual(result["mode"], "custom")
        self.assertEqual(result["hostname"], "migrator.example.com")

    def test_rejects_non_pem_certificate_upload(self):
        upload = main.UploadFile(filename="certificate.txt", file=io.BytesIO(b"not a certificate"))

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(main.read_tls_upload(upload, "Certificate chain", b"-----BEGIN CERTIFICATE-----"))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("PEM", raised.exception.detail)

    def test_rejects_oversized_tls_upload(self):
        content = b"-----BEGIN CERTIFICATE-----\n" + (b"A" * main.TLS_UPLOAD_MAX_BYTES)
        upload = main.UploadFile(filename="certificate.pem", file=io.BytesIO(content))

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(main.read_tls_upload(upload, "Certificate chain", b"-----BEGIN CERTIFICATE-----"))

        self.assertEqual(raised.exception.status_code, 413)

    def test_corporate_upload_uses_and_removes_private_temporary_files(self):
        certificate = main.UploadFile(
            filename="fullchain.pem",
            file=io.BytesIO(b"-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"),
        )
        private_key = main.UploadFile(
            filename="private-key.pem",
            file=io.BytesIO(b"-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n"),
        )

        with patch.object(main, "run_tls_helper", return_value={"status": "ok"}) as helper:
            result = asyncio.run(
                main.update_corporate_tls_settings(
                    hostname="migrator.example.com",
                    certificate=certificate,
                    private_key=private_key,
                )
            )

        command = helper.call_args.args[0]
        certificate_path = Path(command[command.index("--cert-path") + 1])
        private_key_path = Path(command[command.index("--key-path") + 1])
        self.assertEqual(result["status"], "ok")
        self.assertFalse(certificate_path.exists())
        self.assertFalse(private_key_path.exists())

    def test_json_tls_endpoint_rejects_custom_mode_without_uploads(self):
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                main.update_tls_settings(
                    main.TlsSettingsRequest(mode="custom", hostname="migrator.example.com")
                )
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Upload", raised.exception.detail)

    def test_rejects_invalid_hostname(self):
        with self.assertRaises(HTTPException) as raised:
            self.normalize(mode="letsencrypt", hostname="https://migrator.example.com/path")

        self.assertEqual(raised.exception.status_code, 400)

    def test_http_setup_does_not_require_hostname(self):
        result = self.normalize(mode="http", acknowledge_http=True)

        self.assertEqual(result["mode"], "http")
        self.assertEqual(result["hostname"], "")
        self.assertTrue(result["acknowledge_http"])

    def test_http_setup_requires_acknowledgement(self):
        with self.assertRaises(HTTPException) as raised:
            self.normalize(mode="http", acknowledge_http=False)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("not encrypted", raised.exception.detail)

    def test_runtime_import_keeps_server_specific_tls_values(self):
        imported = (
            "OCI_MIGRATOR_API_TOKEN=imported\n"
            "OCI_MIGRATOR_TLS_MODE=custom\n"
            "OCI_MIGRATOR_TLS_HOSTNAME=old.example.com\n"
            "OCI_MIGRATOR_ALLOWED_ORIGINS=https://old.example.com\n"
        ).encode("utf-8")
        current = {
            "OCI_MIGRATOR_TLS_MODE": "letsencrypt",
            "OCI_MIGRATOR_TLS_HOSTNAME": "new.example.com",
            "OCI_MIGRATOR_ALLOWED_ORIGINS": "https://new.example.com",
            "OCI_MIGRATOR_TLS_HTTP_ACKNOWLEDGED": "true",
        }

        with patch.object(main, "read_runtime_env", return_value=current):
            result = main.preserve_server_tls_env(imported).decode("utf-8")

        self.assertIn("OCI_MIGRATOR_API_TOKEN=imported", result)
        self.assertIn("OCI_MIGRATOR_TLS_MODE=letsencrypt", result)
        self.assertIn("OCI_MIGRATOR_TLS_HOSTNAME=new.example.com", result)
        self.assertIn("OCI_MIGRATOR_TLS_HTTP_ACKNOWLEDGED=true", result)
        self.assertNotIn("old.example.com", result)


if __name__ == "__main__":
    unittest.main()
