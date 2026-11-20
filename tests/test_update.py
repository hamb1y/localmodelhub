import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmh import update


OLD = "a" * 40
NEW = "b" * 40


class UpdateTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = root / "installed"
        self.current = self.data / "current"
        self.current.mkdir(parents=True)
        (self.current / ".ready").touch()
        self.marker = self.current / "source-commit"
        self.marker.write_text(OLD + "\n")
        self.git = self.enterContext(patch.object(update, "git"))
        self.run = self.enterContext(patch.object(update.subprocess, "run"))

    def test_unchanged_main_skips_the_clone_and_build(self):
        self.git.return_value = f"{OLD}\t{update.REF}"
        update.update(self.data)
        self.git.assert_called_once_with("ls-remote", "--exit-code", update.REPO, "refs/heads/main")
        self.run.assert_not_called()

    def test_update_uses_the_checked_out_main_commit_without_onboarding(self):
        self.git.side_effect = [f"{NEW}\t{update.REF}", "", NEW]

        def install(args, *, cwd, check):
            self.assertEqual(args, ["bash", "deploy/install.sh"])
            self.assertEqual(cwd.parent.parent, self.data)
            self.marker.write_text(NEW + "\n")
            return type("Exit", (), {"returncode": 0})()

        self.run.side_effect = install
        update.update(self.data)
        clone = self.git.call_args_list[1].args
        self.assertEqual(clone[:7], ("clone", "--depth", "1", "--single-branch", "--branch", "main", update.REPO))
        self.assertEqual(list(self.data.glob(".update.*")), [])

    def test_moving_branch_and_bad_refs_are_not_installed(self):
        for advertised in (f"{NEW}\trefs/heads/other", f"not-a-hash\t{update.REF}"):
            self.git.side_effect = [advertised]
            with self.subTest(advertised=advertised), self.assertRaises(update.UpdateError):
                update.update(self.data)
        self.git.side_effect = [f"{NEW}\t{update.REF}", "", OLD]
        with self.assertRaisesRegex(update.UpdateError, "advanced"):
            update.update(self.data)
        self.run.assert_not_called()

    def test_failed_source_build_does_not_change_installed_commit(self):
        self.git.side_effect = [f"{NEW}\t{update.REF}", "", NEW]
        self.run.return_value.returncode = 17
        with self.assertRaisesRegex(update.UpdateError, "status 17"):
            update.update(self.data)
        self.assertEqual(update.installed_commit(self.data), OLD)
        self.assertEqual(list(self.data.glob(".update.*")), [])

    def test_uninstalled_does_not_fetch(self):
        (self.current / ".ready").unlink()
        with self.assertRaisesRegex(update.UpdateError, "not installed"):
            update.update(self.data)
        self.git.assert_not_called()
