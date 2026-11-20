# Build and test LocalModelHub

Build from a checkout. If `web/build/` already contains a complete static web
build, package it without invoking Node:

```bash
bash deploy/build-release.sh
```

To build the UI and package it, install Node 22.12+, npm, and Bun on the
build machine. The build installs from the checked-in Bun lockfile in frozen
mode:

```bash
bash deploy/build-release.sh --build-ui
```

Both commands produce `dist/localmodelhub.tar.gz` by default and print its
SHA-256. The archive includes the Python wheel, static web assets, example
catalogs, deployment files, and a checksum manifest. The target host needs
the archive and trusted checksum, a compatible Python, and access to Python
dependencies, but does not need a checkout, npm, or Node.js.

## Run the Python tests

From a checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests
```

## Smoke-test the release

Verify and extract the archive, change into the extracted `localmodelhub/`
directory, and install into a temporary home. This does not enable or start
any service:

```bash
test_home=$(mktemp -d)
HOME="$test_home" bash deploy/install.sh --no-systemd
prefix="$test_home/.local/share/localmodelhub/current"
"$prefix/venv/bin/lmh" --help
"$prefix/venv/bin/lmh-server" --help
test -s "$prefix/web/build/index.html"
```
