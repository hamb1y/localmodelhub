"""Build a new app revision from the official Git main branch."""

import argparse
import fcntl
import os
import pwd
import re
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = "https://github.com/hamb1y/localmodelhub.git"
BRANCH = "main"
REF = f"refs/heads/{BRANCH}"


class UpdateError(Exception):
    pass


def git(*args, cwd=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0")
    return subprocess.check_output(
        ["git", "-c", "http.followRedirects=false", *args], cwd=cwd, env=env, timeout=120, text=True,
    ).strip()


def _commit(value):
    if not re.fullmatch(r"[a-f0-9]{40}", value):
        raise UpdateError("invalid Git revision")
    return value


def installed_commit(data):
    active = data / "current"
    if not (active / ".ready").is_file():
        raise UpdateError("LocalModelHub is not installed; run the public installer first")
    marker = active / "source-commit"
    return _commit(marker.read_text().strip()) if marker.is_file() else None


def update(data):
    if not data.is_dir():
        raise UpdateError("LocalModelHub is not installed; run the public installer first")
    with (data / "update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another LocalModelHub install or update is already running.")
            return
        current = installed_commit(data)
        advertised = git("ls-remote", "--exit-code", REPO, REF)
        revision, sep, ref = advertised.partition("\t")
        if not sep or ref != REF:
            raise UpdateError("could not identify the official Git main branch")
        revision = _commit(revision)
        if revision == current:
            print(f"LocalModelHub is already at {revision[:12]}.")
            return
        with tempfile.TemporaryDirectory(prefix=".update.", dir=data) as temp:
            checkout = Path(temp) / "source"
            git("clone", "--depth", "1", "--single-branch", "--branch", BRANCH, REPO, str(checkout))
            if _commit(git("rev-parse", "--verify", "HEAD", cwd=checkout)) != revision:
                raise UpdateError("main advanced during the download; retry the update")
            if installed_commit(data) != current:
                raise UpdateError("the active checkout changed; retry the update")
            result = subprocess.run(["bash", "deploy/install.sh"], cwd=checkout, check=False)
            if result.returncode:
                raise UpdateError(f"source install exited with status {result.returncode}; check the service logs")
            if installed_commit(data) != revision:
                raise UpdateError("source installer did not activate the requested commit")
        print(f"LocalModelHub updated to {revision[:12]}.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        account = pwd.getpwuid(os.geteuid())
        if account.pw_name != "ai" or account.pw_uid == 0:
            raise UpdateError("run as the dedicated ai account, not root")
        home = Path(account.pw_dir)
        if not home.is_absolute() or home == Path("/") or Path.home() != home:
            raise UpdateError("HOME must match the ai account's registered home directory")
        os.umask(0o077)
        update(home / ".local/share/localmodelhub")
    except (UpdateError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"LocalModelHub update: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
