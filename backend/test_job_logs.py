import json
import tempfile
import unittest
from pathlib import Path

from job_logs import summarize_rclone_json_log


class SummarizeRcloneJsonLogTests(unittest.TestCase):
    def test_uses_average_speed_when_final_rclone_speed_is_zero(self):
        payload = {
            "level": "info",
            "msg": "Transferred",
            "stats": {
                "bytes": 4_194_304,
                "totalBytes": 4_194_304,
                "transfers": 2,
                "totalTransfers": 2,
                "elapsedTime": 2,
                "speed": 0,
                "errors": 0,
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "rclone_test.log"
            log_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            summary = summarize_rclone_json_log(log_path)

        self.assertEqual(summary["bytes"], 4_194_304)
        self.assertEqual(summary["speed_bps"], 2_097_152)

    def test_keeps_nonzero_rclone_speed(self):
        payload = {
            "level": "info",
            "msg": "Transferred",
            "stats": {
                "bytes": 4_194_304,
                "totalBytes": 8_388_608,
                "elapsedTime": 2,
                "speed": 1_000_000,
                "errors": 0,
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "rclone_test.log"
            log_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            summary = summarize_rclone_json_log(log_path)

        self.assertEqual(summary["speed_bps"], 1_000_000)


if __name__ == "__main__":
    unittest.main()
