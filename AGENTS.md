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
- **Updates replace the app, not inference.** Build the official Git checkout
  before activating it. Preserve local config, jobs, caches, and running engine units.
  Uninstall stops only LocalModelHub-owned units; never remove adopted services
  or externally referenced model files.
- **Metrics are evidence.** Keep unreported values distinct from zero. Store
  only language counts by default. Store inference bodies only under the
  explicit local logging policy, and never capture authentication headers.

## Where to work

- **Lifecycle:** `lmh/controller.py`, `lmh/state.py`, `lmh/supervisor.py`.
- **Catalog and execution:** `lmh/catalog.py`, `lmh/artifacts.py`, and
  `lmh/engines.py`; add focused adapter tests before adding option translations.
- **Operations:** `lmh/api.py` is shared by SvelteKit `web/` and the
  Rust/Ratatui `terminal/`. Keep lifecycle behavior in the backend.
- **Onboarding:** `install.sh` and `lmh/onboarding.py` provision and test
  the starter through an additive Start. App updates never call onboarding.
- **Install from source:** `install.sh` and `lmh/update.py` fetch Git `main`;
  `deploy/install.sh` builds and activates the commit. Keep Python and Rust
  version strings in sync. Do not require GitHub release artifacts.

## Verify before shipping

1. Run `python -m pip install -e .` in a virtual environment, then
   `python -m unittest discover -s tests -v`. For lifecycle changes, assert what
   was **not** stopped as well as what started.
2. For web changes, run `bun install --frozen-lockfile`, `npm run check`,
   `npm run build`, and `bun test` in `web/`. Install Playwright Chromium when
   there is no local Chromium.
3. For terminal changes, run `cargo fmt --check`, `cargo clippy --locked
   --all-targets -- -D warnings`, and `cargo test --locked` in `terminal/`.
4. For install or update changes, test `bash deploy/install.sh --no-systemd`
   from a clean checkout and an isolated home. Never exercise
   destructive deployment paths against someone’s running inference without
   explicit approval.
