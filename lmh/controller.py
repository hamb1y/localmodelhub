"""One boot reconciliation; explicit lifecycle operations thereafter."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import ipaddress
import json
import os
import queue
import socket
import sqlite3
import tempfile
import threading
import time
from http.client import HTTPException
from pathlib import Path

from .artifacts import ArtifactStore, ArtifactUnavailable
from .catalog import CatalogError, load_catalog, validate_catalog
from .engines import CONTAINER_ADAPTERS, LLAMA_ENGINE_PORT, build_launch
from .hardware import snapshot
from .podman import Podman, image_digest, image_reference
from .state import State
from .supervisor import Supervisor
from .telemetry import Telemetry

MIB = 1024 ** 2
ACTIVE = {"running", "starting", "stopping", "ready", "unhealthy"}
_PROBE_GPU = "GPU-00000000-0000-0000-0000-000000000000"
_PROBE_IMAGE = "localhost/localmodelhub-probe@sha256:" + "0" * 64


def _catalog_digest(config: dict) -> str | None:
    try:
        from blake3 import blake3
    except ImportError:
        return None
    canonical = json.dumps(config, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()
    return blake3(canonical).hexdigest()


def _check_config(config: dict, *, portable: bool = False) -> dict:
    """Validate adapter options without downloading, hashing, or executing runtimes."""
    for name, runtime in config["runtimes"].items():
        adapter = runtime["adapter"]
        if adapter not in CONTAINER_ADAPTERS | {"native", "systemd"}:
            raise CatalogError(f"{name}: unsupported adapter {adapter}")
        if portable and (adapter not in CONTAINER_ADAPTERS
                         or set(runtime) - {"adapter", "version", "sha256", "options", "image",
                                            "executable"}):
            raise CatalogError(f"{name}: imported runtimes must use portable typed adapters")
        if portable and "image" in runtime:
            try:
                image_reference(runtime["image"])
            except ValueError as exc:
                raise CatalogError(f"{name}: {exc}") from None
        if portable and "executable" in runtime:
            executable = runtime["executable"]
            if not isinstance(executable, str) or not Path(executable).is_absolute():
                raise CatalogError(f"{name}: imported executables must be absolute in-container paths")
    sources = {}
    for name, artifact in config["artifacts"].items():
        source = artifact["source"]
        if portable and (source["kind"] != "hf" or not artifact.get("hashes") or not artifact.get("size")):
            raise CatalogError(f"{name}: imported artifacts require pinned Hugging Face files, hash and size")
        sources[name] = (Path(source["path"]) if source["kind"] == "local" else
                         Path("/artifact") / source["repo"] / source["revision"] / source["file"])
    for model, spec in config["models"].items():
        for name, profile in spec["profiles"].items():
            try:
                runtime = config["runtimes"][profile["runtime"]]
                if runtime["adapter"] in {"llama.cpp", "stable-diffusion.cpp"}:
                    profile.setdefault("port", LLAMA_ENGINE_PORT if runtime["adapter"] == "llama.cpp" else 1234)
                    probe = "/health" if runtime["adapter"] == "llama.cpp" else "/v1/models"
                    if isinstance(profile.setdefault("health", {}), dict):
                        profile["health"].setdefault("path", probe)
                if set(profile) - {"runtime", "artifacts", "device", "gpu", "vram_mib", "ram_mib", "host", "port",
                                   "options", "health", "resources", "lifecycle", "metrics_path"}:
                    raise ValueError("unsupported profile fields")
                for key, minimum, maximum in (("port", 1, 65535), ("vram_mib", 1, 1048576),
                                              ("ram_mib", 1, 2097152)):
                    if key in profile and (type(profile[key]) is not int or not minimum <= profile[key] <= maximum):
                        raise ValueError(f"{key} must be an integer in [{minimum}, {maximum}]")
                if profile["device"] == "cpu" and "vram_mib" in profile:
                    raise ValueError("vram_mib applies only to GPU profiles")
                if "gpu" in profile and (profile["device"] != "gpu" or not isinstance(profile["gpu"], str)):
                    raise ValueError("gpu selector must be a string on a GPU profile")
                host = profile.get("host", "127.0.0.1")
                if host == "localhost":
                    address = ipaddress.ip_address("127.0.0.1")
                else:
                    address = ipaddress.ip_address(host)
                if runtime["adapter"] in CONTAINER_ADAPTERS and not address.is_loopback:
                    raise ValueError("container engines must bind to loopback")
                if portable and not address.is_loopback:
                    raise ValueError("imported profiles must bind to loopback")
                for key, bounds in (("resources", {"memory_high_mib": (1, 2097152),
                                                     "memory_max_mib": (1, 2097152),
                                                     "swap_max_mib": (0, 2097152)}),
                                    ("lifecycle", {"stop_timeout_seconds": (1, 600)}),
                                    ("health", {"timeout_seconds": (1, 3600), "path": None})):
                    settings = profile.get(key, {})
                    if not isinstance(settings, dict) or settings.keys() - bounds.keys():
                        raise ValueError(f"invalid {key} fields")
                    for option, value in settings.items():
                        limit = bounds[option]
                        if limit and (type(value) is not int or not limit[0] <= value <= limit[1]):
                            raise ValueError(f"{key}.{option} invalid")
                        if not limit and (not isinstance(value, str) or not value.startswith("/")
                                          or value.startswith("//") or any(x.isspace() for x in value)):
                            raise ValueError(f"{key}.{option} must be a local HTTP path")
                if "metrics_path" in profile:
                    value = profile["metrics_path"]
                    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
                            or any(x.isspace() for x in value) or "port" not in profile):
                        raise ValueError("metrics_path requires a local HTTP path and an explicit port")
                resources = profile.get("resources", {})
                if resources.get("memory_high_mib", 0) > resources.get("memory_max_mib", 2097152):
                    raise ValueError("memory_high_mib cannot exceed memory_max_mib")
                if runtime["adapter"] == "systemd":
                    from re import fullmatch
                    if set(runtime) - {"adapter", "unit"} or not fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*\.service",
                                                                           str(runtime.get("unit", ""))):
                        raise ValueError("invalid adopted systemd unit")
                    continue
                paths = {role: ([sources[key] for key in ids] if isinstance(ids, list) else sources[ids])
                         for role, ids in profile["artifacts"].items()}
                selector = (profile.get("gpu", _PROBE_GPU) if profile["device"] == "gpu" else None)
                probe = dict(runtime)
                if runtime["adapter"] in CONTAINER_ADAPTERS:
                    probe["image"] = runtime.get("image", _PROBE_IMAGE)
                else:
                    probe["executable"] = "/runtime/placeholder"
                build_launch(probe, profile, paths, gpu_id=selector,
                             trusted_native=not portable, identity=f"{model}\0{name}")
            except (ValueError, TypeError) as exc:
                raise CatalogError(f"{model}/{name}: {exc}") from None
    return config


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _write_json(path: Path, raw: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            json.dump(raw, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _http_ready(url: str | None, timeout: float = 1.0) -> bool:
    if not url:
        return True
    # Managed readiness checks are local. Do not follow redirects out of the process.
    try:
        from http.client import HTTPConnection
        from urllib.parse import urlsplit

        p = urlsplit(url)
        if p.scheme != "http" or p.hostname not in {"127.0.0.1", "::1", "localhost"}:
            return False
        with_connection = HTTPConnection(p.hostname, p.port, timeout=timeout)
        try:
            with_connection.request("GET", p.path or "/")
            return with_connection.getresponse().status == 200
        finally:
            with_connection.close()
    except (ValueError, OSError, HTTPException):
        return False


class Controller:
    def __init__(self, catalog: Path, state_dir: Path, policy: dict | None = None, *,
                 driver=None, hardware=snapshot, artifact_store=None, boot_reader=boot_id,
                 ready=_http_ready, container=None):
        self.catalog_path = catalog
        self.state_dir = state_dir
        self.policy = policy or {}
        self.state = State(state_dir)
        self.state.recover()
        self.telemetry = Telemetry(self.state.db)
        self.store = artifact_store or ArtifactStore(state_dir / "artifacts")
        self.container = container or Podman()
        self.driver = driver or Supervisor(self.policy.get("adopted", {}))
        self.hardware = hardware
        self.ready = ready
        self.boot_reader = boot_reader
        self.boot = {"state": "pending", "message": "Waiting for boot reconciliation"}
        self.config: dict = {"models": {}, "runtimes": {}, "artifacts": {}, "boot": []}
        self.config_valid = False
        self.lock = threading.Lock()
        self.jobs = queue.Queue(maxsize=64)
        self.in_progress: dict[tuple[str, str, str, str, str, str], dict] = {}
        self.halt = threading.Event()
        self.threads: list[threading.Thread] = []

    def _maintenance_read_lock(self, *, wait: bool):
        lock = (self.state_dir / "maintenance.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | (0 if wait else fcntl.LOCK_NB))
        except OSError as exc:
            lock.close()
            if isinstance(exc, BlockingIOError):
                raise RuntimeError("LocalModelHub is applying an update; retry the action shortly") from None
            raise
        return lock

    def stage_catalog(self, raw: dict) -> dict:
        checked = _check_config(validate_catalog(raw), portable=True)
        # Imported catalogs describe recipes. The operator selects boot profiles
        # in the local catalog after review, not by accepting another publisher's defaults.
        checked["boot"] = []
        with self.lock:
            _write_json(self.state_dir / "pending-models.json", checked)
        return {"state": "staged", "message": "Catalog staged for the next OS boot with no automatic starts",
                "catalog_blake3": _catalog_digest(checked)}

    def _key(self, model: str, profile: str | None) -> tuple[str, str]:
        if model not in self.config["models"]:
            raise KeyError(f"Unknown model: {model}")
        entry = self.config["models"][model]
        profiles = entry["profiles"]
        selected = profile or entry.get("default_profile") or (next(iter(profiles)) if len(profiles) == 1 else None)
        if selected not in profiles:
            raise ValueError(f"Select an existing profile for {model}")
        return model, selected

    def _profile(self, key: tuple[str, str]) -> tuple[dict, dict]:
        profile = self.config["models"][key[0]]["profiles"][key[1]]
        return profile, self.config["runtimes"][profile["runtime"]]

    def _all(self):
        for model, data in self.config["models"].items():
            for profile in data["profiles"]:
                yield model, profile

    def _observe(self, key) -> dict:
        _, runtime = self._profile(key)
        return self.driver.observe(*key, runtime)

    @staticmethod
    def _url(profile):
        if not profile.get("port"):
            return None
        health = profile.get("health") or {}
        path = health.get("path", "/health")
        host = "[::1]" if ":" in profile.get("host", "") else "127.0.0.1"
        return f"http://{host}:{profile['port']}{path}" if path else None

    def _trusted_runtime(self, profile, runtime) -> dict:
        runtime = copy.deepcopy(runtime)
        rid = profile["runtime"]
        adapter = runtime["adapter"]
        if adapter == "systemd":
            if self.policy.get("adopted", {}).get(runtime.get("unit")) != "allowlisted":
                raise RuntimeError("This adopted system unit is read-only")
            return runtime
        approved = self.policy.get("runtimes", {}).get(rid)
        if not isinstance(approved, dict) or not approved:
            raise RuntimeError(f"Runtime {rid} is not approved locally; add its container image to local policy")
        if approved.get("adapter") != adapter:
            raise RuntimeError(f"Runtime adapter does not match approval for {rid}")
        if runtime.get("version") and approved.get("version") != runtime["version"]:
            raise RuntimeError(f"Runtime {rid} version does not match local approval")

        if adapter in CONTAINER_ADAPTERS:
            image = approved.get("image")
            try:
                image = image_reference(image)
            except ValueError as exc:
                raise RuntimeError(f"Runtime {rid} needs a digest-pinned image in local policy: {exc}") from None
            if runtime.get("image") and image_reference(runtime["image"]) != image:
                raise RuntimeError(f"Catalog image does not match the approved image for {rid}")
            approved_digest = image_digest(image)
            for source, declared in (("local policy", approved.get("sha256")),
                                     ("catalog", runtime.get("sha256"))):
                if declared and str(declared).lower() != approved_digest:
                    raise RuntimeError(f"Runtime {rid} image digest does not match the {source}")
            if "executable" in runtime:
                try:
                    runtime["executable"] = str(Path(runtime["executable"]))
                except TypeError:
                    raise RuntimeError(f"Runtime {rid} executable must be a path") from None
                if not Path(runtime["executable"]).is_absolute():
                    raise RuntimeError(f"Runtime {rid} executable must be an absolute in-container path")
            runtime["image"] = image
            return runtime

        # Native is an explicitly reviewed host-only integration.
        raw_exe = approved.get("executable")
        if not isinstance(raw_exe, str) or not Path(raw_exe).is_absolute():
            raise RuntimeError(f"Runtime {rid} requires an absolute approved executable")
        exe = Path(raw_exe).resolve(strict=True)
        if not exe.is_file() or not os.access(exe, os.X_OK):
            raise RuntimeError(f"Runtime not executable as the service user: {exe}")
        if runtime.get("executable") and Path(runtime["executable"]).resolve() != exe:
            raise RuntimeError(f"Catalog executable does not match the approved executable for {rid}")
        digest = approved.get("sha256")
        if digest:
            with exe.open("rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != digest:
                    raise RuntimeError(f"Runtime checksum mismatch: {rid}")
        advanced = any(k in profile or k in runtime for k in
                       ("args", "argv", "extra_argv", "extra_args", "environment", "env",
                        "cwd", "ld_library_path"))
        if advanced and not approved.get("allow_advanced"):
            raise RuntimeError(f"Runtime {rid} needs explicit approval for native arguments/environment")
        if runtime.get("sha256") and runtime["sha256"] != digest:
            raise RuntimeError(f"Runtime {rid} hash does not match local approval")
        runtime["executable"] = str(exe)
        return runtime

    def _gpu(self, profile, hw) -> dict | None:
        if profile["device"] != "gpu":
            return None
        gpus = hw.get("gpus", [])
        requested = profile.get("gpu")
        if requested is not None:
            candidates = [g for g in gpus if requested.casefold() == str(g.get("id")).casefold()
                          or (len(gpus) == 1 and str(requested) == "0")]
        else:
            candidates = list(gpus)
        if len(candidates) != 1:
            raise RuntimeError("No matching GPU detected" if not candidates
                               else "Several GPUs match; choose an explicit GPU in the profile")
        return candidates[0]

    def _coexistence(self, key, profile, gpu, hw, ignore=None) -> None:
        if (ram := profile.get("ram_mib")) and hw.get("ram_available_bytes") is not None and ignore is None:
            if ram * MIB > hw["ram_available_bytes"]:
                raise RuntimeError(f"Not enough available host RAM: need {ram} MiB, "
                                   f"have {hw['ram_available_bytes'] // MIB} MiB")
        if not gpu:
            return
        active = []
        for other in self._all():
            if other == key or other == ignore:
                continue
            spec, _ = self._profile(other)
            if spec["device"] != "gpu" or self._observe(other)["state"] not in ACTIVE:
                continue
            try:
                other_gpu = self._gpu(spec, hw)
            except RuntimeError:
                other_gpu = None
            if not other_gpu or other_gpu["id"] == gpu["id"]:
                active.append((other, spec.get("vram_mib")))
        free, total = gpu.get("free_bytes"), gpu.get("total_bytes")
        need = profile.get("vram_mib")
        reserve = self.policy.get("gpu_headroom_mib", 512) * MIB
        if active and (not need or any(not budget for _, budget in active) or not total or free is None):
            occupants = ", ".join(f"{m}/{p}" for (m, p), _ in active)
            raise RuntimeError(f"Cannot verify GPU coexistence with {occupants}; no model was stopped")
        if need and total and (need + sum(budget or 0 for _, budget in active)) * MIB + reserve > total:
            raise RuntimeError(f"Declared VRAM budgets do not fit on {gpu.get('name', gpu['id'])}")
        if need and free is not None and need * MIB + reserve > free:
            raise RuntimeError(f"Not enough free VRAM: need {need} MiB plus headroom, have {free // MIB} MiB")
        if not need and not active and total and free is not None:
            # Unknown estimates are only safe to try on a substantially unoccupied device.
            if free < total * 0.85 and ignore is None:
                raise RuntimeError("GPU already holds allocations and the profile has no VRAM budget")

    def _files(self, profile, progress) -> dict:
        resolved = {}
        roles = profile.get("artifacts", {})

        def resolve(artifact_id):
            artifact = self.config["artifacts"][artifact_id]
            try:
                return self.store.resolve(artifact)
            except ArtifactUnavailable:
                progress(f"Downloading {artifact_id} from Hugging Face")
                return self.store.resolve(artifact, download=True)

        for index, (role, refs) in enumerate(roles.items(), 1):
            progress(f"Verifying artifact role {role} ({index}/{len(roles)})")
            if isinstance(refs, list):
                resolved[role] = [resolve(r) for r in refs]
            else:
                resolved[role] = resolve(refs)
        return resolved

    def _prepare(self, key, *, capacity=True, progress=lambda _: None):
        profile, original_runtime = self._profile(key)
        progress("Verifying runtime and resources")
        runtime = self._trusted_runtime(profile, original_runtime)
        observed = self._observe(key)
        if observed["state"] == "unknown":
            raise RuntimeError(f"Unable to inspect model state: {observed.get('message') or 'unknown'}")
        hw = self.hardware()
        gpu = self._gpu(profile, hw)
        if capacity:
            self._coexistence(key, profile, gpu, hw)
            self._check_port(profile)
        files = self._files(profile, progress)
        launch = None if runtime["adapter"] == "systemd" else build_launch(
            runtime, profile, files, gpu_id=str(gpu["id"]) if gpu else None,
            trusted_native=runtime["adapter"] == "native",
            identity=f"{key[0]}\0{key[1]}", podman=self.container.executable)
        if launch is not None and launch.container is not None:
            self.container.ensure(launch.container.image, progress=progress)
        return profile, runtime, launch

    @staticmethod
    def _check_port(profile):
        port = profile.get("port")
        if not port:
            return
        host = profile.get("host", "127.0.0.1")
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
            except OSError as exc:
                raise RuntimeError(f"Cannot bind {host}:{port}: {exc.strerror}") from None

    def _wait(self, key, *, until: str, timeout: float, url=None):
        start = time.monotonic()
        deadline = start + timeout
        scheduled = self._profile(key)[1]["adapter"] == "systemd"
        while True:
            observed = self._observe(key)
            current = observed["state"]
            if until == "stopped" and current in {"failed", "stopped", "unconfigured"}:
                return
            if until == "ready":
                if current in {"failed", "unconfigured"} or (
                    current == "stopped" and (not scheduled or time.monotonic() - start >= 2)
                ):
                    raise RuntimeError(f"Process exited before readiness: {observed.get('message') or current}")
                if current == "running" and self.ready(url):
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"Timed out waiting for {key[0]}/{key[1]} to be {until}")
            if self.halt.wait(min(0.25, remaining)):
                raise RuntimeError("Controller stopping")

    def _start(self, key, prepared=None, progress=lambda _: None):
        profile, runtime, launch = prepared or self._prepare(key, progress=progress)
        # State and capacity can change while artifacts are downloaded.
        hw = self.hardware()
        self._coexistence(key, profile, self._gpu(profile, hw), hw)
        self._check_port(profile)
        progress("Starting Podman container" if getattr(launch, "container", None) else "Starting native service")
        self.driver.start(*key, runtime, launch, profile)
        health = profile.get("health") or {}
        timeout = health.get("timeout_seconds", 120)
        url = getattr(launch, "health_url", None) or self._url(profile)
        progress("Waiting for inference readiness")
        self._wait(key, until="ready", timeout=timeout, url=url)

    def _stop(self, key):
        profile, runtime = self._profile(key)
        self.driver.stop(*key, runtime)
        self._wait(key, until="stopped", timeout=(profile.get("lifecycle", {}).get("stop_timeout_seconds", 30) + 10))

    def _operate(self, action, key, previous, progress=lambda _: None):
        profile, _ = self._profile(key)
        state = self._observe(key)["state"]
        if action == "stop":
            if state in {"stopped", "unconfigured", "failed"}:
                return "Already stopped"
            progress("Stopping the selected profile")
            self._stop(key)
            return "Stopped"
        if state in ACTIVE:
            if action == "switch":
                raise RuntimeError("Target is already running; nothing was switched")
            return "Already running"
        if action == "switch":
            if self._observe(previous)["state"] not in {"running", "ready"}:
                raise RuntimeError("The explicitly selected source is no longer running")
            prepared = self._prepare(key, capacity=False, progress=progress)
            hw = self.hardware()
            gpu = self._gpu(profile, hw)
            # Check whether the target could fit after releasing this exact source.
            hypothetical = dict(gpu, free_bytes=gpu.get("total_bytes")) if gpu else None
            self._coexistence(key, profile, hypothetical, hw, ignore=previous)
            old_spec, _ = self._profile(previous)
            if old_spec.get("port") != profile.get("port"):
                self._check_port(profile)
            if self._observe(previous)["state"] not in {"running", "ready"}:
                raise RuntimeError("The selected source changed during preflight")
            progress(f"Stopping selected source {previous[0]}/{previous[1]}")
            self._stop(previous)
            try:
                self._start(key, prepared=prepared, progress=progress)
            except Exception as exc:
                raise RuntimeError(f"Stopped {previous[0]}/{previous[1]}, but target failed: {exc}. "
                                   "Restore the previous model manually if desired.") from None
            return f"Switched from {previous[0]}/{previous[1]}; ready"
        self._start(key, progress=progress)
        return "Ready"

    def submit(self, action, model, profile=None, from_model=None, from_profile=None, source="panel"):
        if action not in {"start", "stop", "switch"}:
            raise ValueError("Invalid action")
        with self.lock:
            if not self.config_valid or self.boot["state"] != "complete":
                raise RuntimeError("Boot verification has not completed successfully")
            key = self._key(model, profile)
            previous = None
            if action == "switch":
                if not from_model or not from_profile:
                    raise ValueError("Switch needs an explicit source model and profile")
                previous = self._key(from_model, from_profile)
                if key == previous:
                    raise ValueError("Source and target are identical")
            elif from_model is not None or from_profile is not None:
                raise ValueError("Only Switch accepts a source model")
            fingerprint = (action, *key, *(previous or ("", "")), source)
            if fingerprint in self.in_progress:
                return self.in_progress[fingerprint]
            if self.jobs.full():
                raise RuntimeError("Action queue is full")
            maintenance = self._maintenance_read_lock(wait=False)
            try:
                job = self.state.add(action, *key, source)
                self.in_progress[fingerprint] = job
                self.jobs.put_nowait((fingerprint, job, key, previous, maintenance))
            except BaseException:
                maintenance.close()
                raise
            return job

    def reconcile(self):
        """Called once by controller startup; persistent ID prevents repeat on daemon restart."""
        try:
            current = self.boot_reader()
            is_new = self.state.get("boot_id") != current
            pending = self.state_dir / "pending-models.json"
            saved = self.state_dir / "current-models.json"
            if is_new:
                selected = pending if pending.exists() else self.catalog_path
                self.config = _check_config(load_catalog(selected))
                _write_json(saved, self.config)
                if selected == pending:
                    # Keep the approved catalog as the ongoing local input on future boots.
                    _write_json(self.catalog_path, self.config)
                    pending.unlink()
            else:
                # The saved snapshot is the already-validated configuration from
                # this OS boot; ignore newly edited input until the next boot.
                self.config = load_catalog(saved if saved.exists() else self.catalog_path)
            digest = _catalog_digest(self.config)
            self.config_valid = True
            if not is_new:
                self.boot = {"state": "complete", "message": self.state.get("boot_message")
                             or "Already reconciled this OS boot", "catalog_blake3": digest}
                return
        except Exception as exc:
            self.boot = {"state": "failed", "message": str(exc)}
            return

        self.boot = {"state": "reconciling", "message": "Verified catalog; checking boot profiles",
                     "catalog_blake3": digest}
        stopped = []
        same = 0
        completed = 0
        for entry in self.config.get("boot", []):
            if self.halt.is_set():
                return
            key = entry["model"], entry.get("profile")
            try:
                key = self._key(*key)
                observed = self._observe(key)["state"]
                if observed in ACTIVE:
                    same += 1
                    continue
                others = [k for k in self._all() if k[0] == key[0] and k != key
                          and self._observe(k)["state"] in ACTIVE]
                if others:
                    raise RuntimeError(f"Existing profile {others[0][1]} is active; boot never switches")
                job = self.state.add("start", *key, "boot")
                self.state.update(job["id"], "running", "Applying missing boot action")
                try:
                    self._start(key, progress=lambda text: self.state.update(job["id"], "running", text))
                except Exception as exc:
                    self.state.update(job["id"], "failed", str(exc))
                    raise
                self.state.update(job["id"], "done", "Ready")
                completed += 1
            except Exception as exc:
                stopped.append(f"{key[0]}/{key[1]}: {exc}")
        report = f"Boot: {completed} started, {same} already active, {len(stopped)} blocked"
        if stopped:
            report += ". " + "; ".join(stopped)
        self.state.put("boot_message", report)
        self.state.put("boot_id", current)
        self.boot = {"state": "complete", "message": report, "catalog_blake3": digest}

    def _worker(self):
        with self._maintenance_read_lock(wait=True):
            self.reconcile()
        while not self.halt.is_set():
            try:
                fingerprint, job, key, previous, maintenance = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.state.update(job["id"], "running", "Working")
                message = self._operate(job["action"], key, previous,
                                        progress=lambda text: self.state.update(job["id"], "running", text))
                self.state.update(job["id"], "done", message)
            except Exception as exc:
                self.state.update(job["id"], "failed", str(exc))
            finally:
                with self.lock:
                    self.in_progress.pop(fingerprint, None)
                maintenance.close()
                self.jobs.task_done()

    def _samples(self):
        while not self.halt.is_set():
            try:
                hw = self.hardware()
                ram, spare = hw.get("ram_total_bytes"), hw.get("ram_available_bytes")
                allocations = [max(0, g["total_bytes"] - g["free_bytes"]) for g in hw.get("gpus", [])
                               if g.get("total_bytes") is not None and g.get("free_bytes") is not None]
                self.state.sample(time.time(), max(0, ram - spare) if ram is not None and spare is not None else None,
                                  sum(allocations) if allocations else None)
                if self.config_valid:
                    for key in self._all():
                        spec, _ = self._profile(key)
                        if (path := spec.get("metrics_path")) and self._observe(key)["state"] == "running":
                            self.telemetry.sample(*key, spec["port"], path)
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                pass
            self.halt.wait(20)

    def start(self):
        for operation in (self._worker, self._samples):
            thread = threading.Thread(target=operation, daemon=True)
            thread.start()
            self.threads.append(thread)

    def shutdown(self):
        self.halt.set()
        for thread in self.threads:
            thread.join(timeout=1)
        while True:
            try:
                *_, maintenance = self.jobs.get_nowait()
            except queue.Empty:
                break
            maintenance.close()
            self.jobs.task_done()

    def inference_target(self, model: str, *, exclude_ports=()) -> tuple[str, int] | None:
        """Find a ready loopback engine without changing lifecycle state."""
        if not self.config_valid or self.boot["state"] != "complete":
            return None
        spec = self.config["models"].get(model)
        if spec is None:
            return None
        profiles = spec["profiles"]
        preferred = spec.get("default_profile")
        for name in sorted(profiles, key=lambda name: (name != preferred, name)):
            profile = profiles[name]
            host = profile.get("host", "127.0.0.1")
            port = profile.get("port")
            if host not in {"localhost", "127.0.0.1", "::1"} or not port or port in exclude_ports:
                continue
            try:
                if self._observe((model, name))["state"] == "running" and self.ready(self._url(profile)):
                    return ("127.0.0.1" if host == "localhost" else host), port
            except (OSError, RuntimeError):
                continue
        return None

    def inference_types(self) -> dict:
        return {model: spec["io"] for model, spec in self.config["models"].items()} if self.config_valid else {}

    def status(self):
        models = []
        for model, spec in self.config["models"].items():
            profiles = []
            for name, profile in spec["profiles"].items():
                try:
                    observed = self._observe((model, name))
                    if observed["state"] == "running":
                        observed["state"] = "ready" if self.ready(self._url(profile)) else "unhealthy"
                except RuntimeError as exc:
                    observed = {"state": "unknown", "message": str(exc)}
                profiles.append({"id": name, "device": profile["device"], "port": profile.get("port"),
                                 "host": profile.get("host", "127.0.0.1"),
                                 "start_on_boot": any(b["model"] == model and self._key(model, b.get("profile"))[1] == name
                                                      for b in self.config["boot"]),
                                 "controllable": self._can_control(profile), **observed})
            models.append({"id": model, "name": spec["name"], "io": spec["io"], "profiles": profiles,
                           "default_profile": spec.get("default_profile"), "description": spec.get("description")})
        return {"models": models, "jobs": self.state.jobs(), "hardware": self.hardware(), "boot": self.boot,
                "catalog_staged": (self.state_dir / "pending-models.json").exists()}

    def logs(self, model, profile):
        key = self._key(model, profile)
        _, runtime = self._profile(key)
        return self.driver.logs(*key, runtime)

    def stats(self, model=None):
        stats = self.state.stats(model)
        measured = self.telemetry.stats(model)
        return {**stats, **measured, "model": model, "language_by_model": self.state.language_stats(model),
                "metrics": {**stats["metrics"], **measured["metrics"]}}

    def _can_control(self, profile: dict) -> bool:
        runtime = self.config["runtimes"][profile["runtime"]]
        if runtime["adapter"] == "systemd":
            return self.policy.get("adopted", {}).get(runtime.get("unit")) == "allowlisted"
        approved = self.policy.get("runtimes", {}).get(profile["runtime"])
        if not isinstance(approved, dict) or not approved:
            return False
        if runtime["adapter"] in CONTAINER_ADAPTERS:
            return bool(approved.get("image"))
        return bool(approved.get("executable"))
