import hashlib
import os
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lmh.artifacts import ArtifactError, ArtifactStore


class ArtifactStoreTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "weights.gguf"
        self.data = b"abc"
        self.path.write_bytes(self.data)
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        self.store = ArtifactStore(self.root / "cache", token="my-private-token")
        self.local = {
            "source": {"kind": "local", "path": str(self.path)},
            "hashes": {"sha256": self.sha256},
            "size": len(self.data),
        }
        self.hf = {
            "source": {"kind": "hf", "repo": "org/model", "revision": "A" * 40,
                       "file": "folder/weights.gguf"},
            "hashes": {"sha256": self.sha256},
        }

    def test_rechecks_modified_file_even_if_size_and_mtime_match(self):
        self.assertEqual(self.store.resolve(self.local), self.path)
        original = self.path.stat()
        self.path.write_bytes(b"def")
        os.utime(self.path, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaisesRegex(ArtifactError, "sha256 checksum mismatch"):
            self.store.resolve(self.local, download=True)
        self.path.write_bytes(b"longer")
        with self.assertRaisesRegex(ArtifactError, "size mismatch"):
            self.store.resolve(self.local)

    def test_caches_hashes_by_identity_and_supports_forced_verification(self):
        alias = self.root / "alias.gguf"
        alias.symlink_to(self.path)
        via_alias = {**self.local, "source": {"kind": "local", "path": str(alias)}}
        with patch("lmh.artifacts.hashlib.sha256", wraps=hashlib.sha256) as sha256:
            self.assertEqual(self.store.resolve(self.local), self.path)
            self.assertEqual(self.store.resolve(via_alias), alias)
            self.assertEqual(sha256.call_count, 1)
            self.assertEqual(self.store.resolve(self.local, force_verify=True), self.path)
            self.assertEqual(self.store.resolve(self.local), self.path)
            self.assertEqual(sha256.call_count, 2)
            wrong_hash = {**self.local, "hashes": {"sha256": "0" * 64}}
            for _ in range(2):
                with self.assertRaisesRegex(ArtifactError, "sha256 checksum mismatch"):
                    self.store.resolve(wrong_hash)
            self.assertEqual(sha256.call_count, 4)

    def test_rehashes_replacement_with_same_size_and_mtime(self):
        self.assertEqual(self.store.resolve(self.local), self.path)
        original = self.path.stat()
        replacement = self.root / "replacement.gguf"
        replacement.write_bytes(b"def")
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        replacement.replace(self.path)
        with self.assertRaisesRegex(ArtifactError, "sha256 checksum mismatch"):
            self.store.resolve(self.local)

    def test_failed_forced_verification_invalidates_previous_success(self):
        self.store.resolve(self.local)
        bad_digest = Mock()
        bad_digest.hexdigest.return_value = "0" * 64
        with patch("lmh.artifacts.hashlib.sha256", return_value=bad_digest) as sha256:
            with self.assertRaisesRegex(ArtifactError, "sha256 checksum mismatch"):
                self.store.resolve(self.local, force_verify=True)
            with self.assertRaisesRegex(ArtifactError, "sha256 checksum mismatch"):
                self.store.resolve(self.local)
            self.assertEqual(sha256.call_count, 2)

    def test_detects_in_place_write_during_streamed_hashing(self):
        digest = Mock(wraps=hashlib.sha256())
        digest.update.side_effect = lambda _: self.path.write_bytes(b"def")
        with patch("lmh.artifacts.hashlib.sha256", return_value=digest):
            with self.assertRaisesRegex(ArtifactError, "changed during verification"):
                self.store.resolve(self.local)
        with self.assertRaisesRegex(ArtifactError, "sha256 checksum mismatch"):
            self.store.resolve(self.local)

    def test_rejects_symlink_retargeted_during_streamed_hashing(self):
        alias = self.root / "alias.gguf"
        alias.symlink_to(self.path)
        via_alias = {**self.local, "source": {"kind": "local", "path": str(alias)}}
        replacement = self.root / "replacement.gguf"
        replacement.write_bytes(b"def")
        real_digest = hashlib.sha256()

        def retarget(chunk):
            real_digest.update(chunk)
            alias.unlink()
            alias.symlink_to(replacement)

        digest = Mock(wraps=real_digest)
        digest.update.side_effect = retarget
        with patch("lmh.artifacts.hashlib.sha256", return_value=digest):
            with self.assertRaisesRegex(ArtifactError, "changed during verification"):
                self.store.resolve(via_alias)
        with self.assertRaisesRegex(ArtifactError, "sha256 checksum mismatch"):
            self.store.resolve(via_alias)

    def test_requires_a_regular_file_and_redacts_local_path(self):
        self.path.unlink()
        with self.assertRaisesRegex(ArtifactError, "does not exist") as caught:
            self.store.resolve(self.local)
        self.assertNotIn(str(self.path), str(caught.exception))
        self.path.mkdir()
        with self.assertRaisesRegex(ArtifactError, "regular file"):
            self.store.resolve(self.local)
        if hasattr(os, "mkfifo"):
            self.path.rmdir()
            os.mkfifo(self.path)
            with self.assertRaisesRegex(ArtifactError, "regular file"):
                self.store.resolve(self.local)

    def test_missing_blake3_dependency_is_not_silently_ignored(self):
        self.local["hashes"]["blake3"] = "0" * 64
        with patch.dict(sys.modules, {"blake3": None}):
            with self.assertRaisesRegex(ArtifactError, "requires the blake3 package"):
                self.store.resolve(self.local)

    def test_blake3_cannot_be_skipped_when_sha256_matches(self):
        # An optional dependency double checks the actual bytes it receives.
        known = "6437b3ac38465133ffb63b75273a8db548c558465d79db03fd359c6cd5bd9d85"
        digest = Mock()
        digest.hexdigest.return_value = known
        module = SimpleNamespace(blake3=Mock(return_value=digest))
        self.local["hashes"]["blake3"] = known
        with patch.dict(sys.modules, {"blake3": module}):
            self.assertEqual(self.store.resolve(self.local), self.path)
            digest.update.assert_called_once_with(self.data)
            self.local["hashes"]["blake3"] = "0" * 64
            with self.assertRaisesRegex(ArtifactError, "blake3 checksum mismatch"):
                self.store.resolve(self.local)

    def test_pinned_hf_download_uses_explicit_token_or_normal_hub_login(self):
        hub = Mock(return_value=str(self.path))
        with patch.dict(sys.modules, {"huggingface_hub": SimpleNamespace(hf_hub_download=hub)}):
            self.assertEqual(self.store.resolve(self.hf), self.path)
            self.assertEqual(self.store.resolve(self.hf, download=True), self.path)
            self.assertEqual(ArtifactStore(self.root / "cache").resolve(self.hf, download=True), self.path)
            for index, call in enumerate(hub.call_args_list):
                self.assertEqual(call.kwargs, {
                    "repo_id": "org/model", "revision": "a" * 40, "filename": "folder/weights.gguf",
                    "repo_type": "model", "cache_dir": self.root / "cache", "local_files_only": index == 0,
                    "token": (False, "my-private-token", None)[index], "endpoint": "https://huggingface.co",
                })
            self.path.write_bytes(b"wrong")
            with self.assertRaisesRegex(ArtifactError, "sha256 checksum mismatch"):
                self.store.resolve(self.hf)

    def test_rejects_mutable_revision_before_any_hub_call(self):
        hub = Mock()
        self.hf["source"]["revision"] = "main"
        with patch.dict(sys.modules, {"huggingface_hub": SimpleNamespace(hf_hub_download=hub)}):
            with self.assertRaisesRegex(ArtifactError, "full 40-digit Git commit hash"):
                self.store.resolve(self.hf, download=True)
        hub.assert_not_called()

    def test_redacts_credentials_in_backend_errors(self):
        hub = Mock(side_effect=OSError("https://host/file?token=my-private-token"))
        with patch.dict(sys.modules, {"huggingface_hub": SimpleNamespace(hf_hub_download=hub)}):
            for download, message in [(False, "local cache"), (True, "download failed")]:
                with self.subTest(download=download):
                    with self.assertRaisesRegex(ArtifactError, message) as caught:
                        self.store.resolve(self.hf, download=download)
                    displayed = "".join(traceback.format_exception(caught.exception))
                    self.assertNotIn("my-private-token", displayed)
                    self.assertNotIn("https://host", displayed)
