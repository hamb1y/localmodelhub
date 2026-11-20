# Working on LocalModelHub

LocalModelHub manages **inference only**, natively on Linux. The daemon and its
managed engines run under the dedicated `ai` account; the web UI is static at
runtime. See [README.md](README.md) for the user workflow.

## Lifecycle invariants

- **Boot is reconciliation.** Validate the entire catalog before starting
  anything. Reconcile only missing boot actions; retain the saved snapshot for
  the rest of that Linux boot. Restarting or updating the daemon is not a new boot.
- **Start is additive.** Check available resources on explicit Start and
  report conflicts without stopping any profile. Replacement requires an
  explicit panel/TUI Switch or an authorized webhook naming the exact source.
  No background task, health check, or inference request switches models.
- **Imports are data.** Keep runtime approvals, local paths, and credentials
  outside portable imports. Check pinned artifact bytes against declared
  digests. An imported catalog can be staged, not executed immediately.
- **Updates replace the app, not inference.** Ship stable, verified release
  artifacts. Preserve local config, jobs, caches, and running engine units.
  Uninstall stops only LocalModelHub-owned units; never remove adopted services
  or externally referenced model files.
- **Metrics are evidence.** Keep unreported values distinct from zero. Do not
  store prompts, responses, or tokens used for authentication.

## Where to work

- **Lifecycle:** `lmh/controller.py`, `lmh/state.py`, `lmh/supervisor.py`.
- **Catalog and execution:** `lmh/catalog.py`, `lmh/artifacts.py`, and
  `lmh/engines.py`; add focused adapter tests before adding option translations.
- **Operations:** `lmh/api.py` is shared by the SvelteKit `web/` UI and the
  Python CLI/Textual TUI. Keep behavior in the backend, not the clients.
- **Releases:** the root `install.sh` is the public entry point;
  `deploy/` packages the verified application and systemd files. Keep
  `pyproject.toml`, `lmh/__init__.py`, and the stable release tag in sync.

## Verify before shipping

1. Run `python -m pip install -e .` in a virtual environment, then
   `python -m unittest discover -s tests -v`. For lifecycle changes, assert what
   was **not** stopped as well as what started.
2. For web changes, run `bun install --frozen-lockfile`, `npm run check`,
   `npm run build`, and `bun test` in `web/`. Install Playwright Chromium when
   there is no local Chromium.
3. For install or update changes, build `bash deploy/build-release.sh --build-ui`
   and test in an isolated home or with a systemd test double. Never exercise
   destructive deployment paths against someone’s running inference without
   explicit approval.
