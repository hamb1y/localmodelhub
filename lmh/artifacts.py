"""Resolve and verify local files and immutable Hugging Face model artifacts."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from collections import OrderedDict
from pathlib import Path
from threading import Lock

from .catalog import CatalogError, _validate_artifact


class ArtifactError(RuntimeError):
    """The artifact is invalid, unavailable, or failed integrity checks."""


class ArtifactUnavailable(ArtifactError):
    """The pinned file is not available in the local Hugging Face cache."""


_FileIdentity = tuple[int, int, int, int, int]
_VerificationKey = tuple[_FileIdentity, tuple[tuple[str, str], ...]]
_VERIFICATION_CACHE_LIMIT = 1024


def _identity(info: os.stat_result) -> _FileIdentity:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class ArtifactStore:
    """Keep HF cache data under root; local sources stay in place."""

    def __init__(self, root: Path, token: str | None = None):
        self.root = Path(root)
        self._token = token
        self._verified: OrderedDict[_VerificationKey, None] = OrderedDict()
        self._verification_lock = Lock()

    def resolve(self, artifact: dict, download: bool = False, *, force_verify: bool = False) -> Path:
        """Return a readable file matching all declared constraints.

        Set force_verify=True to stream the declared hashes even if a successful
        verification is cached for this file identity.
        HF resolution is cache-only unless download=True. Local paths are
        trusted only after controller approval; this method does not approve them.
        """
        try:
            artifact = _validate_artifact(artifact)
        except CatalogError as exc:
            raise ArtifactError(str(exc)) from None

        source = artifact["source"]
        path = (
            Path(source["path"]) if source["kind"] == "local"
            else self._resolve_hf(source, download)
        )
        self._verify(path, artifact, force_verify)
        return path

    def _resolve_hf(self, source: dict, download: bool) -> Path:
        try:
            from huggingface_hub import hf_hub_download
        except ImportError:
            raise ArtifactError("Hugging Face artifacts require the huggingface_hub package") from None

        try:
            # The hub cache retains incomplete transfers and resumes them by
            # default. Passing neither force_download nor local_dir preserves it.
            return Path(hf_hub_download(
                repo_id=source["repo"], revision=source["revision"], filename=source["file"],
                repo_type="model", cache_dir=self.root, local_files_only=not download,
                # None uses the Hub's normal login/environment lookup (and
                # respects HF_HUB_DISABLE_IMPLICIT_TOKEN). Keep cache-only offline.
                token=self._token if download else False,
                endpoint="https://huggingface.co",
            ))
        except Exception:
            # Hub errors can contain signed URLs, request headers, and paths.
            if not download:
                raise ArtifactUnavailable(
                    "pinned Hugging Face file is unavailable in the local cache; enable downloads to fetch it"
                ) from None
            raise ArtifactError(
                "Hugging Face download failed; check the repository, pinned commit, file, access token and network"
            ) from None

    @staticmethod
    def _check_unchanged(path: Path, identity: _FileIdentity) -> None:
        try:
            current = _identity(path.stat())
        except FileNotFoundError:
            raise ArtifactError("artifact changed during verification") from None
        if identity != current:
            raise ArtifactError("artifact changed during verification")

    def _verify(self, path: Path, artifact: dict, force_verify: bool) -> None:
        size = artifact.get("size")
        hashes = artifact.get("hashes", {})
        try:
            # Nonblocking open prevents a FIFO from blocking the verification
            # before fstat can reject it. Follow HF snapshot and local symlinks.
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(path, flags)
            try:
                handle = os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
            with handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ArtifactError("artifact path does not point to a regular file")
                if size is not None and before.st_size != size:
                    raise ArtifactError(f"artifact size mismatch: expected {size} bytes, got {before.st_size}")
                identity = _identity(before)
                if not hashes:
                    self._check_unchanged(path, identity)
                    return
                key = (identity, tuple(sorted(hashes.items())))
                with self._verification_lock:
                    if force_verify:
                        self._verified.pop(key, None)
                    cached = key in self._verified
                    if cached:
                        self._verified.move_to_end(key)
                if cached:
                    self._check_unchanged(path, identity)
                    return

                digests = {}
                if "sha256" in hashes:
                    digests["sha256"] = hashlib.sha256()
                if "blake3" in hashes:
                    try:
                        from blake3 import blake3
                    except ImportError:
                        raise ArtifactError("BLAKE3 verification requires the blake3 package") from None
                    digests["blake3"] = blake3()
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    for digest in digests.values():
                        digest.update(chunk)
                if identity != _identity(os.fstat(handle.fileno())):
                    raise ArtifactError("artifact changed during verification")
                for algorithm, digest in digests.items():
                    if not hmac.compare_digest(digest.hexdigest(), hashes[algorithm]):
                        raise ArtifactError(f"artifact {algorithm} checksum mismatch")
                # The descriptor may still point to the original inode if the
                # pathname or symlink was replaced while we read.
                self._check_unchanged(path, identity)
                with self._verification_lock:
                    self._verified[key] = None
                    self._verified.move_to_end(key)
                    if len(self._verified) > _VERIFICATION_CACHE_LIMIT:
                        self._verified.popitem(last=False)
        except FileNotFoundError:
            raise ArtifactError("artifact file does not exist") from None
        except (OSError, ValueError):
            raise ArtifactError("artifact file cannot be read as a regular file") from None
