import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class UpgradeCheckTests(unittest.TestCase):
    def test_returns_latest_commit_title_after_fetch(self):
        current = {
            "branch": "main",
            "current_commit": "a" * 40,
            "current_short": "aaaaaaa",
            "remote_url": "https://github.com/example/repo.git",
        }
        latest = "b" * 40

        with patch.object(main, "current_git_info", return_value=current), patch.object(
            main,
            "git_command",
            side_effect=[f"{latest}\trefs/heads/main", ""],
        ) as git, patch.object(
            main,
            "safe_git_command",
            side_effect=["", "feat: improve dashboard update details"],
        ):
            result = main.latest_git_info()

        self.assertFalse(result["up_to_date"])
        self.assertEqual(result["latest_short"], "bbbbbbb")
        self.assertEqual(result["latest_title"], "feat: improve dashboard update details")
        self.assertEqual(git.call_args_list[1].args[0][:3], ["fetch", "--quiet", "--no-tags"])

    def test_update_check_still_works_when_title_fetch_fails(self):
        current = {
            "branch": "main",
            "current_commit": "a" * 40,
            "current_short": "aaaaaaa",
            "remote_url": "https://github.com/example/repo.git",
        }
        latest = "b" * 40

        with patch.object(main, "current_git_info", return_value=current), patch.object(
            main,
            "git_command",
            side_effect=[f"{latest}\trefs/heads/main", RuntimeError("fetch failed")],
        ), patch.object(main, "safe_git_command", return_value=""):
            result = main.latest_git_info()

        self.assertEqual(result["latest_commit"], latest)
        self.assertEqual(result["latest_title"], "")

    def test_automatic_update_check_is_cached_for_24_hours(self):
        current = {
            "branch": "main",
            "current_commit": "a" * 40,
            "current_short": "aaaaaaa",
            "remote_url": "https://github.com/example/repo.git",
        }
        latest = {
            **current,
            "latest_commit": "b" * 40,
            "latest_short": "bbbbbbb",
            "latest_title": "feat: cached update check",
            "up_to_date": False,
        }

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            main,
            "UPGRADE_CHECK_FILE",
            Path(temporary) / "check.json",
        ), patch.object(main, "current_git_info", return_value=current), patch.object(
            main,
            "latest_git_info",
            return_value=latest,
        ) as latest_info, patch.object(main.time, "time", side_effect=[1000.0, 1100.0]):
            first = main.cached_latest_git_info()
            second = main.cached_latest_git_info()

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["latest_title"], "feat: cached update check")
        self.assertEqual(latest_info.call_count, 1)

    def test_automatic_update_check_refreshes_after_24_hours(self):
        current = {
            "branch": "main",
            "current_commit": "a" * 40,
            "current_short": "aaaaaaa",
            "remote_url": "https://github.com/example/repo.git",
        }
        latest = {
            **current,
            "latest_commit": "b" * 40,
            "latest_short": "bbbbbbb",
            "latest_title": "feat: daily refresh",
            "up_to_date": False,
        }

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            main,
            "UPGRADE_CHECK_FILE",
            Path(temporary) / "check.json",
        ), patch.object(main, "current_git_info", return_value=current), patch.object(
            main,
            "latest_git_info",
            return_value=latest,
        ) as latest_info, patch.object(
            main.time,
            "time",
            side_effect=[
                1000.0,
                1000.0 + main.UPGRADE_CHECK_INTERVAL_SECONDS + 1,
                1000.0 + main.UPGRADE_CHECK_INTERVAL_SECONDS + 1,
            ],
        ):
            main.cached_latest_git_info()
            refreshed = main.cached_latest_git_info()

        self.assertFalse(refreshed["cached"])
        self.assertEqual(latest_info.call_count, 2)


if __name__ == "__main__":
    unittest.main()
