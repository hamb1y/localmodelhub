# Build and test LocalModelHub

Build from a checkout. A release includes a native Rust `lmh` CLI and
Ratatui terminal UI; use a Rust toolchain on the build machine and build for
the target Linux architecture (x86_64 or aarch64). If `web/build/` already
contains a complete static web build, package it without invoking Node:

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
SHA-256. A release needs the Python backend, native `lmh` executable, static
web assets, catalogs, deployment files, and a checksum manifest. The
target host needs neither a checkout nor a Node.js or Rust toolchain.

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
"$prefix/bin/lmh" --help
"$prefix/venv/bin/lmh-server" --help
test -s "$prefix/web/build/index.html"
```

The archive smoke test does not exercise the one-step bootstrap. On a
disposable supported Linux host, test that the bootstrap installs the
dependencies and CPU runtime, downloads and verifies the public starter,
and succeeds at a real chat request on port 8081. Check that an interactive
run opens the Ratatui UI and that a noninteractive run completes the same
setup in the logs. For an existing install, check that the custom catalog
is unchanged and running profiles are not stopped.
