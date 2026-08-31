import unittest
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


if __name__ == "__main__":
    unittest.main()
