"""Opt-in installation of the upstream llama.cpp CPU runtime."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
from zlib import error as ZlibError


class RuntimeInstallError(ValueError):
    """The requested CPU runtime could not be verified or installed."""


_VERSION = "b12359"
_RELEASE_DIR = f"llama-{_VERSION}"
_EXECUTABLE = "llama-server"
_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{_VERSION}"
_VERSION_LINE = rb"^version: [^\r\n]+ \(build 12359, commit b1b770d90\)\r?$"
_MAX_EXPANDED = 128 * 1024 * 1024
_MAX_MEMBERS = 256
_CHUNK = 128 * 1024


@dataclass(frozen=True)
class _Release:
    asset: str
    size: int
    sha256: str

    @property
    def url(self) -> str:
        return f"{_URL}/{_RELEASE_DIR}-bin-ubuntu-{self.asset}.tar.gz"


_RELEASES = {
    "x86_64": _Release("x64", 23311084,
                       "0bc1f091e8916102b6d8f2cf944233a620e90a0f856c72d0ce41150c65c2e72e"),
    "aarch64": _Release("arm64", 18949862,
                        "dc7aa17a67884619b7223870aca9fd6addbc01717713b04a714add1c31137359"),
}


class _GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            target = urlsplit(newurl)
            approved = (
                target.scheme == "https" and target.hostname in {
                    "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
                } and target.port in (None, 443) and target.username is None
            )
        except ValueError:
            approved = False
        if not approved:
            raise HTTPError(req.full_url, code, "unapproved release redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(release: _Release, path: Path) -> None:
    size = 0
    digest = hashlib.sha256()
    deadline = time.monotonic() + 120
    try:
        with build_opener(_GitHubRedirects()).open(release.url, timeout=15) as response, \
                path.open("xb") as out:
            while True:
                if time.monotonic() > deadline:
                    raise RuntimeInstallError("CPU runtime download timed out")
                chunk = response.read1(min(_CHUNK, release.size - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > release.size:
                    raise RuntimeInstallError("CPU runtime archive size mismatch")
                digest.update(chunk)
                out.write(chunk)
    except (OSError, HTTPException) as exc:
        if isinstance(exc, HTTPError):
            exc.close()
        raise RuntimeInstallError("Could not download the approved CPU runtime from GitHub") from None
    if size != release.size:
        raise RuntimeInstallError("CPU runtime archive size mismatch")
    if digest.hexdigest() != release.sha256:
        raise RuntimeInstallError("CPU runtime archive SHA-256 mismatch")


class _BoundedTar:
    """Limit decompressed bytes, including tar headers, padding and metadata."""

    def __init__(self, stream):
        self.stream = stream
        self.remaining = _MAX_EXPANDED

    def read(self, size: int) -> bytes:
        chunk = self.stream.read(min(size, self.remaining + 1))
        self.remaining -= len(chunk)
        if self.remaining < 0:
            raise RuntimeInstallError("Expanded CPU runtime archive exceeds size limit")
        return chunk


def _extract(archive: Path, destination: Path) -> Path:
    links: list[Path] = []

    def checked_members(package: tarfile.TarFile):
        members: set[str] = set()
        total_size = 0
        # The pinned packages have one flat release directory, with some
        # soname symlinks pointing at sibling libraries. Accept no hard links.
        for member in package:
            name = PurePosixPath(member.name)
            if (not name.parts or name.parts[0] != _RELEASE_DIR or len(name.parts) > 2
                    or ".." in name.parts or name.as_posix() in members
                    or (len(name.parts) == 1 and not member.isdir())
                    or (len(name.parts) == 2 and not (member.isreg() or member.issym()))
                    or member.sparse is not None):
                raise RuntimeInstallError("Unexpected path or entry in CPU runtime archive")
            members.add(name.as_posix())
            if member.size < 0 or (not member.isreg() and member.size != 0):
                raise RuntimeInstallError("Invalid entry size in CPU runtime archive")
            total_size += member.size
            if total_size > _MAX_EXPANDED or len(members) > _MAX_MEMBERS:
                raise RuntimeInstallError("Expanded CPU runtime archive exceeds limits")
            if member.issym():
                target = PurePosixPath(member.linkname)
                if len(target.parts) != 1 or target.is_absolute() or target.name == "..":
                    raise RuntimeInstallError("Unsafe symlink in CPU runtime archive")
                links.append(destination / name)
            yield member

    def filter_member(member: tarfile.TarInfo, root: str) -> tarfile.TarInfo:
        filtered = tarfile.data_filter(member, root)
        if member.isreg():
            filtered = filtered.replace(mode=0o600 | (filtered.mode & stat.S_IXUSR))
        return filtered

    if not hasattr(tarfile, "data_filter"):
        raise RuntimeInstallError("CPU runtime installation requires Python's tar data filter")
    try:
        with gzip.open(archive, "rb") as stream, \
                tarfile.open(fileobj=_BoundedTar(stream), mode="r|", errorlevel=2) as package:
            package.extractall(destination, members=checked_members(package), filter=filter_member)
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, ZlibError) as exc:
        raise RuntimeInstallError("Invalid or unsafe CPU runtime archive") from exc

    release_dir = destination / _RELEASE_DIR
    executable = release_dir / _EXECUTABLE
    try:
        mode = executable.lstat().st_mode
    except FileNotFoundError:
        raise RuntimeInstallError("Missing llama-server in CPU runtime archive") from None
    if not stat.S_ISREG(mode) or not mode & stat.S_IXUSR:
        raise RuntimeInstallError("llama-server must be a regular executable file")
    for link in links:
        try:
            resolved = link.resolve(strict=True)
        except (RuntimeError, OSError, ValueError):
            raise RuntimeInstallError("Unresolved symlink in CPU runtime archive") from None
        if resolved.parent != release_dir or not resolved.is_file():
            raise RuntimeInstallError("Unsafe symlink target in CPU runtime archive")
    release_dir.chmod(0o700)
    return executable


def _check_version(executable: Path) -> None:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            cwd=executable.parent,
            env={"PATH": os.defpath, "LC_ALL": "C", "CUDA_VISIBLE_DEVICES": ""},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=15, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeInstallError("llama-server --version failed on this host") from None
    if not re.search(_VERSION_LINE, result.stdout, re.MULTILINE):
        raise RuntimeInstallError(f"llama-server --version did not report the approved {_VERSION} build")


def _digest(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _matches_installation(staged: Path, installed: Path) -> bool:
    """Never run or approve a pre-existing runtime based on its path alone."""
    info = installed.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        return False
    if {p.name for p in staged.iterdir()} != {p.name for p in installed.iterdir()}:
        return False
    for source in staged.iterdir():
        target = installed / source.name
        left, right = source.lstat(), target.lstat()
        if left.st_mode != right.st_mode or right.st_uid != os.geteuid():
            return False
        if source.is_symlink():
            if source.readlink() != target.readlink():
                return False
        elif right.st_nlink != 1 or left.st_size != right.st_size or _digest(source) != _digest(target):
            return False
    return True


def _private_parent(path: Path) -> None:
    """Refuse directories where other users can replace installed files."""
    uid = os.geteuid()
    for parent in (path, *path.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, uid}
                or (info.st_mode & 0o022 and
                    (parent == path or info.st_uid != 0 or not info.st_mode & stat.S_ISVTX))):
            raise RuntimeInstallError(f"Unsafe directory for CPU runtime installation: {parent}")
    if path.stat().st_uid != uid:
        raise RuntimeInstallError(f"CPU runtime installation directory must be owned by the user: {path}")


@contextmanager
def _directory_lock(path: Path):
    import fcntl

    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_policy(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"runtimes": {}}
    except OSError:
        raise RuntimeInstallError("Cannot securely open the local policy file") from None
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        info = os.fstat(f.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or info.st_nlink != 1):
            raise RuntimeInstallError("Local policy must be a user-owned regular file")
        policy = json.load(f)
    if not isinstance(policy, dict) or not isinstance(policy.setdefault("runtimes", {}), dict):
        raise RuntimeInstallError("Local policy and its runtimes field must be JSON objects")
    return policy


def _approve(path: Path, executable: Path, digest: str) -> None:
    with _directory_lock(path.parent):
        policy = _read_policy(path)
        policy["runtimes"]["llama-cpu"] = {
            "adapter": "llama.cpp", "version": _VERSION, "executable": str(executable),
            "sha256": digest,
        }
        with tempfile.TemporaryDirectory(prefix=".policy-", dir=path.parent) as stage:
            staged = Path(stage) / "policy.json"
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump(policy, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            staged.replace(path)


def _require_unprivileged_user() -> None:
    if not os.getuid() or os.getuid() != os.geteuid():
        raise RuntimeInstallError("Install the CPU runtime as your regular user, not root")


def install_cpu_runtime(data_dir: Path, policy_path: Path) -> Path:
    """Verify and install the pinned Linux CPU runtime; return its absolute path.

    Only this explicit call downloads the release and runs its ``--version``
    probe. It never starts a server. A different existing installation is left
    untouched; repeat installs re-verify the release before any policy approval.
    """
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    release = _RELEASES.get(machine)
    if platform.system() != "Linux" or release is None:
        raise RuntimeInstallError("Pinned CPU runtimes are available only on Linux x86_64 and arm64")
    _require_unprivileged_user()

    data_dir = data_dir.expanduser().resolve()
    runtime_dir = data_dir / "runtimes"
    policy_path = policy_path.expanduser()
    policy_path = policy_path.parent.resolve() / policy_path.name
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    policy_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_parent(runtime_dir)
    _private_parent(policy_path.parent)
    _read_policy(policy_path)

    installed = runtime_dir / f"{_RELEASE_DIR}-{release.asset}"
    executable = installed / _EXECUTABLE
    with tempfile.TemporaryDirectory(prefix=".cpu-runtime-", dir=runtime_dir) as temp:
        stage = Path(temp)
        archive = stage / "release.tar.gz"
        _download(release, archive)
        staged_executable = _extract(archive, stage)
        _check_version(staged_executable)
        digest = _digest(staged_executable)
        with _directory_lock(runtime_dir):
            if os.path.lexists(installed):
                if not _matches_installation(staged_executable.parent, installed):
                    raise RuntimeInstallError(f"Conflicting CPU runtime already exists: {installed}")
            else:
                staged_executable.parent.rename(installed)
    _approve(policy_path, executable, digest)
    return executable
