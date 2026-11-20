"""The native systemd user supervisor; no shell and no privilege escalation."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path


class SupervisorError(RuntimeError):
    pass


class Supervisor:
    def __init__(self, adopted: dict[str, str] | None = None):
        self.adopted = adopted or {}

    @staticmethod
    def unit_name(model: str, profile: str) -> str:
        suffix = hashlib.sha256(f"{model}\0{profile}".encode()).hexdigest()[:12]
        return f"lmh-{model[:30]}-{suffix}.service"

    @staticmethod
    def _call(argv: list[str], timeout: float = 15) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(argv, timeout=timeout, capture_output=True, text=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SupervisorError(f"{argv[0]}: {exc}") from None

    def _checked(self, argv: list[str], timeout: float = 15) -> str:
        result = self._call(argv, timeout)
        if result.returncode:
            raise SupervisorError((result.stderr or result.stdout).strip() or f"{argv[0]} failed")
        return result.stdout

    def _target(self, model: str, profile: str, runtime: dict) -> tuple[list[str], str]:
        if runtime["adapter"] == "systemd":
            unit = runtime.get("unit", "")
            if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", unit) or unit.startswith("-"):
                raise SupervisorError("Invalid adopted system unit name")
            return [], unit
        return ["--user"], self.unit_name(model, profile)

    def observe(self, model: str, profile: str, runtime: dict) -> dict:
        scope, unit = self._target(model, profile, runtime)
        result = self._call([
            "systemctl", *scope, "show", unit, "--property=LoadState,ActiveState,SubState,MainPID,Result",
        ], timeout=5)
        props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if not props or props.get("LoadState") == "not-found":
            if not props and result.returncode:
                return {"state": "unknown", "message": result.stderr.strip(), "pid": None}
            return {"state": "stopped" if scope else "unconfigured", "pid": None}
        state = {
            "active": "running", "activating": "starting", "deactivating": "stopping",
            "failed": "failed", "inactive": "stopped", "reloading": "running",
        }.get(props.get("ActiveState"), "unknown")
        return {
            "state": state, "pid": int(props.get("MainPID") or "0") or None,
            "message": (props.get("Result") or "Process failed") if state == "failed" else None,
        }

    def _adopted_action(self, action: str, unit: str) -> None:
        if self.adopted.get(unit) != "allowlisted":
            raise SupervisorError(f"{unit} is adopted in read-only mode")
        if self._checked(["systemctl", "show", unit, "-p", "User", "--value"]).strip() != "ai":
            raise SupervisorError(f"Refusing to control {unit}: its service does not run as ai")
        self._checked(["sudo", "-n", "/usr/local/sbin/ai-service-control", action, unit])

    def start(self, model: str, profile: str, runtime: dict, launch, options: dict) -> None:
        scope, unit = self._target(model, profile, runtime)
        if not scope:
            self._adopted_action("start", unit)
            return
        # A loaded failed transient unit must be reset before reusing its name.
        if self.observe(model, profile, runtime)["state"] == "failed":
            self._checked(["systemctl", "--user", "reset-failed", unit])
        if not launch or not Path(launch.argv[0]).is_absolute():
            raise SupervisorError("Native launch requires an absolute executable")
        argv = ["systemd-run", "--user", "--expand-environment=no", "--collect", f"--unit={unit}", "--property=Type=exec",
                "--property=KillMode=control-group", "--property=NoNewPrivileges=yes"]
        if launch.cwd:
            argv.append(f"--working-directory={launch.cwd}")
        resources = options.get("resources", {})
        policy = options.get("lifecycle", {})
        for field, prop in (("memory_high_mib", "MemoryHigh"), ("memory_max_mib", "MemoryMax"),
                            ("swap_max_mib", "MemorySwapMax")):
            if field in resources:
                argv.append(f"--property={prop}={int(resources[field])}M")
        argv.append(f"--property=TimeoutStopSec={int(policy.get('stop_timeout_seconds', 30))}")
        environment = []
        for key, value in sorted(launch.env.items()):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise SupervisorError("Invalid environment variable name")
            environment.append(f"{key}={value}")
        argv.extend(["--", "/usr/bin/env", "-i", *environment, *launch.argv])
        self._checked(argv)

    def stop(self, model: str, profile: str, runtime: dict) -> None:
        scope, unit = self._target(model, profile, runtime)
        if not scope:
            self._adopted_action("stop", unit)
            return
        self._checked(["systemctl", "--user", "stop", "--no-block", unit])

    def logs(self, model: str, profile: str, runtime: dict) -> list[str]:
        scope, unit = self._target(model, profile, runtime)
        argv = ["journalctl", "--user-unit" if scope else "-u", unit, "-n", "100",
                "--no-pager", "-o", "short-iso"]
        if not scope and self.adopted.get(unit) == "allowlisted":
            argv = ["sudo", "-n", "/usr/local/sbin/ai-service-control", "logs", unit]
        result = self._call(argv, timeout=8)
        return (result.stdout or result.stderr).splitlines()
