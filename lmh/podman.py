"""Rootless Podman images and command-line helpers for managed inference engines.

The controller approves one digest-pinned image per runtime in local policy.
Engines never run on the host: launches are wrapped in ``podman run`` and the
manifest digest in the reference is what the local image must match.
"""

from __future__ import annotations

import re
import shutil
import subprocess

# repository[:tag] with an optional registry, followed by an optional
# sha256 manifest digest. Tags are deliberately case-sensitive; repositories
# and registries are not.
_COMPONENT = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
_IMAGE = re.compile(
    rf"(?:{_COMPONENT}(?::[0-9]+)?/)?"
    rf"{_COMPONENT}(?:/{_COMPONENT})*"
    rf"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}})?"
    rf"(?:@sha256:[0-9a-f]{{64}})?"
)
_DIGEST = re.compile(r"@sha256:([0-9a-f]{64})$")
_PULL_TIMEOUT = 60 * 60
_PROBE_TIMEOUT = 20


class PodmanError(RuntimeError):
    """Podman is unavailable, unusable, or could not provide the image."""


def image_reference(value: object, *, require_digest: bool = True) -> str:
    """Validate an OCI image reference and return it unchanged.

    A digest pin is required for managed images so that a mutable tag cannot
    silently change what the service executes.
    """
    if not isinstance(value, str) or not value or len(value) > 512 or not _IMAGE.fullmatch(value):
        raise ValueError("image must be a valid OCI image reference")
    if require_digest and not _DIGEST.search(value):
        raise ValueError("image must be pinned by an @sha256:<64 hex> digest")
    return value


def image_digest(value: str) -> str | None:
    """Return the hex digest pinned in an image reference, if any."""
    match = _DIGEST.search(value)
    return match[1] if match else None


def image_name(value: str) -> str:
    """Return the image reference without its digest, for display."""
    return value.split("@", 1)[0]


class Podman:
    """Small, shell-free wrapper around the rootless Podman CLI."""

    def __init__(self, executable: str | None = None):
        self.executable = executable or shutil.which("podman") or "podman"

    def _call(self, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.executable, *args], timeout=timeout, capture_output=True, text=True,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise PodmanError(
                "Podman is required to run models; install podman and enable rootless use for this account"
            ) from None
        except subprocess.TimeoutExpired:
            raise PodmanError(f"podman {args[0]} timed out") from None
        except OSError as exc:
            raise PodmanError(f"Podman could not be executed: {exc.strerror or exc}") from None

    def available(self) -> None:
        """Fail early with an actionable message when rootless Podman is unusable."""
        result = self._call(["info", "--format", "{{.Host.OCIRuntime.Name}}"], _PROBE_TIMEOUT)
        if result.returncode:
            raise PodmanError(
                "Rootless Podman is not usable for this account; verify podman, "
                "/etc/subuid and /etc/subgid, and XDG_RUNTIME_DIR"
            )

    def present(self, reference: str) -> bool:
        result = self._call(["image", "exists", reference], _PROBE_TIMEOUT)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise PodmanError("Podman could not inspect the local image store")

    def ensure(self, reference: str, progress=lambda _: None) -> str:
        """Make the approved image available locally, pulling only when missing.

        Pulling is bounded to the exact reference from local policy. A digest
        reference cannot be redirected to different content by a later tag move.
        """
        reference = image_reference(reference)
        if self.present(reference):
            return reference
        progress(f"Pulling container image {image_name(reference)}")
        result = self._call(["pull", reference], _PULL_TIMEOUT)
        if result.returncode:
            # Registry errors can echo credentials embedded in a URL.
            raise PodmanError(
                f"Could not pull container image {image_name(reference)}; "
                "check the pinned digest, registry access and network"
            )
        if not self.present(reference):
            raise PodmanError(f"Podman did not retain the pinned image {image_name(reference)}")
        return reference
