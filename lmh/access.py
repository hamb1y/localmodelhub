"""Manage inference-only credentials and Cloudflare Tunnel hostnames."""

import hashlib
import re
import secrets
import sys
from pathlib import Path

from .api import INFERENCE_PORT, inference_hostname, inference_policy
from .controller import _write_json
from .onboarding import _restart
from .runtime_install import _directory_lock, _private_parent, _read_policy


def _default_paths():
    home = Path.home()
    return (home / ".config/localmodelhub/policy.json", home / ".local/share/localmodelhub/state")


def configure(group, action, value=None, *, paths=None):
    path, state = paths or _default_paths()
    if group == "api-key" and action != "list" and not (
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value)
    ):
        raise ValueError("Choose a key name of 1–64 letters, digits, dots, dashes, or underscores.")
    if not state.is_dir():
        raise ValueError("LocalModelHub is not installed.")
    if group == "api-key" and action == "list":
        policy = _read_policy(path)
        inference_policy(policy.get("inference_keys"), policy.get("inference_public_hostname"))
        for name in sorted(policy.get("inference_keys") or {}):
            print(name)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private_parent(path.parent)
    key = None
    with _directory_lock(path.parent):
        policy = _read_policy(path)
        inference_policy(policy.get("inference_keys"), policy.get("inference_public_hostname"))
        if group == "api-key":
            keys = policy.setdefault("inference_keys", {})
            if action == "create":
                if value in keys:
                    raise ValueError("That key name already exists.")
                key = "lmh_" + secrets.token_urlsafe(32)
                keys[value] = hashlib.sha256(key.encode("ascii")).hexdigest()
            elif action == "revoke":
                if value not in keys:
                    raise ValueError("No key with that name.")
                del keys[value]
        elif action == "setup":
            value = inference_hostname(value)
            if not policy.get("inference_keys"):
                raise ValueError("Create an inference key first: lmh api-key create NAME")
            policy["inference_public_hostname"] = value
        else:
            policy.pop("inference_public_hostname", None)
        inference_policy(policy.get("inference_keys"), policy.get("inference_public_hostname"))
        _write_json(path, policy)
    if key:
        print(key, flush=True)
    try:
        _restart(state)
    except RuntimeError as exc:
        raise RuntimeError(f"Policy saved but not activated: {exc} Restart localmodelhub.service as ai.") from None
    if group == "cloudflare" and action == "setup":
        print(f"Cloudflare Tunnel → Published application route\n"
              f"  Public hostname:   {value}\n"
              f"  Service URL:       http://127.0.0.1:{INFERENCE_PORT}\n"
              f"  HTTP Host Header:  {value}\n"
              f"OpenAI base URL: https://{value}/v1 (use an LMH inference key)")
    elif key:
        print("Inference key created. Save it now; it cannot be shown again.", file=sys.stderr)
    else:
        print("Inference policy updated.")
