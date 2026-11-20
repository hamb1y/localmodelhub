import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from lmh.local_admin import _login


class HuggingFaceLoginTest(unittest.TestCase):
    def test_existing_credentials_are_not_reprinted_or_overwritten(self):
        output = io.StringIO()
        with (
            patch("huggingface_hub.get_token", return_value="private-credential"),
            patch("huggingface_hub.login") as login,
            redirect_stdout(output),
        ):
            self.assertEqual(_login(), 0)
        login.assert_not_called()
        self.assertNotIn("private-credential", output.getvalue())

    def test_login_reads_a_hidden_token_without_passing_it_to_git(self):
        with (
            patch("huggingface_hub.get_token", return_value=None),
            patch("huggingface_hub.login") as login,
            patch("lmh.local_admin.sys.stdin.isatty", return_value=True),
            patch("lmh.local_admin.sys.stderr.isatty", return_value=True),
            patch("lmh.local_admin.getpass.getpass", return_value="private-credential") as prompt,
        ):
            self.assertEqual(_login(), 0)
        prompt.assert_called_once()
        login.assert_called_once_with(token="private-credential", add_to_git_credential=False)

    def test_unattended_use_never_prompts_for_credentials(self):
        with (
            patch("huggingface_hub.get_token", return_value=None),
            patch("lmh.local_admin.sys.stdin.isatty", return_value=False),
            patch("lmh.local_admin.getpass.getpass") as prompt,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(_login(), 1)
        prompt.assert_not_called()
