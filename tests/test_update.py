import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from lmh import update


def member(name, data=b"", kind=tarfile.REGTYPE):
    info = tarfile.TarInfo(name)
    info.type = kind
    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        info.linkname = data.decode()
        return info, None
    info.size = len(data)
    return info, io.BytesIO(data)


def make_bundle(path, *entries):
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(*member("localmodelhub", kind=tarfile.DIRTYPE))
        for entry in entries:
            archive.addfile(*entry)


def make_wheel(filename_version, metadata_version):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as wheel:
        wheel.writestr(
            f"localmodelhub-{filename_version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: localmodelhub\nVersion: {metadata_version}\n\n",
        )
    return stream.getvalue()


class UpdateTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.data = self.root / "installed"
        self.data.mkdir()
        self.release = self.root / "archive.tar.gz"

    def build_release(self, metadata_version="1.2.3"):
        make_bundle(
            self.release,
            member("localmodelhub/deploy/install.sh", b"exit 0\n"),
            member(
                "localmodelhub/wheels/localmodelhub-1.2.3-py3-none-any.whl",
                make_wheel("1.2.3", metadata_version),
            ),
        )

    def download(self, url, output, limit, *, asset=False):
        if url == update.LATEST and not asset:
            output.write_text(json.dumps({
                "tag_name": "v1.2.3", "draft": False, "prerelease": False,
            }))
            return
        self.assertTrue(asset)
        payload = self.release.read_bytes()
        if url == update.asset_url("1.2.3", update.CHECKSUM):
            output.write_text(f"{hashlib.sha256(payload).hexdigest()}  {update.ASSET}\n")
        elif url == update.asset_url("1.2.3", update.ASSET):
            output.write_bytes(payload)
        else:
            self.fail(f"unexpected download: {url}")

    def test_published_stable_versions_only(self):
        path = self.root / "metadata"
        for overrides in (
            {"draft": True}, {"prerelease": True}, {"tag_name": "v1.2.3-rc1"},
            {"tag_name": "v01.2.3"}, {"tag_name": "../v1.2.3"}, {"tag_name": "1.2.3"},
        ):
            with self.subTest(overrides=overrides):
                metadata = {"tag_name": "v1.2.3", "draft": False, "prerelease": False}
                path.write_text(json.dumps(metadata | overrides))
                with self.assertRaises(update.UpdateError):
                    update.latest_version(path)

    def test_same_or_older_stable_does_not_fetch_assets(self):
        for installed in ("1.2.3", "1.10.0"):
            with (
                self.subTest(installed=installed),
                patch.object(update, "installed_version", return_value=installed),
                patch.object(update, "download", side_effect=self.download) as fetch,
            ):
                update.update(self.data)
                self.assertEqual([call.args[0] for call in fetch.call_args_list], [update.LATEST])

    def test_update_and_bootstrap_install_verified_release(self):
        self.build_release()
        for initial in ("1.2.2", None):
            with (
                self.subTest(initial=initial),
                patch.object(update, "installed_version", side_effect=[initial, initial, "1.2.3"]),
                patch.object(update, "download", side_effect=self.download),
                patch.object(update.subprocess, "run") as run,
            ):
                run.return_value.returncode = 0
                update.update(self.data, bootstrap=initial is None)
                run.assert_called_once()
                command = run.call_args.args[0]
                self.assertEqual(command[0], "bash")
                self.assertEqual(Path(command[1]).relative_to(run.call_args.kwargs["cwd"]),
                                 Path("deploy/install.sh"))
                self.assertEqual(list(self.data.glob(".update.*")), [])

    def test_app_updater_does_not_bootstrap_missing_install(self):
        with patch.object(update, "download") as fetch:
            with self.assertRaisesRegex(update.UpdateError, "not installed"):
                update.update(self.data)
            fetch.assert_not_called()

    def test_checksum_failure_does_not_invoke_installer(self):
        self.build_release()

        def corrupted(url, output, limit, *, asset=False):
            self.download(url, output, limit, asset=asset)
            if output.name == update.ASSET:
                output.write_bytes(output.read_bytes() + b"unverified")

        with (
            patch.object(update, "installed_version", return_value="1.2.2"),
            patch.object(update, "download", side_effect=corrupted),
            patch.object(update.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(update.UpdateError, "SHA-256 mismatch"):
                update.update(self.data)
            run.assert_not_called()

    def test_install_failure_reports_current_version(self):
        self.build_release()
        with (
            patch.object(update, "installed_version", return_value="1.2.2"),
            patch.object(update, "download", side_effect=self.download),
            patch.object(update.subprocess, "run") as run,
        ):
            run.return_value.returncode = 17
            with self.assertRaisesRegex(update.UpdateError, "status 17; current version: 1.2.2"):
                update.update(self.data)

    def test_rejects_archive_traversal_links_special_files_and_duplicates(self):
        for unsafe in (
            member("localmodelhub/../../escaped", b"bad"),
            member("/localmodelhub/escaped", b"bad"),
            member("localmodelhub/link", b"../../escaped", tarfile.SYMTYPE),
            member("localmodelhub/link", b"../../escaped", tarfile.LNKTYPE),
            member("localmodelhub/pipe", kind=tarfile.FIFOTYPE),
            member("localmodelhub", kind=tarfile.DIRTYPE),
        ):
            with self.subTest(name=unsafe[0].name, kind=unsafe[0].type):
                make_bundle(self.release, unsafe)
                with tempfile.TemporaryDirectory(dir=self.root) as scratch:
                    with self.assertRaisesRegex(update.UpdateError, "unsafe"):
                        update.extract_archive(self.release, Path(scratch))
                self.assertFalse((self.root / "escaped").exists())

    def test_rejects_excessive_expansion(self):
        self.build_release()
        with patch.object(update, "UNPACKED_LIMIT", 512):
            with self.assertRaisesRegex(update.UpdateError, "exceeds"):
                update.extract_archive(self.release, self.root)

    def test_rejects_wheel_metadata_that_disagrees_with_tag(self):
        self.build_release(metadata_version="1.2.2")
        bundle = update.extract_archive(self.release, self.root)
        with self.assertRaisesRegex(update.UpdateError, "wheel metadata"):
            update.verify_bundle(bundle, "1.2.3")

    def test_download_follows_only_official_asset_hosts(self):
        origin = update.asset_url("1.2.3", update.ASSET)
        output = self.root / update.ASSET
        official = "https://release-assets.githubusercontent.com/path?signed=true"
        for redirect in (official, "http://release-assets.githubusercontent.com/path",
                         "https://release-assets.githubusercontent.com@elsewhere.example/path",
                         "https://elsewhere.example/path"):
            with self.subTest(redirect=redirect):
                urls = []

                def curl(command, **kwargs):
                    urls.append(command[-1])
                    if command[-1] == origin:
                        headers = f"HTTP/2 302\r\nlocation: {redirect}\r\n\r\n".encode()
                    else:
                        headers = b"HTTP/2 200\r\ncontent-length: 4\r\n\r\n"
                    process = MagicMock()
                    process.__enter__.return_value = process
                    process.stdout = io.BytesIO(headers + b"data")
                    process.wait.return_value = 0
                    self.assertEqual(kwargs["stdout"], subprocess.PIPE)
                    return process

                with patch.object(update.subprocess, "Popen", side_effect=curl):
                    if redirect == official:
                        update.download(origin, output, 32, asset=True)
                        self.assertEqual(urls, [origin, official])
                        self.assertEqual(output.read_bytes(), b"data")
                    else:
                        with self.assertRaisesRegex(update.UpdateError, "HTTPS asset hosts"):
                            update.download(origin, output, 32, asset=True)
                        self.assertEqual(urls, [origin])

    def test_header_reader_handles_informational_responses_with_a_size_limit(self):
        status, headers = update.response_headers(io.BytesIO(
            b"HTTP/2 103 \r\nlink: </style>\r\n\r\nHTTP/2 200 \r\nx-version: 1\r\n\r\n"
        ))
        self.assertEqual((status, headers["x-version"]), (200, "1"))
        with self.assertRaisesRegex(update.UpdateError, "oversized"):
            update.response_headers(io.BytesIO(b"HTTP/2 200 \r\nx: " + b"a" * 65536))

    def test_bootstrap_pins_the_exact_updater_source(self):
        project = Path(__file__).resolve().parents[1]
        bootstrap = (project / "install.sh").read_text()
        match = re.search(r"^updater_sha256=([a-f0-9]{64})$", bootstrap, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match[1], hashlib.sha256((project / "lmh" / "update.py").read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
