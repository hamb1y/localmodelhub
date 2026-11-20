import hashlib
import io
import json
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from lmh import runtime_install as installer


_DIR = "llama-b12359"
_BIN = f"{_DIR}/llama-server"
_BINARY = b"mock executable"
_VERSION = b"version: 1.0.0-dev (build 12359, commit b1b770d90)\n"
_REAL_USER_GUARD = installer._require_unprivileged_user


def _member(name, kind=tarfile.REGTYPE, value=b"", *, mode=0o755):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    if info.isreg():
        info.size = len(value)
        return info, io.BytesIO(value)
    if info.issym() or info.islnk():
        info.linkname = value
    return info, None


def _archive(*members):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        archive.addfile(*_member(_DIR, tarfile.DIRTYPE))
        for member in members:
            archive.addfile(*member)
    return stream.getvalue()


class RuntimeInstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "data"
        self.policy = self.root / "config" / "policy.json"
        self._patch("platform.system", return_value="Linux")
        self.machine = self._patch("platform.machine", return_value="x86_64")
        self.user = self._patch("_require_unprivileged_user")
        self.opener = self._patch("build_opener").return_value
        self.probe = self._patch("subprocess.run", side_effect=self._probe)
        self.payload = _archive(
            _member(_BIN, value=_BINARY, mode=0o7755),
            _member(f"{_DIR}/libexample.so", tarfile.SYMTYPE, "libexample.so.1"),
            _member(f"{_DIR}/libexample.so.1", value=b"library", mode=0o666),
        )

    def _patch(self, target, **kwargs):
        patcher = patch(f"lmh.runtime_install.{target}", **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _probe(self, argv, *, cwd, **kwargs):
        self.assertEqual(argv, [str(cwd / "llama-server"), "--version"])
        self.assertTrue(cwd.parent.name.startswith(".cpu-runtime-"))
        self.assertEqual((cwd / "llama-server").read_bytes(), _BINARY)
        self.assertEqual(stat.S_IMODE((cwd / "llama-server").stat().st_mode), 0o700)
        self.assertNotIn("LD_PRELOAD", kwargs["env"])
        self.assertEqual(kwargs["env"]["CUDA_VISIBLE_DEVICES"], "")
        self.assertTrue(kwargs["check"])
        self.assertGreater(kwargs["timeout"], 0)
        return subprocess.CompletedProcess(argv, 0, stdout=_VERSION)

    def _install(self, *, payload=None, body=None):
        payload = self.payload if payload is None else payload
        self.opener.open.return_value = io.BytesIO(payload if body is None else body)
        release = installer._Release(
            "x64", len(payload), hashlib.sha256(payload).hexdigest(),
        )
        with patch.dict(installer._RELEASES, {"x86_64": release}):
            return installer.install_cpu_runtime(self.data, self.policy)

    def _assert_no_installation(self):
        self.assertEqual(list((self.data / "runtimes").iterdir()), [])
        self.assertFalse(self.policy.exists())
        self.probe.assert_not_called()

    def test_installs_verified_archive_and_atomically_updates_policy(self):
        self.policy.parent.mkdir()
        original = {
            "api_token": "secret",
            "runtimes": {"custom": {"setting": "keep"}, "llama-cpu": {"original": True}},
            "extra": [1, 2],
        }
        self.policy.write_text(json.dumps(original))
        self.policy.chmod(0o644)

        executable = self._install()

        self.assertEqual(executable, self.data / "runtimes" / f"{_DIR}-x64" / "llama-server")
        self.assertEqual(executable.read_bytes(), _BINARY)
        self.assertEqual((executable.parent / "libexample.so").readlink(), Path("libexample.so.1"))
        self.assertEqual((executable.parent / "libexample.so").read_bytes(), b"library")
        self.assertEqual(stat.S_IMODE(self.policy.stat().st_mode), 0o600)
        original["runtimes"]["llama-cpu"] = {
            "adapter": "llama.cpp", "version": "b12359", "executable": str(executable),
            "sha256": hashlib.sha256(_BINARY).hexdigest(),
        }
        self.assertEqual(json.loads(self.policy.read_text()), original)
        self.assertEqual(list(self.policy.parent.iterdir()), [self.policy])
        self.assertEqual(list((self.data / "runtimes").iterdir()), [executable.parent])
        self.probe.assert_called_once()

    def test_reuses_identical_install_but_refuses_changed_library(self):
        executable = self._install()
        inode = executable.stat().st_ino
        self.assertEqual(self._install(), executable)
        self.assertEqual(executable.stat().st_ino, inode)

        library = executable.parent / "libexample.so.1"
        library.write_bytes(b"changed")
        before = self.policy.read_bytes()
        with self.assertRaisesRegex(installer.RuntimeInstallError, "Conflicting"):
            self._install()
        self.assertEqual(library.read_bytes(), b"changed")
        self.assertEqual(self.policy.read_bytes(), before)

    def test_size_and_digest_must_match_before_extracting(self):
        for body, error in [
            (self.payload[:-1], "size"),
            (self.payload + b"x", "size"),
            (self.payload[:-1] + bytes([self.payload[-1] ^ 1]), "SHA-256"),
        ]:
            with self.subTest(error=error), patch.object(installer, "_extract") as extract:
                with self.assertRaisesRegex(installer.RuntimeInstallError, error):
                    self._install(body=body)
                extract.assert_not_called()
                self._assert_no_installation()

    def test_rejects_paths_special_files_and_links_before_probe(self):
        for member in [
            _member("../escaped", value=b"data"),
            _member(str(self.root / "escaped"), value=b"data"),
            _member(f"{_DIR}/../escaped", value=b"data"),
            _member(f"{_DIR}/pipe", tarfile.FIFOTYPE),
            _member(f"{_DIR}/hard", tarfile.LNKTYPE, _BIN),
            _member(f"{_DIR}/link", tarfile.SYMTYPE, "../../escaped"),
            _member(f"{_DIR}/link", tarfile.SYMTYPE, str(self.root / "escaped")),
            _member(f"{_DIR}/link", tarfile.SYMTYPE, "missing"),
            _member(f"{_DIR}/link", tarfile.SYMTYPE, "link"),
            _member(_BIN, value=_BINARY),
        ]:
            with self.subTest(name=member[0].name, target=member[0].linkname):
                with self.assertRaises(installer.RuntimeInstallError):
                    self._install(payload=_archive(_member(_BIN, value=_BINARY), member))
                self.assertFalse((self.root / "escaped").exists())
                self._assert_no_installation()

    def test_requires_regular_executable_at_pinned_path(self):
        for members in [
            [_member(f"{_DIR}/other", value=_BINARY)],
            [_member(_BIN, value=_BINARY, mode=0o644)],
            [_member(_BIN, tarfile.SYMTYPE, "other"), _member(f"{_DIR}/other", value=_BINARY)],
        ]:
            with self.subTest(members=members), self.assertRaises(installer.RuntimeInstallError):
                self._install(payload=_archive(*members))
            self._assert_no_installation()

    def test_limits_unpacked_bytes_and_member_count(self):
        for name, limit, payload in [
            ("_MAX_EXPANDED", 14000, _archive(_member(_BIN, value=b"x" * 16000))),
            ("_MAX_EXPANDED", 14000, _archive(_member(_BIN, value=_BINARY),
                                             _member(f"{_DIR}/" + "x" * 20000))),
            ("_MAX_MEMBERS", 2, self.payload),
        ]:
            with self.subTest(name=name), patch.object(installer, name, limit):
                with self.assertRaisesRegex(installer.RuntimeInstallError, "exceeds"):
                    self._install(payload=payload)
                self._assert_no_installation()

    def test_probe_failure_or_wrong_build_leaves_policy_untouched(self):
        self.policy.parent.mkdir()
        self.policy.write_text('{"other": true}\n')
        for failure in [
            subprocess.TimeoutExpired("llama-server", timeout=15),
            subprocess.CalledProcessError(1, "llama-server"),
            subprocess.CompletedProcess([], 0, stdout=_VERSION.replace(b"12359", b"123590")),
            subprocess.CompletedProcess([], 0, stdout=_VERSION.replace(b"b1b770d90", b"badc0ffee")),
        ]:
            with self.subTest(failure=failure):
                if isinstance(failure, Exception):
                    self.probe.side_effect = failure
                else:
                    self.probe.side_effect = None
                    self.probe.return_value = failure
                with self.assertRaisesRegex(installer.RuntimeInstallError, "--version"):
                    self._install()
                self.assertEqual(self.policy.read_text(), '{"other": true}\n')
                self.assertEqual(list((self.data / "runtimes").iterdir()), [])

    def test_rejects_invalid_policy_and_policy_symlinks_before_download(self):
        self.policy.parent.mkdir()
        self.policy.write_text('{"runtimes": []}')
        with self.assertRaisesRegex(installer.RuntimeInstallError, "JSON objects"):
            self._install()
        target = self.policy.rename(self.policy.with_name("other.json"))
        self.policy.symlink_to(target)
        with self.assertRaisesRegex(installer.RuntimeInstallError, "securely open"):
            self._install()
        self.assertEqual(target.read_text(), '{"runtimes": []}')
        self.opener.open.assert_not_called()

    def test_refuses_unsafe_runtime_directory_before_download(self):
        path = self.data / "runtimes"
        path.mkdir(parents=True)
        path.chmod(0o777)
        with self.assertRaisesRegex(installer.RuntimeInstallError, "Unsafe directory"):
            self._install()
        self.opener.open.assert_not_called()

    def test_host_selection_uses_only_pinned_assets(self):
        x64 = "0bc1f091e8916102b6d8f2cf944233a620e90a0f856c72d0ce41150c65c2e72e"
        arm64 = "dc7aa17a67884619b7223870aca9fd6addbc01717713b04a714add1c31137359"
        for architecture, size, suffix, digest in [
            ("x86_64", 23311084, "x64", x64),
            ("arm64", 18949862, "arm64", arm64),
            ("aarch64", 18949862, "arm64", arm64),
        ]:
            with self.subTest(architecture=architecture), patch.object(
                installer, "_download", side_effect=installer.RuntimeInstallError("stop"),
            ) as download:
                self.machine.return_value = architecture
                with self.assertRaisesRegex(installer.RuntimeInstallError, "stop"):
                    installer.install_cpu_runtime(self.data, self.policy)
                release = download.call_args.args[0]
                self.assertEqual(release.size, size)
                self.assertEqual(release.sha256, digest)
                self.assertEqual(release.url, "https://github.com/ggml-org/llama.cpp/releases/download/"
                                              f"b12359/llama-b12359-bin-ubuntu-{suffix}.tar.gz")
        self.machine.return_value = "riscv64"
        with self.assertRaisesRegex(installer.RuntimeInstallError, "only on Linux"):
            self._install()
        self.opener.open.assert_not_called()

    def test_root_and_setuid_callers_are_refused(self):
        self.user.side_effect = _REAL_USER_GUARD
        for uid, euid in [(0, 0), (1000, 0), (1000, 1001)]:
            with self.subTest(uid=uid, euid=euid), patch.object(installer.os, "getuid", return_value=uid), \
                    patch.object(installer.os, "geteuid", return_value=euid):
                with self.assertRaisesRegex(installer.RuntimeInstallError, "regular user"):
                    self._install()
        self.opener.open.assert_not_called()

    def test_redirects_must_stay_on_github_https(self):
        handler = installer._GitHubRedirects()
        req = Request(installer._RELEASES["x86_64"].url)
        good = "https://release-assets.githubusercontent.com/download"
        self.assertEqual(handler.redirect_request(req, None, 302, "", {}, good).full_url, good)
        for bad in [
            "http://github.com/archive", "https://github.com.example.org/archive",
            "https://user@github.com/archive", "https://github.com:8443/archive",
            "https://github.com:invalid/archive",
        ]:
            with self.subTest(url=bad), self.assertRaises(HTTPError) as error:
                handler.redirect_request(req, io.BytesIO(), 302, "", {}, bad)
            error.exception.close()
