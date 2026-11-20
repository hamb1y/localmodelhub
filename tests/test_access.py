import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lmh.access import configure


class AccessTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = root / "state"
        self.state.mkdir()
        self.policy = root / "config" / "policy.json"
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(redirect_stderr(io.StringIO()))
        self.restart = self.enterContext(patch("lmh.access._restart"))

    def configure(self, *args):
        self.stdout.seek(0)
        self.stdout.truncate()
        configure(*args, paths=(self.policy, self.state))
        return self.stdout.getvalue().strip()

    def test_named_keys_are_hashed_listable_and_revocable(self):
        key = self.configure("api-key", "create", "laptop")
        self.assertRegex(key, r"^lmh_[A-Za-z0-9_-]{43}$")
        stored = self.policy.read_text()
        self.assertNotIn(key, stored)
        self.assertEqual(json.loads(stored)["inference_keys"], {"laptop": hashlib.sha256(key.encode()).hexdigest()})
        self.assertEqual(self.policy.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.configure("api-key", "list"), "laptop")
        self.restart.assert_called_once_with(self.state)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.configure("api-key", "create", "laptop")
        self.configure("api-key", "revoke", "laptop")
        self.assertEqual(json.loads(self.policy.read_text())["inference_keys"], {})

    def test_cloudflare_requires_a_key_and_keeps_only_one_hostname(self):
        with self.assertRaisesRegex(ValueError, "Create an inference key"):
            self.configure("cloudflare", "setup", "models.example.net")
        self.configure("api-key", "create", "phone")
        with self.assertRaisesRegex(ValueError, "hostname"):
            self.configure("cloudflare", "setup", "https://models.example.net")
        output = self.configure("cloudflare", "setup", "Models.Example.Net")
        self.assertIn("http://127.0.0.1:5101", output)
        self.assertIn("https://models.example.net/v1", output)
        self.assertEqual(json.loads(self.policy.read_text())["inference_public_hostname"], "models.example.net")
        self.configure("cloudflare", "disable")
        self.assertNotIn("inference_public_hostname", json.loads(self.policy.read_text()))
