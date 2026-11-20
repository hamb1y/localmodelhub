"""Install official LocalModelHub releases (also used by the public bootstrap)."""

import argparse
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import BytesParser
from urllib.parse import urljoin, urlsplit


REPO = "hamb1y/localmodelhub"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "localmodelhub.tar.gz"
CHECKSUM = f"{ASSET}.sha256"
ASSET_HOSTS = {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
VERSION = r"(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){2}"
MIB = 1024 * 1024
ARCHIVE_LIMIT = 128 * MIB
UNPACKED_LIMIT = 384 * MIB
MEMBER_LIMIT = 10_000


class UpdateError(Exception):
    pass


def version(value):
    if not isinstance(value, str) or not re.fullmatch(VERSION, value):
        raise UpdateError("expected a stable X.Y.Z version")
    return tuple(map(int, value.split(".")))


def latest_version(path):
    metadata = json.loads(path.read_bytes())
    if (
        not isinstance(metadata, dict)
        or metadata.get("draft") is not False
        or metadata.get("prerelease") is not False
    ):
        raise UpdateError("GitHub did not return a published stable release")
    tag = metadata.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError("expected a stable GitHub tag vX.Y.Z")
    version(tag[1:])
    return tag[1:]


def asset_url(release, name):
    version(release)
    if name not in (ASSET, CHECKSUM):
        raise UpdateError("unexpected release asset name")
    return f"https://github.com/{REPO}/releases/download/v{release}/{name}"


def copy_bounded(source, target, limit):
    total = 0
    while chunk := source.read(min(MIB, limit - total + 1)):
        total += len(chunk)
        if total > limit:
            raise UpdateError(f"payload exceeds {limit} bytes")
        target.write(chunk)


def asset_redirect(url, location):
    redirected = urljoin(url, location)
    parsed = urlsplit(redirected)
    if parsed.scheme != "https" or parsed.netloc not in ASSET_HOSTS:
        raise UpdateError("release asset redirected outside GitHub's HTTPS asset hosts")
    return redirected


def response_headers(stream):
    remaining = 64 * 1024
    while remaining:
        line = stream.readline(remaining + 1)
        match = re.fullmatch(rb"HTTP/\S+ ([1-5]\d{2})(?: [^\r\n]*)?\r\n", line)
        if not match or len(line) >= remaining:
            raise UpdateError("invalid or oversized GitHub HTTP response headers")
        remaining -= len(line)
        fields = bytearray()
        while True:
            field = stream.readline(remaining + 1)
            if not field or len(field) > remaining:
                raise UpdateError("invalid or oversized GitHub HTTP response headers")
            remaining -= len(field)
            if field == b"\r\n":
                break
            fields.extend(field)
        status = int(match[1])
        if status >= 200:
            return status, BytesParser().parsebytes(fields, headersonly=True)
    raise UpdateError("oversized GitHub HTTP response headers")


def download(url, output, limit, *, asset=False):
    """Follow only known release redirects, with a byte cap even on older curl."""
    for _ in range(3 if asset else 1):
        command = [
            "curl", "--disable", "--globoff", "--proto", "=https", "--fail", "--silent",
            "--show-error", "--connect-timeout", "10", "--max-time",
            "180" if limit == ARCHIVE_LIMIT else "30", "--max-filesize", str(limit),
            "--suppress-connect-headers", "--include",
            "--user-agent", "LocalModelHub-Updater", "--url", url,
        ]
        with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
            try:
                status, headers = response_headers(process.stdout)
                with output.open("wb") as target:
                    copy_bounded(process.stdout, target, limit)
            finally:
                # Do not wait on a live producer if copying raised (e.g. size cap).
                process.stdout.close()
            if process.wait():
                raise UpdateError("GitHub download failed")
        if status == 200:
            return
        if asset and status in (301, 302, 303, 307, 308):
            locations = headers.get_all("Location", [])
            if len(locations) == 1:
                url = asset_redirect(url, locations[0])
                continue
        raise UpdateError("unexpected GitHub HTTP response")
    raise UpdateError("too many release asset redirects")


def verify_checksum(checksum, archive):
    line = checksum.read_bytes()
    match = re.fullmatch(rb"([a-fA-F0-9]{64}) [ *]localmodelhub\.tar\.gz\r?\n?", line)
    if not match:
        raise UpdateError("invalid release checksum file")
    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest().encode()
    if actual != match[1].lower():
        raise UpdateError("release archive SHA-256 mismatch")


def extract_archive(archive, scratch):
    """Extract plain directories/files below the fixed bundle root, never links."""
    unpacked = scratch / "archive.tar"
    with gzip.open(archive, "rb") as source, unpacked.open("xb") as target:
        copy_bounded(source, target, UNPACKED_LIMIT)
    root = scratch / "localmodelhub"
    seen = set()
    with tarfile.open(unpacked, "r:") as bundle:
        for member in bundle:
            parts = member.name.split("/")
            if (
                len(seen) >= MEMBER_LIMIT
                or len(member.name) > 2048
                or len(parts) > 64
                or parts[0] != root.name
                or any(part in ("", ".", "..") or "\\" in part for part in parts)
                or member.name in seen
                or member.type not in (tarfile.DIRTYPE, tarfile.REGTYPE, tarfile.AREGTYPE)
                or member.size < 0
                or member.sparse is not None
                or (len(parts) == 1 and not member.isdir())
            ):
                raise UpdateError("unsafe or unsupported release archive member")
            seen.add(member.name)
            destination = scratch.joinpath(*parts)
            if member.isdir():
                if member.size != 0:
                    raise UpdateError("invalid directory in release archive")
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, destination.open("xb") as target:
                    copy_bounded(source, target, UNPACKED_LIMIT)
    unpacked.unlink()
    return root


def verify_bundle(root, release):
    if not (root / "deploy" / "install.sh").is_file():
        raise UpdateError("release archive is missing deploy/install.sh")
    wheels = list((root / "wheels").glob("localmodelhub-*.whl"))
    if len(wheels) != 1 or not wheels[0].name.startswith(f"localmodelhub-{release}-"):
        raise UpdateError("release tag does not match the LocalModelHub wheel filename")
    metadata_name = f"localmodelhub-{release}.dist-info/METADATA"
    with zipfile.ZipFile(wheels[0]) as wheel:
        entries = [entry for entry in wheel.infolist() if entry.filename == metadata_name]
        if len(entries) != 1 or entries[0].file_size > MIB:
            raise UpdateError("missing, ambiguous, or oversized LocalModelHub wheel metadata")
        with wheel.open(entries[0]) as stream:
            metadata = BytesParser().parsebytes(stream.read(MIB + 1), headersonly=True)
    if metadata.get_all("Name") != ["localmodelhub"] or metadata.get_all("Version") != [release]:
        raise UpdateError("release tag does not match the LocalModelHub wheel metadata")


def installed_version(data):
    current = data / "current"
    if not os.path.lexists(current):
        return None
    python = current / "venv" / "bin" / "python"
    if not python.is_file():
        raise UpdateError("cannot determine installed version: current release has no Python")
    result = subprocess.run(
        [str(python), "-I", "-c",
         'from importlib.metadata import version; print(version("localmodelhub"))'],
        check=True, capture_output=True, text=True, timeout=15,
    )
    release = result.stdout.strip()
    version(release)
    return release


def update(data, *, bootstrap=False):
    if bootstrap:
        data.mkdir(parents=True, exist_ok=True)
    if not data.is_dir():
        raise UpdateError("LocalModelHub is not installed; run the public bootstrap first")
    with (data / "update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another LocalModelHub bootstrap or update is already running.")
            return
        current = installed_version(data)
        if current is None and not bootstrap:
            raise UpdateError("LocalModelHub is not installed; run the public bootstrap first")
        with tempfile.TemporaryDirectory(prefix=".update.", dir=data) as temp:
            scratch = Path(temp)
            metadata = scratch / "release.json"
            download(LATEST, metadata, MIB)
            release = latest_version(metadata)
            if current is not None and version(release) <= version(current):
                print(f"Keeping LocalModelHub {current}; latest stable is {release}.")
                return
            checksum = scratch / CHECKSUM
            archive = scratch / ASSET
            download(asset_url(release, CHECKSUM), checksum, 4096, asset=True)
            download(asset_url(release, ASSET), archive, ARCHIVE_LIMIT, asset=True)
            verify_checksum(checksum, archive)
            root = extract_archive(archive, scratch)
            verify_bundle(root, release)
            # Catch another manual install that happened during the download.
            if installed_version(data) != current:
                raise UpdateError("current release changed during download; retry the update")
            result = subprocess.run(["bash", str(root / "deploy" / "install.sh")], cwd=root)
            if result.returncode:
                try:
                    after = installed_version(data) or "none"
                except (UpdateError, OSError, subprocess.SubprocessError):
                    after = "unknown"
                raise UpdateError(
                    f"installer exited with status {result.returncode}; current version: {after} "
                    "(check localmodelhub.service status before retrying)"
                )
            if installed_version(data) != release:
                raise UpdateError("installer succeeded but the expected release is not current")
            print(f"LocalModelHub {release} is installed.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install a newer official stable LocalModelHub release.")
    parser.add_argument("--bootstrap", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        account = pwd.getpwuid(os.geteuid())
        if account.pw_name != "ai" or account.pw_uid == 0:
            raise UpdateError("run as the dedicated ai account, not root")
        home = Path(account.pw_dir)
        if not home.is_absolute() or home == Path("/") or Path.home() != home:
            raise UpdateError("HOME must match the ai account's registered home directory")
        os.umask(0o077)
        update(home / ".local" / "share" / "localmodelhub", bootstrap=args.bootstrap)
    except (
        UpdateError, OSError, ValueError, KeyError, subprocess.SubprocessError,
        tarfile.TarError, zipfile.BadZipFile, EOFError,
    ) as exc:
        print(f"LocalModelHub update: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
