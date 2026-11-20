"""Native LocalModelHub daemon."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import signal
import sys
import threading
from pathlib import Path

from .api import make_server
from .controller import Controller


def _xdg(variable: str, fallback: str) -> Path:
    return Path(os.environ.get(variable, str(Path.home() / fallback))) / "localmodelhub"


def _policy(path: Path) -> dict:
    if not path.exists():
        return {}
    policy = json.loads(path.read_text())
    if not isinstance(policy, dict):
        raise ValueError("Local policy must be a JSON object")
    if path.stat().st_mode & 0o077:
        raise ValueError(f"Local policy must not be readable by other users: chmod 600 {path}")
    for key in ("runtimes", "adopted", "webhooks"):
        if key in policy and not isinstance(policy[key], dict):
            raise ValueError(f"Local policy {key!r} must be an object")
    if "api_token" in policy and (not isinstance(policy["api_token"], str) or len(policy["api_token"]) < 24):
        raise ValueError("api_token must be an unguessable string of at least 24 characters")
    if not isinstance(policy.get("inference_logging", False), bool):
        raise ValueError("inference_logging must be true or false")
    budget = policy.get("gpu_headroom_mib", 512)
    if type(budget) is not int or not 0 <= budget <= 16384:
        raise ValueError("gpu_headroom_mib must be an integer from 0 to 16384")
    return policy


def main(argv: list[str] | None = None) -> int:
    config = _xdg("XDG_CONFIG_HOME", ".config")
    data = _xdg("XDG_DATA_HOME", ".local/share")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=config / "models.json", type=Path)
    parser.add_argument("--policy", default=config / "policy.json", type=Path)
    parser.add_argument("--state-dir", default=data / "state", type=Path)
    parser.add_argument("--ui-dir", default=data / "ui", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=5189, type=int)
    args = parser.parse_args(argv)
    if pwd.getpwuid(os.geteuid()).pw_name != "ai":
        parser.error("run the daemon as the dedicated Linux user ai")
    os.umask(0o077)
    try:
        policy = _policy(args.policy)
        args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (args.state_dir / "controller.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another LocalModelHub process owns this state directory") from None
            controller = Controller(args.catalog, args.state_dir, policy)
            server = make_server(controller, host=args.host, port=args.port, ui_dir=args.ui_dir,
                                 api_token=policy.get("api_token"), webhooks=policy.get("webhooks"),
                                 inference_log_dir=(args.state_dir / "inference-logs"
                                                    if policy.get("inference_logging") else None))
            with server:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
                controller.start()
                print(f"LocalModelHub listening on {args.host}:{server.server_port} as ai", flush=True)
                try:
                    server.serve_forever()
                finally:
                    controller.shutdown()
    except (OSError, ValueError) as exc:
        print(f"LocalModelHub: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
