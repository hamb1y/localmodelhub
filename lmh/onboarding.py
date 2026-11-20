"""Bring up the verified release's public CPU starter without switching models."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import shutil
import subprocess
import sys
import time
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.parse import urlsplit

from .api import _credential
from .catalog import load_catalog
from .controller import MIB, _catalog_digest, _check_config
from .runtime_install import _read_policy, install_cpu_runtime


DASHBOARD = "http://127.0.0.1:5189"
MODEL = "qwen2.5-chat"
PROFILE = "cpu"
SERVICE = "localmodelhub.service"
SERVICE_HINT = f"check `journalctl --user -u {SERVICE}` as ai"
START_HINT = "inspect the profile and its logs in the panel (no models were stopped)"
PANEL_TIMEOUT = 45
JOB_TIMEOUT = 20 * 60
POLL_INTERVAL = 1.0


class BackendError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _local_address(endpoint: str) -> tuple[str, int]:
    message = "LMH_ENDPOINT must be a plain HTTP loopback URL, without credentials, a path, query, or fragment."
    try:
        address = urlsplit(endpoint)
        port = address.port
    except ValueError:
        raise ValueError(message) from None
    host = "[::1]" if address.hostname == "::1" else address.hostname
    authority = host if port is None else f"{host}:{port}"
    if (address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "::1"}
            or address.username is not None or address.password is not None or address.path not in {"", "/"}
            or "?" in endpoint or "#" in endpoint or any(not 33 <= ord(c) <= 126 for c in endpoint)
            or port == 0 or address.netloc.lower() != authority):
        raise ValueError(message)
    return ("127.0.0.1" if address.hostname == "localhost" else address.hostname, port or 80)


class Backend:
    """Bounded direct loopback requests; neither redirects nor proxies are used."""

    def __init__(self, endpoint: str, timeout: float, token: str | None = None):
        self.host, self.port = _local_address(endpoint)
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.headers = {"Accept": "application/json"}
        if token:
            try:
                self.headers["Authorization"] = f"Bearer {_credential(token)}"
            except ValueError:
                raise ValueError("LMH_API_TOKEN must be a valid bearer token.") from None

    def request(self, path: str, payload: dict | None = None) -> dict:
        headers = self.headers if payload is None else {**self.headers, "Content-Type": "application/json"}
        body = None if payload is None else json.dumps(payload).encode()
        connection = HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request("GET" if payload is None else "POST", path, body=body, headers=headers)
            with connection.getresponse() as response:
                status = response.status
                raw = response.read(4 * 1024 * 1024 + 1)
        except (OSError, HTTPException) as exc:
            raise BackendError(f"Cannot reach {self.endpoint}{path}: {exc}") from None
        finally:
            connection.close()
        result = None
        if len(raw) <= 4 * 1024 * 1024:
            try:
                result = json.loads(raw)
            except ValueError:
                pass
        if not 200 <= status < 300:
            detail = result.get("error") if isinstance(result, dict) else None
            if isinstance(detail, dict):
                detail = detail.get("message")
            raise BackendError(f"HTTP {status}" + (f": {detail}" if isinstance(detail, str) else ""), status)
        if not isinstance(result, dict):
            raise BackendError(f"Invalid or oversized JSON response from {path}")
        return result

    def status(self) -> dict:
        return self.request("/api/status")

    def action(self, action: str, model: str, profile: str) -> dict:
        return self.request("/api/actions", {"action": action, "model": model, "profile": profile})


def _ai_home(*, isolated: bool = False) -> Path:
    uid = os.geteuid()
    try:
        account = pwd.getpwuid(uid)
    except KeyError:
        account = None
    if uid == 0 or os.getuid() != uid or account is None or account.pw_name != "ai":
        raise ValueError("Run onboarding as the dedicated Linux user ai or rerun the public installer as an administrator.")
    home = Path(account.pw_dir)
    current = Path.home()
    if not current.is_absolute() or current == Path("/"):
        raise ValueError("HOME must be an absolute non-root directory.")
    if not isolated and (not home.is_absolute() or home == Path("/") or current != home):
        raise ValueError("HOME must match the ai account's registered home directory.")
    return current


def _until(check, timeout: float, on_timeout: str):
    deadline = time.monotonic() + timeout
    last_error = None
    while True:
        try:
            result = check()
        except BackendError as exc:
            if exc.status is not None and exc.status < 500:
                hint = " Check LMH_API_TOKEN against the local policy." if exc.status in {401, 403} else ""
                raise RuntimeError(f"{exc}.{hint}") from None
            last_error = exc
        else:
            if result is not None:
                return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            detail = f" Last HTTP error: {last_error}" if last_error else ""
            raise RuntimeError(on_timeout + detail)
        time.sleep(min(POLL_INTERVAL, remaining))


def _panel_status(backend: Backend, dashboard: str) -> dict:
    def check():
        status = backend.status()
        boot = status.get("boot", {})
        if boot.get("state") == "failed":
            raise RuntimeError(f"Boot catalog verification failed: {boot.get('message')}; {SERVICE_HINT}.")
        return status if boot.get("state") == "complete" else None

    return _until(check, PANEL_TIMEOUT, f"Panel or boot verification not ready at {dashboard}; {SERVICE_HINT}.")


def _entry(status: dict) -> dict | None:
    return next((profile for model in status["models"] if model["id"] == MODEL
                 for profile in model["profiles"] if profile["id"] == PROFILE), None)


def _recipe(catalog: dict):
    model = catalog["models"].get(MODEL, {})
    profile = model.get("profiles", {}).get(PROFILE)
    if profile is None:
        return None
    artifacts = profile["artifacts"]
    if set(artifacts) != {"model"} or not isinstance(artifacts["model"], str):
        return None
    return profile, catalog["runtimes"][profile["runtime"]], catalog["artifacts"][artifacts["model"]], model["io"]


def _load_active(config: Path, state: Path, status: dict) -> dict:
    saved = state / "current-models.json"
    active = load_catalog(saved if saved.exists() else config / "models.json")
    if not status["boot"].get("catalog_blake3") or _catalog_digest(active) != status["boot"]["catalog_blake3"]:
        raise RuntimeError("Cannot match the daemon's active catalog; rerun with the installed local panel.")
    return active


def _matches_bundled(bundled, active, *, alternate_port: bool) -> bool:
    if alternate_port and active is not None:
        profile, runtime, artifact, model_io = active
        active = ({**profile, "port": bundled[0]["port"]}, runtime, artifact, model_io)
    return bundled == active


def _inference_port(entry: dict, profile: dict) -> int:
    port = entry.get("port")
    if type(port) is not int or port != profile["port"] or entry.get("host") != profile["host"]:
        raise RuntimeError("The inference address reported by the panel does not match the approved catalog.")
    return port


def _free(path: Path) -> int:
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def _cached(artifact: dict, cache: Path) -> bool:
    from huggingface_hub import try_to_load_from_cache

    source = artifact["source"]
    file = try_to_load_from_cache(source["repo"], source["file"], revision=source["revision"], cache_dir=cache)
    try:
        return isinstance(file, str) and Path(file).is_file() and Path(file).stat().st_size == artifact["size"]
    except OSError:
        return False


def _preflight(status: dict, profile: dict, artifact: dict, data: Path, state: Path, install: bool):
    hardware = status["hardware"]
    if profile["device"] != "cpu" or not hardware.get("cpu_logical_count"):
        raise RuntimeError("The bundled starter needs an available CPU.")
    ram = hardware.get("ram_available_bytes")
    if ram is not None and ram < profile["ram_mib"] * MIB:
        raise RuntimeError(f"The starter needs {profile['ram_mib']} MiB of available RAM; free memory and retry.")
    cache = state / "artifacts"
    if not _cached(artifact, cache) and _free(cache) < artifact["size"]:
        raise RuntimeError(f"Free at least {(artifact['size'] + MIB - 1) // MIB} MiB on the filesystem for {cache}.")
    if install and _free(data / "runtimes") < 160 * MIB:
        raise RuntimeError(f"Free at least 160 MiB on the filesystem for {data / 'runtimes'}.")


def _restart(state: Path) -> None:
    # Like release activation, don't interrupt a queued/running lifecycle job.
    # The new daemon's worker waits on this lock before reconciling the saved boot.
    with (state / "maintenance.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A lifecycle job or update is active; wait for it to finish and rerun onboarding.") from None
        try:
            subprocess.run(["systemctl", "--user", "restart", SERVICE], stdin=subprocess.DEVNULL,
                           capture_output=True, check=True, timeout=45)
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError(f"Cannot restart the panel to load runtime approval; {SERVICE_HINT}.") from None


def _wait_for_model(backend: Backend, job_id: str | None, emit) -> dict:
    previous = None

    def check():
        nonlocal previous
        status = backend.status()
        job = next((job for job in status["jobs"] if job["id"] == job_id), None) if job_id else None
        if job:
            message = job.get("message")
            if job["state"] in {"failed", "interrupted"}:
                raise RuntimeError(f"Start job {job['state']}: {message}; {START_HINT}.")
            if message and message != previous:
                emit("start", "working", message)
                previous = message
        entry = _entry(status)
        if entry is None:
            raise RuntimeError("The starter disappeared from the active catalog; rerun onboarding.")
        if entry["state"] == "ready" and (not job_id or (job and job["state"] == "done")):
            return entry
        return None

    return _until(check, JOB_TIMEOUT, f"Starter readiness timed out; {START_HINT}.")


def _smoke(endpoint: str, token: str | None) -> str:
    client = Backend(endpoint, timeout=60, token=token)
    if not any(model.get("id") == MODEL for model in client.request("/v1/models").get("data", [])):
        raise RuntimeError("The unified inference API did not list the starter model.")
    response = client.request("/v1/chat/completions", {
        "model": MODEL, "messages": [{"role": "user", "content": "Reply with the word hello."}],
        "temperature": 0, "max_tokens": 16, "stream": False,
    })
    choices = response.get("choices") or [{}]
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("The local chat smoke test did not return text.")
    return endpoint + "/v1"


def onboard(emit) -> int:
    stage = "backend"
    try:
        dashboard = os.environ.get("LMH_ENDPOINT") or DASHBOARD
        local_address = _local_address(dashboard)
        dashboard = dashboard.rstrip("/")
        alternate_port = local_address != _local_address(DASHBOARD)
        stage = "account"
        home = _ai_home(isolated=alternate_port)
        data = home / ".local/share/localmodelhub"
        config = home / ".config/localmodelhub"
        state = data / "state"
        policy_path = config / "policy.json"
        stage = "backend"
        emit(stage, "working", "Checking the local panel and boot catalog.")
        policy = _read_policy(policy_path)
        token = os.environ.get("LMH_API_TOKEN") or (None if alternate_port else policy.get("api_token"))
        backend = Backend(dashboard, timeout=4, token=token)
        status = _panel_status(backend, dashboard)
        emit(stage, "ok", "Panel is ready.")

        stage = "catalog"
        entry = _entry(status)
        if entry is None:
            emit("done", "ok", "Panel is ready. Your catalog has no bundled starter; it was left unchanged.",
                 dashboard=dashboard)
            return 0
        bundled = _recipe(_check_config(load_catalog(data / "current/catalogs/starter.json")))
        if bundled is None:
            raise RuntimeError("The installed release is missing its approved starter recipe; reinstall the release.")
        active = _recipe(_load_active(config, state, status))
        if (not _matches_bundled(bundled, active, alternate_port=alternate_port)
                or active != _recipe(_check_config(load_catalog(config / "models.json")))):
            emit("done", "ok", "Panel is ready. Your custom starter was left unchanged and was not started.",
                 dashboard=dashboard)
            return 0
        profile, _, artifact, _ = active
        if (profile["device"] != "cpu" or profile["host"] != "127.0.0.1"
                or artifact["source"]["kind"] != "hf" or not artifact.get("hashes")):
            raise RuntimeError("The installed starter is not a pinned local CPU profile; reinstall the release.")
        emit(stage, "ok", f"Selected the bundled {MODEL}/{PROFILE} starter.")
        _inference_port(entry, profile)

        if entry["state"] != "ready":
            pending = next((job["id"] for job in status["jobs"]
                            if job["model"] == MODEL and job["profile"] == PROFILE
                            and job["action"] == "start" and job["state"] in {"queued", "running"}), None)
            if not pending:
                stage = "host"
                approved = profile["runtime"] in policy["runtimes"]
                if alternate_port and (not approved or not entry["controllable"]):
                    raise RuntimeError("The override panel must already have the starter CPU runtime approved and loaded; "
                                       "onboarding will not restart the default systemd service for LMH_ENDPOINT.")
                _preflight(status, profile, artifact, data, state, install=not approved)
                emit(stage, "ok", "CPU, available memory and disk checked.")
                if not approved:
                    stage = "runtime"
                    emit(stage, "working", "Downloading and verifying the approved llama.cpp CPU runtime.")
                    install_cpu_runtime(data, policy_path)
                if not entry["controllable"]:
                    stage = "runtime"
                    emit(stage, "working", "Reloading policy by restarting only the panel service.")
                    _restart(state)
                    status = _panel_status(backend, dashboard)
                    entry = _entry(status)
                    if entry is None or not entry["controllable"] or active != _recipe(_load_active(config, state, status)):
                        raise RuntimeError(f"The approved starter was not loaded; {SERVICE_HINT}.")
                    _inference_port(entry, profile)
            stage = "start"
            if entry["state"] != "ready" or pending:
                if not pending:
                    emit(stage, "working", f"Starting {MODEL}/{PROFILE} without stopping any model.")
                    try:
                        pending = backend.action("start", MODEL, PROFILE)["id"]
                    except BackendError as exc:
                        raise RuntimeError(f"Could not submit the starter start: {exc}; {START_HINT}.") from None
                _inference_port(_wait_for_model(backend, pending, emit), profile)
            emit(stage, "ok", "Starter is ready.")
        else:
            emit("start", "ok", "Starter is already ready.")

        stage = "smoke"
        emit(stage, "working", "Checking a local chat completion.")
        try:
            inference = _smoke(dashboard, token)
        except (BackendError, RuntimeError, TypeError, AttributeError, IndexError) as exc:
            raise RuntimeError(f"Chat smoke test failed: {exc}; {START_HINT}.") from None
        emit(stage, "ok", "Local chat completion succeeded.")
        emit("done", "ok", "LocalModelHub is ready.", dashboard=dashboard, inference=inference)
        return 0
    except (OSError, ValueError, RuntimeError, BackendError, KeyError) as exc:
        emit(stage, "error", str(exc))
        return 1
    except KeyboardInterrupt:
        emit(stage, "error", "Onboarding interrupted. Rerun onboarding to resume.")
        return 130


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jsonl", action="store_true", help="emit one JSON progress event per line")
    args = parser.parse_args(argv)
    os.umask(0o077)

    def emit(stage, state, message, **addresses):
        if args.jsonl:
            print(json.dumps(dict(stage=stage, state=state, message=message, **addresses)), flush=True)
        else:
            stream = sys.stderr if state == "error" else sys.stdout
            print(f"[{stage}] {message}", file=stream, flush=True)
            if addresses.get("dashboard"):
                print(f"Panel: {addresses['dashboard']}", flush=True)
            if addresses.get("inference"):
                print(f"Inference API: {addresses['inference']}", flush=True)

    return onboard(emit)


if __name__ == "__main__":
    sys.exit(main())
