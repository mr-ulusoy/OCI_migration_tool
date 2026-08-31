import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import main


class NormalizeNetworkSettingsTests(unittest.TestCase):
    def normalize(self, **values):
        request = main.NetworkSettingsRequest(**values)
        with patch.object(Path, "is_dir", return_value=True):
            return main.normalize_network_settings(request)

    def test_accepts_dhcp_without_static_fields(self):
        result = self.normalize(mode="dhcp", interface="ens3")

        self.assertEqual(result["mode"], "dhcp")
        self.assertEqual(result["interface"], "ens3")
        self.assertEqual(result["address"], "")

    def test_normalizes_static_ipv4_and_dns(self):
        result = self.normalize(
            mode="static",
            interface="ens3",
            address="10.0.1.20",
            prefix_length=24,
            gateway="10.0.1.1",
            dns_servers="1.1.1.1, 8.8.8.8",
        )

        self.assertEqual(result["address"], "10.0.1.20/24")
        self.assertEqual(result["gateway"], "10.0.1.1")
        self.assertEqual(result["dns_servers"], "1.1.1.1 8.8.8.8")

    def test_rejects_loopback_interface(self):
        with self.assertRaises(HTTPException) as raised:
            self.normalize(mode="dhcp", interface="lo")

        self.assertEqual(raised.exception.status_code, 400)

    def test_rejects_network_address(self):
        with self.assertRaises(HTTPException) as raised:
            self.normalize(
                mode="static",
                interface="ens3",
                address="10.0.1.0",
                prefix_length=24,
                gateway="10.0.1.1",
                dns_servers="1.1.1.1",
            )

        self.assertIn("network or broadcast", raised.exception.detail)

    def test_rejects_invalid_dns(self):
        with self.assertRaises(HTTPException) as raised:
            self.normalize(
                mode="static",
                interface="ens3",
                address="10.0.1.20",
                prefix_length=24,
                gateway="10.0.1.1",
                dns_servers="not-an-ip",
            )

        self.assertIn("DNS servers", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
