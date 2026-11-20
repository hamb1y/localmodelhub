"""Native LocalModelHub CLI and shared HTTP client for the terminal UI."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import platform
import shutil
import subprocess
import sys
import warnings
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEFAULT_ENDPOINT = "http://localhost:5189"


class BackendError(Exception):
    """The local backend could not complete a request."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Profile:
    model: str
    name: str
    id: str
    device: str
    state: str
    port: int | None
    message: str
    controllable: bool = True

    @property
    def label(self) -> str:
        return f"{self.model}/{self.id}"

    @property
    def key(self) -> str:
        return json.dumps([self.model, self.id])


def profiles(status: dict[str, Any]) -> list[Profile]:
    return [
        Profile(
            model=model["id"],
            name=model.get("name") or model["id"],
            id=profile["id"],
            device=profile.get("device") or "—",
            state=profile.get("state") or "unknown",
            port=profile.get("port"),
            message=profile.get("message") or "",
            controllable=profile.get("controllable") is not False,
        )
        for model in status.get("models", [])
        for profile in model.get("profiles", [])
    ]


class Backend:
    def __init__(
        self, endpoint: str = DEFAULT_ENDPOINT, timeout: float = 8, *, token: str | None = None,
    ) -> None:
        try:
            url = urlsplit(endpoint)
            valid = (
                url.scheme in {"http", "https"} and url.hostname
                and url.username is None and url.password is None
                and not url.query and not url.fragment
                and not any(char.isspace() or not char.isprintable() for char in endpoint)
            )
            # Accessing port validates the syntax and range.
            url.port
        except ValueError:
            valid = False
        if not valid:
            raise BackendError("Endpoint must be an HTTP(S) URL without credentials, query or fragment.")
        if token and url.scheme == "http":
            host = url.hostname
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == "localhost"
            if not loopback:
                raise BackendError("LMH_API_TOKEN requires HTTPS or a loopback endpoint.")
        if token and (not token.isascii() or not token.isprintable() or any(c.isspace() for c in token)):
            raise BackendError("LMH_API_TOKEN must be an ASCII bearer token without whitespace.")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self._token = token

    def _request(self, path: str, payload: dict | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.endpoint + path, data=data, headers=headers)
        try:
            # Local service requests should not be sent through an environment proxy.
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            with exc:
                try:
                    body = json.load(exc)
                except (ValueError, OSError):
                    body = None
            detail = None
            if isinstance(body, dict):
                detail = body.get("message") or body.get("error") or body.get("detail")
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("detail")
            raise BackendError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        except (URLError, OSError, HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            raise BackendError(f"Cannot reach {self.endpoint}: {reason}") from exc
        except ValueError as exc:
            raise BackendError("Backend returned invalid JSON.") from exc
        if not isinstance(result, dict):
            raise BackendError("Backend returned an unexpected response.")
        return result

    def status(self) -> dict[str, Any]:
        return self._request("/api/status")

    def stats(self) -> dict[str, Any]:
        return self._request("/api/stats")

    def logs(self, model: str, profile: str) -> list[str]:
        query = urlencode({"model": model, "profile": profile})
        response = self._request(f"/api/logs?{query}")
        lines = response.get("lines")
        if not isinstance(lines, list):
            raise BackendError("Backend did not return log lines.")
        return lines

    def action(
        self, action: str, model: str, profile: str, *, source: Profile | None = None,
    ) -> dict[str, Any]:
        payload = {"action": action, "model": model, "profile": profile}
        if source is not None:
            payload.update(from_model=source.model, from_profile=source.id)
        return self._request("/api/actions", payload)

    def preview(self, *, url: str | None = None, catalog: Any = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if url is not None:
            payload["url"] = url
        if catalog is not None:
            payload["catalog"] = catalog
        return self._request("/api/catalog/preview", payload)

    def stage(self, catalog: dict[str, Any]) -> dict[str, Any]:
        return self._request("/api/catalog/stage", {"catalog": catalog})


def size(value: Any) -> str:
    if not isinstance(value, (float, int)) or value < 0:
        return "unknown"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "unknown"


def hardware_lines(hardware: dict[str, Any]) -> list[str]:
    lines = [
        f"CPU: {hardware.get('cpu') or 'unknown'}",
        f"RAM: {size(hardware.get('ram_available_bytes'))} available / "
        f"{size(hardware.get('ram_total_bytes'))} total",
    ]
    gpus = hardware.get("gpus") or []
    for gpu in gpus:
        lines.append(
            f"GPU {gpu.get('id', '?')}: {gpu.get('name') or 'unknown'} — "
            f"{size(gpu.get('free_bytes'))} free / {size(gpu.get('total_bytes'))} total"
        )
    if not gpus:
        lines.append("GPU: none reported")
    return lines


def stats_lines(stats: dict[str, Any]) -> list[str]:
    def reported(values: dict, fields: tuple) -> str:
        return ", ".join(
            f"{label}: {form.format(values[key])}"
            for key, label, form in fields
            if type(values.get(key)) in (int, float)
        )

    lines = ["Memory usage (latest sample):"]
    samples = stats.get("samples") or []
    if not samples:
        lines.append("  No memory samples reported.")
    else:
        latest = samples[-1]
        lines.append(f"  RAM: {size(latest.get('ram_used_bytes'))}; VRAM: {size(latest.get('vram_used_bytes'))}")

    lines.extend(("", "Lifecycle jobs (last 24 hours):"))
    jobs = stats.get("jobs_by_model") or []
    if not jobs:
        lines.append("  No finished jobs reported.")
    for job in jobs:
        fields = (("finished", "finished", "{:,}"), ("failed", "failed", "{:,}"),
                  ("start_seconds", "average successful start/switch", "{:.2f} s"))
        lines.append(f"  {job.get('model', '—')}: {reported(job, fields) or 'not reported'}")

    lines.extend(("", "Inference counters:"))
    fields = (
        ("requests", "requests", "{:,}"),
        ("requests_per_second", "requests/s", "{:.2f}"),
        ("tokens", "tokens", "{:,}"),
        ("prompt_tokens", "prompt tokens", "{:,}"),
        ("generation_tokens", "generated tokens", "{:,}"),
        ("cached_prompt_tokens", "cached prompt tokens", "{:,}"),
        ("prompt_cache_hit_rate", "prompt cache hit rate", "{:.1%}"),
        ("expert_cache_hit_rate", "expert cache hit rate", "{:.1%}"),
        ("inference_error_rate", "inference error rate", "{:.1%}"),
    )
    aggregate = reported(stats.get("metrics") or {}, fields)
    outputs = [f"  Total: {aggregate}"] if aggregate else []
    for profile in stats.get("profiles") or []:
        values = reported(profile, fields)
        if values:
            outputs.append(f"  {profile.get('model', '—')}/{profile.get('profile', '—')}: {values}")
    lines.extend(outputs or ["  No inference counters reported."])
    return lines


def local_hardware() -> tuple[dict[str, Any], bool]:
    try:
        from lmh.hardware import snapshot

        result = snapshot()
        if isinstance(result, dict):
            return result, False
    except Exception:
        # A missing or failed hardware probe must not prevent offline diagnostics.
        pass
    result = {"cpu": platform.processor() or platform.machine(), "gpus": []}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            field = {"MemTotal": "ram_total_bytes", "MemAvailable": "ram_available_bytes"}.get(key)
            if field:
                result[field] = int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return result, True


def boot_line(status: dict[str, Any]) -> str:
    boot = status.get("boot") or {}
    state, message = boot.get("state") or "unknown", boot.get("message")
    return f"Boot: {state}" + (f" — {message}" if message else "")


def _user() -> str:
    # The native account, not a caller-controlled USER/LOGNAME variable.
    import pwd

    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


def _xdg_directory(variable: str, fallback: str) -> Path:
    return Path(os.environ.get(variable, str(Path.home() / fallback))) / "localmodelhub"


def _default_disk_path() -> Path:
    configured = _xdg_directory("XDG_DATA_HOME", ".local/share").absolute()
    return next((path for path in (configured, *configured.parents) if path.is_dir()), Path.home())


def _install_cpu_runtime() -> None:
    if _user() != "ai":
        raise BackendError("Install the optional CPU runtime as the native ai user, not as root or another user.")
    from lmh.runtime_install import install_cpu_runtime

    data_dir = _xdg_directory("XDG_DATA_HOME", ".local/share")
    policy_path = _xdg_directory("XDG_CONFIG_HOME", ".config") / "policy.json"
    try:
        runtime_path = install_cpu_runtime(data_dir, policy_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise BackendError(f"CPU runtime install failed: {exc}") from exc
    print(f"CPU runtime: {runtime_path}")
    print(f"Runtime policy: {policy_path}")
    print(
        "If LocalModelHub is already running, use `systemctl --user restart localmodelhub.service` "
        "as ai to read the new policy. A restart does not repeat reconciliation within the same OS boot."
    )
    if policy_path != Path.home() / ".config/localmodelhub/policy.json":
        print("With custom XDG paths, ensure the LocalModelHub user service reads the policy file shown above.")


def _hf_setup(want_login: bool | None) -> bool:
    try:
        from huggingface_hub import get_token, login
    except ImportError:
        print("Hugging Face: client package unavailable; public models do not require login.")
        return want_login is not True
    try:
        existing = bool(get_token())
    except OSError:
        print("Hugging Face: could not read existing credentials; no changes made.")
        return want_login is not True
    if existing:
        print("Hugging Face: existing credentials found for this account or environment; left unchanged.")
        return True
    print("Hugging Face: no credentials found. Login is optional for public models; "
          "a token can grant access to private or gated models you are authorized to use.")
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if want_login is False or (want_login is None and not interactive):
        return True
    if _user() != "ai":
        print("To save credentials for LocalModelHub, run `lmh onboard --hf-login` as the native ai user.")
        return want_login is not True
    if not interactive:
        print("Hugging Face login requires an interactive terminal; no token was read.")
        return False
    if want_login is None and input("Log in to Hugging Face now? [y/N] ").strip().lower() not in {"y", "yes"}:
        return True
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Hugging Face token (hidden): ").strip()
        if not token:
            print("No token entered; skipped Hugging Face login.")
            return want_login is not True
        login(token=token, add_to_git_credential=False)
    except getpass.GetPassWarning:
        print("Terminal input could not be hidden; no token was read.")
        return False
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:
        # Avoid echoing a token via a library exception or HTTP response.
        print("Hugging Face login failed. Check the token and network connection, then retry.")
        return False
    print("Hugging Face login saved for the native ai user.")
    return True


def _diagnose(backend: Backend, disk_path: Path) -> tuple[dict[str, Any] | None, bool]:
    user = _user()
    print(f"Native account: {user}" + (" (expected)" if user == "ai" else " (LocalModelHub runs as ai)"))
    try:
        status = backend.status()
    except BackendError as exc:
        print(f"Backend: unavailable ({exc})")
        status = None
    else:
        print(f"Backend: reachable at {backend.endpoint}")
        print(boot_line(status))
    if status and status.get("hardware"):
        print("Device (backend):")
        hardware = status["hardware"]
    else:
        hardware, limited = local_hardware()
        print("Device (local, limited fallback):" if limited else "Device (local):")
    for line in hardware_lines(hardware):
        print(f"  {line}")
    try:
        disk = shutil.disk_usage(disk_path)
    except OSError as exc:
        print(f"Disk ({disk_path}): unavailable ({exc.strerror or exc})")
        return status, False
    print(f"Disk ({disk_path}): {size(disk.free)} free / {size(disk.total)} total")
    return status, True


def _print_profiles(status: dict[str, Any]) -> None:
    entries = profiles(status)
    if not entries:
        print("No model profiles registered.")
        return
    defaults = {model["id"]: model.get("default_profile") for model in status.get("models", [])}
    for entry in entries:
        default = " (default)" if defaults.get(entry.model) == entry.id else ""
        port = f" :{entry.port}" if entry.port is not None else ""
        detail = f" — {entry.message}" if entry.message else ""
        blocked = "" if entry.controllable else " (control blocked)"
        print(f"{entry.label}{default}  {entry.name}  [{entry.device}] {entry.state}{port}{blocked}{detail}")


def _profile_id(backend: Backend, model_id: str, profile_id: str | None) -> str:
    if profile_id:
        return profile_id
    for model in backend.status().get("models", []):
        if model["id"] != model_id:
            continue
        if model.get("default_profile"):
            return model["default_profile"]
        entries = model.get("profiles") or []
        if len(entries) == 1:
            return entries[0]["id"]
        raise BackendError(f"Model {model_id!r} has no default profile; specify PROFILE (see `lmh list`).")
    raise BackendError(f"Unknown model {model_id!r}; see `lmh list`.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lmh", description="Native LocalModelHub control and onboarding.",
        epilog="If your panel requires API authentication, set LMH_API_TOKEN (not a webhook token).",
    )
    parser.add_argument(
        "--endpoint", default=os.environ.get("LMH_ENDPOINT", DEFAULT_ENDPOINT),
        help=f"backend URL (default: {DEFAULT_ENDPOINT}; override with LMH_ENDPOINT)",
    )
    commands = parser.add_subparsers(dest="command")
    for command, help_text in (
        ("onboard", "inspect this machine and get started"),
        ("doctor", "check backend, boot and hardware"),
    ):
        diagnostic = commands.add_parser(command, help=help_text)
        diagnostic.add_argument(
            "--disk-path", type=lambda value: Path(value).expanduser(),
            help="filesystem to check, preferably your model storage path (default: LocalModelHub's data filesystem)",
        )
        if command == "onboard":
            diagnostic.add_argument(
                "--hf-login", action=argparse.BooleanOptionalAction,
                help="log in as ai now, or skip the optional interactive prompt",
            )
            diagnostic.add_argument(
                "--install-cpu-runtime", action="store_true",
                help="as ai, explicitly install the pinned llama.cpp CPU build and register it in local policy",
            )
    commands.add_parser("list", help="list registered model profiles")
    commands.add_parser("jobs", help="show backend jobs")
    commands.add_parser("tui", help="open the interactive terminal UI, including manual switching")
    stats = commands.add_parser("stats", help="show memory usage, lifecycle jobs and reported inference counters")
    stats.add_argument("--json", action="store_true", help="print the raw statistics response as JSON")
    for command, help_text in (
        ("start", "request that the backend start a profile"),
        ("stop", "request that the backend stop a profile"),
        ("logs", "show logs for a profile"),
    ):
        profile = commands.add_parser(command, help=help_text)
        profile.add_argument("model", metavar="MODEL")
        profile.add_argument("profile", metavar="PROFILE", nargs="?", help="defaults to the model's default profile")
    preview = commands.add_parser("preview", help="preview a catalog without installing or starting models")
    source = preview.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="public HTTPS catalog URL for the backend to preview")
    source.add_argument("--catalog", type=Path, help="local JSON catalog file")
    stage = commands.add_parser("stage", help="stage a reviewed catalog for the next OS boot; no immediate changes")
    stage.add_argument("--catalog", type=Path, required=True, help="reviewed local JSON catalog file")
    commands.add_parser("update", help="check for and install a newer stable LocalModelHub release",
                        description="Check for and install a newer stable LocalModelHub release.")
    uninstall = commands.add_parser("uninstall", help="remove LocalModelHub from the ai account")
    uninstall.add_argument("--purge", action="store_true", help="also remove LocalModelHub config, history and cache")
    return parser


def _read_catalog(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BackendError("Catalog file contains a duplicate JSON key.")
            result[key] = value
        return result

    with path.expanduser().open(encoding="utf-8") as file:
        data = json.load(file, object_pairs_hook=unique)
    if not isinstance(data, dict):
        raise BackendError("Catalog file must contain a JSON object.")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        if args.command == "update":
            from lmh.update import main as update

            return update([])
        if args.command == "uninstall":
            script = Path(sys.prefix).parent / "uninstall.sh"
            if not script.is_file():
                raise BackendError("Uninstaller is available from an installed LocalModelHub release.")
            return subprocess.run(["bash", str(script), *(["--purge"] if args.purge else [])]).returncode
        backend = Backend(args.endpoint, token=os.environ.get("LMH_API_TOKEN"))
        if args.command in {"onboard", "doctor"}:
            status, disk_ok = _diagnose(backend, args.disk_path or _default_disk_path())
            if args.command == "doctor":
                boot = (status or {}).get("boot") or {}
                return 0 if status is not None and disk_ok and boot.get("state") != "failed" else 1
            if args.install_cpu_runtime:
                _install_cpu_runtime()
            login_ok = _hf_setup(args.hf_login)
            print("\nStarter profiles:")
            _print_profiles(status or {})
            print(
                "\nStart with a small model. For a CPU profile, check available RAM; for a GPU profile, "
                "check free VRAM on that GPU. Leave room for runtime overhead and check disk space "
                "on the filesystem where models are stored.\n"
                "From the unpacked release, inspect the starter with "
                "`lmh preview --catalog catalogs/starter.json`. Use `--catalog FILE` or "
                "`--url HTTPS_URL` for a custom catalog. Preview does not install or apply it.\n"
                "To install the optional pinned llama.cpp CPU build, run "
                "`lmh onboard --install-cpu-runtime` as ai.\n"
                "Use `lmh tui` to monitor profiles, view logs, and confirm a manual switch. "
                "To start one profile explicitly: `lmh start MODEL PROFILE`."
            )
            return 0 if login_ok else 1
        if args.command == "list":
            _print_profiles(backend.status())
        elif args.command == "stats":
            report = backend.stats()
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                try:
                    hardware = backend.status().get("hardware")
                except BackendError:
                    hardware = None
                if hardware:
                    print("\n".join(hardware_lines(hardware)), end="\n\n")
                print("\n".join(stats_lines(report)))
        elif args.command == "jobs":
            jobs = backend.status().get("jobs", [])
            if not jobs:
                print("No jobs.")
            for job in jobs:
                print(
                    f"{job.get('id', '—')}  {job.get('action', '—')} "
                    f"{job.get('model', '—')}/{job.get('profile', '—')}  {job.get('state', '—')}  "
                    f"{job.get('created_at', '—')} [{job.get('source', '—')}] "
                    f"{job.get('message') or ''}".rstrip()
                )
        elif args.command in {"start", "stop", "logs"}:
            profile = _profile_id(backend, args.model, args.profile)
            if args.command == "logs":
                for line in backend.logs(args.model, profile):
                    print(line)
            else:
                print(json.dumps(backend.action(args.command, args.model, profile), indent=2))
        elif args.command == "preview":
            catalog = _read_catalog(args.catalog) if args.catalog is not None else None
            print(json.dumps(backend.preview(url=args.url, catalog=catalog), indent=2))
        elif args.command == "stage":
            result = backend.stage(_read_catalog(args.catalog))
            print(result.get("message") or "Catalog staged for the next OS boot.")
        elif args.command == "tui":
            try:
                from lmh.tui import LocalModelHubApp
            except ModuleNotFoundError as exc:
                if exc.name != "textual":
                    raise
                raise BackendError("Textual is not installed; reinstall LocalModelHub with its dependencies.") from exc
            LocalModelHubApp(backend).run()
    except (BackendError, OSError, ValueError) as exc:
        print(f"lmh: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nlmh: cancelled", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
