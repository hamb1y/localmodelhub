# Build and test LocalModelHub

The app is installed from a Git checkout, not a release archive. Build and
test each part from the repository root:

```bash
cargo test --manifest-path terminal/Cargo.toml --locked
(cd web && bun install --frozen-lockfile && bun --bun run check && bun --bun run build)
```

With Chromium installed, `cd web && bun test` also runs the browser check.

## Run the Python tests

From a checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests
```

## Smoke-test a source install

From a clean checkout, install into a temporary home without enabling or
starting any service:

```bash
test_home=$(mktemp -d)
HOME="$test_home" bash deploy/install.sh --no-systemd
prefix="$test_home/.local/share/localmodelhub/current"
"$prefix/bin/lmh" --help
"$prefix/venv/bin/lmh-server" --help
test -s "$prefix/web/build/index.html"
```

The temporary-home smoke test doesn't exercise the one-step bootstrap. On a
disposable supported Linux host, test that the bootstrap installs the
dependencies, verifies rootless Podman, pulls the digest-pinned CPU image,
downloads and verifies the public starter, and succeeds at a real chat
request on port 5101. Check that an interactive
run opens the Ratatui UI and that a noninteractive run completes the same
setup in the logs. For an existing install, check that the custom catalog
is unchanged and running profiles are not stopped.
