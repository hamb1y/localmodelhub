import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmh import runtime_install as installer
from lmh.podman import PodmanError


class FakePodman:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.ensured = []
        self.checked = False

    def available(self):
        self.checked = True
        if self.error:
            raise self.error

    def ensure(self, reference):
        self.ensured.append(reference)
        return reference


class RuntimeInstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "data"
        self.policy = self.root / "config" / "policy.json"
        self.podman = FakePodman()
        self.real_user_guard = installer._require_unprivileged_user
        self.enterContext(patch.object(installer, "_require_unprivileged_user"))

    def _install(self, *, podman=...):
        client = self.podman if podman is ... else podman
        return installer.install_cpu_runtime(self.data, self.policy, podman=client)

    def test_pulls_verified_image_and_atomically_approves_it(self):
        self.policy.parent.mkdir()
        original = {
            "api_token": "secret",
            "runtimes": {"custom": {"setting": "keep"}, "llama-cpu": {"original": True}},
            "extra": [1, 2],
        }
        self.policy.write_text(json.dumps(original))
        self.policy.chmod(0o644)

        image = self._install()

        self.assertEqual(image, installer._STARTER_IMAGE)
        self.assertTrue(image.startswith("ghcr.io/ggml-org/llama.cpp:server@sha256:"))
        self.assertEqual(self.podman.ensured, [image])
        self.assertTrue(self.podman.checked)
        self.assertEqual(self.policy.stat().st_mode & 0o777, 0o600)
        original["runtimes"]["llama-cpu"] = {"adapter": "llama.cpp", "image": image}
        self.assertEqual(json.loads(self.policy.read_text()), original)
        self.assertEqual(list(self.policy.parent.iterdir()), [self.policy])
        self.assertTrue(self.data.is_dir())

    def test_starter_catalog_and_installer_pin_the_same_image(self):
        starter = json.loads((Path(__file__).resolve().parents[1] / "catalogs/starter.json").read_text())
        self.assertEqual(starter["runtimes"]["llama-cpu"]["image"], installer._STARTER_IMAGE)
        for runtime in starter["runtimes"].values():
            self.assertIn("@sha256:", runtime["image"])

    def test_podman_failure_leaves_policy_untouched(self):
        self.policy.parent.mkdir()
        self.policy.write_text('{"other": true}\n')
        before = self.policy.read_bytes()
        for failure in (PodmanError("rootless Podman is not usable"), PodmanError("pull failed")):
            with self.subTest(failure=failure), self.assertRaisesRegex(installer.RuntimeInstallError, str(failure)):
                self._install(podman=FakePodman(failure))
            self.assertEqual(self.policy.read_bytes(), before)

    def test_rejects_invalid_policy_and_policy_symlinks(self):
        self.policy.parent.mkdir()
        self.policy.write_text('{"runtimes": []}')
        with self.assertRaisesRegex(installer.RuntimeInstallError, "JSON objects"):
            self._install()
        target = self.policy.rename(self.policy.with_name("other.json"))
        self.policy.symlink_to(target)
        with self.assertRaisesRegex(installer.RuntimeInstallError, "securely open"):
            self._install()
        self.assertEqual(target.read_text(), '{"runtimes": []}')
        self.assertEqual(self.podman.ensured, [])

    def test_refuses_unsafe_policy_directory_before_pulling(self):
        self.policy.parent.mkdir()
        self.policy.parent.chmod(0o777)
        with self.assertRaisesRegex(installer.RuntimeInstallError, "Unsafe directory"):
            self._install()
        self.assertEqual(self.podman.ensured, [])

    def test_root_and_setuid_callers_are_refused(self):
        self.enterContext(patch.object(installer, "_require_unprivileged_user", self.real_user_guard))
        for uid, euid in [(0, 0), (1000, 0), (1000, 1001)]:
            with self.subTest(uid=uid, euid=euid), patch.object(installer.os, "getuid", return_value=uid), \
                    patch.object(installer.os, "geteuid", return_value=euid):
                with self.assertRaisesRegex(installer.RuntimeInstallError, "regular user"):
                    installer.install_cpu_runtime(self.data, self.policy, podman=self.podman)
        self.assertEqual(self.podman.ensured, [])

    def test_default_client_is_rootless_podman(self):
        fake = FakePodman()
        with patch.object(installer, "Podman", return_value=fake) as client:
            self._install(podman=None)
        client.assert_called_once_with()
        self.assertEqual(fake.ensured, [installer._STARTER_IMAGE])
