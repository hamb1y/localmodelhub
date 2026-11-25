"""Provision rootless Podman and approve the pinned starter container image.

Every managed engine — the typed adapters and the generic ``container``
adapter — runs in a digest-pinned Podman image. Onboarding verifies that Podman
works for the service account, pulls the pinned starter image, and records that
image as the local runtime approval. No image is built here.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .podman import Podman, PodmanError, image_reference

_STARTER_IMAGE = (
    "ghcr.io/ggml-org/llama.cpp:server@"
    "sha256:b0cd5490eefff30b40dcd47f7249a7d7211136908f73fe440a2a86b35a22b23b"
)
_RUNTIME_ID = "llama-cpu"
_ADAPTER = "llama.cpp"


class RuntimeInstallError(ValueError):
    """The container runtime or its pinned image could not be provisioned."""


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


def _private_parent(path: Path) -> None:
    """Refuse directories where other users can replace the policy file."""
    uid = os.geteuid()
    for parent in (path, *path.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, uid}
                or (info.st_mode & 0o022 and
                    (parent == path or info.st_uid != 0 or not info.st_mode & stat.S_ISVTX))):
            raise RuntimeInstallError(f"Unsafe directory for local policy: {parent}")
    if path.stat().st_uid != uid:
        raise RuntimeInstallError(f"Local policy directory must be owned by the user: {path}")


@contextmanager
def _directory_lock(path: Path):
    import fcntl

    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _approve(path: Path, image: str) -> None:
    with _directory_lock(path.parent):
        policy = _read_policy(path)
        policy["runtimes"][_RUNTIME_ID] = {"adapter": _ADAPTER, "image": image}
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
        raise RuntimeInstallError("Provision the container runtime as your regular user, not root")


def install_cpu_runtime(data_dir: Path, policy_path: Path, *, podman: Podman | None = None) -> str:
    """Verify rootless Podman, pull the pinned starter image and approve it.

    Only this explicit call contacts a registry, and only for the exact digest
    pinned in the release. It never starts a server or changes the catalog.
    ``data_dir`` is the service data directory; Podman keeps image storage in
    the service account's own XDG data directory.
    """
    _require_unprivileged_user()
    Path(data_dir).expanduser().mkdir(parents=True, exist_ok=True, mode=0o700)
    policy_path = policy_path.expanduser()
    policy_path = policy_path.parent.resolve() / policy_path.name
    policy_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_parent(policy_path.parent)
    _read_policy(policy_path)
    try:
        image = image_reference(_STARTER_IMAGE)
        client = podman or Podman()
        client.available()
        client.ensure(image)
    except (PodmanError, ValueError) as exc:
        raise RuntimeInstallError(str(exc)) from None
    _approve(policy_path, image)
    return image
