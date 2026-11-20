"""Private local operations exposed by the native Rust terminal command."""

from __future__ import annotations

import argparse
import getpass
import os
import pwd
import subprocess
import sys
import warnings
from pathlib import Path


def _login() -> int:
    from huggingface_hub import get_token, login

    if get_token():
        print("Hugging Face credentials are already available to ai.")
        return 0
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print("Hugging Face sign-in requires an interactive terminal.", file=sys.stderr)
        return 1
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Hugging Face read token (hidden): ").strip()
        if not token:
            print("Sign-in skipped.")
            return 0
        login(token=token, add_to_git_credential=False)
    except Exception:
        print("Hugging Face sign-in failed. Check your token and network connection.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("update")
    commands.add_parser("hf-login")
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--purge", action="store_true")
    args = parser.parse_args(argv)
    if pwd.getpwuid(os.geteuid()).pw_name != "ai":
        parser.error("run LocalModelHub administration as the ai user")
    if args.command == "update":
        from .update import main as update

        return update([])
    if args.command == "hf-login":
        try:
            return _login()
        except (KeyboardInterrupt, EOFError):
            print("\nSign-in cancelled.", file=sys.stderr)
            return 130
    script = Path(sys.prefix).parent / "uninstall.sh"
    if not script.is_file():
        print("Uninstaller is available only from an installed LocalModelHub release.", file=sys.stderr)
        return 1
    return subprocess.run(["bash", str(script), *(["--purge"] if args.purge else [])]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
