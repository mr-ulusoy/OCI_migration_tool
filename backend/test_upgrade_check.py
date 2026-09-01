import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class UpgradeCheckTests(unittest.TestCase):
    def current(self, version="0.1.0"):
        return {
            "branch": "main",
            "current_commit": "a" * 40,
            "current_short": "aaaaaaa",
            "current_version": version,
            "remote_url": "https://github.com/example/repo.git",
        }

    def release(self, version="0.2.0"):
        return {
            "tag_name": f"v{version}",
            "name": f"Cloud Migration Console v{version}",
            "html_url": f"https://github.com/example/repo/releases/tag/v{version}",
            "published_at": "2026-09-01T12:00:00Z",
        }

    def test_detects_newer_published_release(self):
        with patch.object(main, "current_git_info", return_value=self.current()), patch.object(
            main, "safe_git_command", return_value="https://github.com/example/repo.git"
        ), patch.object(main, "fetch_latest_github_release", return_value=self.release()):
            result = main.latest_release_info()

        self.assertTrue(result["release_available"])
        self.assertTrue(result["update_available"])
        self.assertFalse(result["up_to_date"])
        self.assertEqual(result["latest_tag"], "v0.2.0")
        self.assertEqual(result["latest_version"], "0.2.0")
        self.assertEqual(result["latest_title"], "Cloud Migration Console v0.2.0")

    def test_same_release_is_current_even_when_commit_differs(self):
        with patch.object(main, "current_git_info", return_value=self.current("0.2.0")), patch.object(
            main, "safe_git_command", return_value="https://github.com/example/repo.git"
        ), patch.object(main, "fetch_latest_github_release", return_value=self.release("0.2.0")):
            result = main.latest_release_info()

        self.assertFalse(result["update_available"])
        self.assertTrue(result["up_to_date"])
        self.assertEqual(result["status_message"], "You are on the latest published release.")

    def test_no_published_release_is_not_an_update(self):
        with patch.object(main, "current_git_info", return_value=self.current()), patch.object(
            main, "safe_git_command", return_value="https://github.com/example/repo.git"
        ), patch.object(main, "fetch_latest_github_release", return_value={}):
            result = main.latest_release_info()

        self.assertFalse(result["release_available"])
        self.assertFalse(result["update_available"])
        self.assertTrue(result["up_to_date"])

    def test_release_tags_must_match_semantic_version_format(self):
        with patch.object(main, "current_git_info", return_value=self.current()), patch.object(
            main, "safe_git_command", return_value="https://github.com/example/repo.git"
        ), patch.object(
            main,
            "fetch_latest_github_release",
            return_value={**self.release(), "tag_name": "release-latest"},
        ):
            with self.assertRaises(main.HTTPException) as context:
                main.latest_release_info()

        self.assertEqual(context.exception.status_code, 502)

    def test_github_repository_is_parsed_from_https_and_ssh_remotes(self):
        self.assertEqual(
            main.github_repository_from_remote("https://github.com/example/repo.git"),
            "example/repo",
        )
        self.assertEqual(
            main.github_repository_from_remote("git@github.com:example/repo.git"),
            "example/repo",
        )

    def test_automatic_release_check_is_cached_for_24_hours(self):
        current = self.current()
        latest = {
            **current,
            "release_available": True,
            "latest_tag": "v0.2.0",
            "latest_version": "0.2.0",
            "latest_title": "Cloud Migration Console v0.2.0",
            "update_available": True,
            "up_to_date": False,
        }

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            main, "UPGRADE_CHECK_FILE", Path(temporary) / "check.json"
        ), patch.object(main, "current_git_info", return_value=current), patch.object(
            main, "latest_release_info", return_value=latest
        ) as latest_info, patch.object(main.time, "time", side_effect=[1000.0, 1100.0]):
            first = main.cached_latest_release_info()
            second = main.cached_latest_release_info()

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(latest_info.call_count, 1)

    def test_automatic_release_check_refreshes_after_24_hours(self):
        current = self.current()
        latest = {
            **current,
            "release_available": True,
            "latest_tag": "v0.2.0",
            "latest_version": "0.2.0",
            "update_available": True,
            "up_to_date": False,
        }

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            main, "UPGRADE_CHECK_FILE", Path(temporary) / "check.json"
        ), patch.object(main, "current_git_info", return_value=current), patch.object(
            main, "latest_release_info", return_value=latest
        ) as latest_info, patch.object(
            main.time,
            "time",
            side_effect=[
                1000.0,
                1000.0 + main.UPGRADE_CHECK_INTERVAL_SECONDS + 1,
                1000.0 + main.UPGRADE_CHECK_INTERVAL_SECONDS + 1,
            ],
        ):
            main.cached_latest_release_info()
            refreshed = main.cached_latest_release_info()

        self.assertFalse(refreshed["cached"])
        self.assertEqual(latest_info.call_count, 2)


if __name__ == "__main__":
    unittest.main()
