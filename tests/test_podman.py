import subprocess
import unittest
from unittest.mock import patch

from lmh.podman import Podman, PodmanError, image_digest, image_name, image_reference


DIGEST = "a" * 64
IMAGE = f"ghcr.io/example/llama.cpp:server@sha256:{DIGEST}"


class ImageReferenceTests(unittest.TestCase):
    def test_accepts_digest_pinned_references(self):
        for value in (
            IMAGE,
            f"docker.io/library/alpine@sha256:{DIGEST}",
            f"localhost:5000/team/model:tag@sha256:{DIGEST}",
            f"model@sha256:{DIGEST}",
        ):
            with self.subTest(value=value):
                self.assertEqual(image_reference(value), value)

    def test_requires_a_sha256_digest_for_managed_images(self):
        for value in ("ghcr.io/example/llama:server", "ghcr.io/example/llama", "llama"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "pinned"):
                image_reference(value)
        self.assertEqual(image_reference("example/model:tag", require_digest=False),
                         "example/model:tag")

    def test_rejects_malformed_or_unsafe_references(self):
        for value in (
            "", None, 7, "ghcr.io/Example/Model@sha256:" + DIGEST, "ghcr.io//model",
            "ghcr.io/model:", "ghcr.io/model@sha256:xyz", "ghcr.io/model@sha256:" + "a" * 63,
            "ghcr.io/model name", "ghcr.io/model\n@sha256:" + DIGEST, "a" * 513,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                image_reference(value, require_digest=False)

    def test_digest_and_name_helpers(self):
        self.assertEqual(image_digest(IMAGE), DIGEST)
        self.assertIsNone(image_digest("example/model:tag"))
        self.assertEqual(image_name(IMAGE), "ghcr.io/example/llama.cpp:server")


class PodmanTests(unittest.TestCase):
    def setUp(self):
        self.podman = Podman(executable="/usr/bin/podman")

    def completed(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)

    def test_missing_executable_has_an_actionable_error(self):
        with patch("lmh.podman.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(PodmanError, "Podman is required"):
                self.podman.available()

    def test_available_checks_rootless_runtime(self):
        with patch("lmh.podman.subprocess.run", return_value=self.completed(stdout="crun\n")) as run:
            self.podman.available()
        run.assert_called_once_with(
            ["/usr/bin/podman", "info", "--format", "{{.Host.OCIRuntime.Name}}"],
            timeout=20, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        with patch("lmh.podman.subprocess.run", return_value=self.completed(1, stderr="denied")):
            with self.assertRaisesRegex(PodmanError, "subuid"):
                self.podman.available()

    def test_ensure_is_a_noop_when_the_pinned_image_is_present(self):
        with patch.object(self.podman, "_call", return_value=self.completed()) as call:
            self.assertEqual(self.podman.ensure(IMAGE), IMAGE)
        call.assert_called_once_with(["image", "exists", IMAGE], 20)

    def test_ensure_pulls_only_the_pinned_reference(self):
        calls = []

        def call(args, timeout):
            calls.append((args, timeout))
            if args[0] == "image":
                return self.completed(1 if len(calls) == 1 else 0)
            return self.completed()

        progress = []
        with patch.object(self.podman, "_call", side_effect=call):
            self.assertEqual(self.podman.ensure(IMAGE, progress.append), IMAGE)
        self.assertEqual(calls[0], (["image", "exists", IMAGE], 20))
        self.assertEqual(calls[1][0], ["pull", IMAGE])
        self.assertEqual(calls[2], (["image", "exists", IMAGE], 20))
        self.assertEqual(len(progress), 1)
        self.assertIn("ghcr.io/example/llama.cpp:server", progress[0])

    def test_failed_pull_does_not_echo_registry_output(self):
        def call(args, timeout):
            if args[0] == "image":
                return self.completed(1)
            return self.completed(1, stderr="https://user:secret@registry.invalid/denied")

        with patch.object(self.podman, "_call", side_effect=call):
            with self.assertRaises(PodmanError) as error:
                self.podman.ensure(IMAGE)
        self.assertNotIn("secret", str(error.exception))

    def test_ensure_rejects_an_unpinned_reference_without_running_podman(self):
        with patch.object(self.podman, "_call") as call:
            with self.assertRaisesRegex(ValueError, "pinned"):
                self.podman.ensure("ghcr.io/example/llama:server")
        call.assert_not_called()
