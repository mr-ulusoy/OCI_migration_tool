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
        self.assertEqual(result["cert_path"], "")

    def test_requires_absolute_custom_certificate_paths(self):
        with self.assertRaises(HTTPException) as raised:
            self.normalize(
                mode="custom",
                hostname="migrator.example.com",
                cert_path="certificate.pem",
                key_path="/etc/company/key.pem",
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("absolute", raised.exception.detail)

    def test_rejects_invalid_hostname(self):
        with self.assertRaises(HTTPException) as raised:
            self.normalize(mode="letsencrypt", hostname="https://migrator.example.com/path")

        self.assertEqual(raised.exception.status_code, 400)

    def test_http_setup_does_not_require_hostname(self):
        result = self.normalize(mode="http")

        self.assertEqual(result["mode"], "http")
        self.assertEqual(result["hostname"], "")

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
        }

        with patch.object(main, "read_runtime_env", return_value=current):
            result = main.preserve_server_tls_env(imported).decode("utf-8")

        self.assertIn("OCI_MIGRATOR_API_TOKEN=imported", result)
        self.assertIn("OCI_MIGRATOR_TLS_MODE=letsencrypt", result)
        self.assertIn("OCI_MIGRATOR_TLS_HOSTNAME=new.example.com", result)
        self.assertNotIn("old.example.com", result)


if __name__ == "__main__":
    unittest.main()
