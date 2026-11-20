# Configuration and operations

For the normal install, start with the [README](../README.md). The commands
below assume the service runs under the dedicated, unprivileged `ai` account.

## Sign in as `ai`

Use a normal login or `sudo -iu ai` from an administrator shell. If `lmh` is
not on your `PATH`, add the installed CLI for the current shell:

```bash
export PATH="$HOME/.local/share/localmodelhub/current/venv/bin:$PATH"
```

If `systemctl --user` cannot connect after `sudo -iu ai`, set the user-bus
environment inside the `ai` shell:

```bash
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
systemctl --user show-environment >/dev/null
```

## Manual release install

The host needs systemd 254+, Python 3.11+, Bash, `tar`, coreutils and `flock`.
Managed runtime units use `systemd-run --expand-environment=no` so systemd
does not expand model arguments. Install Python's `venv`/`ensurepip` support
(for example `python3-venv` on Debian/Ubuntu), or install `uv` alongside
Python. The installer uses `uv` if present, otherwise `venv` and `pip`.
Dependencies come from your Python package index or configured mirror.

If you aren't using the quick-start installer, prepare the account as an
administrator:

```bash
id ai >/dev/null 2>&1 || sudo useradd --create-home --shell /bin/bash ai
sudo loginctl enable-linger ai
```

Linger starts the user manager at boot and keeps it running after logout.
Give `ai` only the access needed to read model files, run approved binaries,
and use configured devices. Do not give the service account root privileges.

Get `localmodelhub.tar.gz` from the
[official releases](https://github.com/hamb1y/localmodelhub/releases) and
verify its SHA-256 against the trusted release checksum before unpacking.
As `ai`, in a directory writable by that account:

```bash
tar -xzf localmodelhub.tar.gz
cd localmodelhub
bash deploy/install.sh
curl --fail --retry 5 --retry-connrefused http://127.0.0.1:5189/api/health
```

You can remove the downloaded archive after installation.

On first install, the starter catalog provides profiles but has no boot
actions. Pass `--catalog FILE` to seed a reviewed local catalog instead, or
`--catalog deploy/example-catalog.json` for an empty panel. Reinstalling
does not overwrite the existing catalog, credentials, policy, unit drop-ins,
or application state. A normal install enables the user service and restarts
it when activating a different release. For staging checks in a disposable
home, `--no-systemd` installs files without talking to the service manager.

| Purpose | Default path under `ai`’s home |
| --- | --- |
| Catalog | `.config/localmodelhub/models.json` |
| Optional policy | `.config/localmodelhub/policy.json` |
| Optional environment file | `.config/localmodelhub/service.env` |
| User unit | `.config/systemd/user/localmodelhub.service` |
| Persistent state | `.local/share/localmodelhub/state/` |
| Active release | `.local/share/localmodelhub/current/` |

## Catalogs

Use `lmh preview --catalog FILE` to inspect a catalog without applying it,
downloading models, or starting inference. From an unpacked release, see
`catalogs/starter.json` for pinned public artifacts and
`catalogs/models.example.json` for local examples.

A catalog is UTF-8 JSON with `schema_version: 1` and named maps `runtimes`,
`artifacts`, and `models`. Profiles choose a runtime, `cpu` or `gpu`, and
artifacts by ID. An artifact role can map to one ID or an ordered list of
shards. Hugging Face artifacts specify a repository, a full 40-character
commit and an exact repository-relative filename. Record `size` in bytes and
`hashes` (`sha256` and/or `blake3`) when available. A local artifact requires
an absolute path. Keep credentials, machine-specific paths, and executable
paths out of shared catalogs.

Boot status reports a BLAKE3 fingerprint of the accepted catalog, independent
of whitespace and key order. Checksums verify file integrity; they are not a
substitute for reviewing a catalog or trusting its publisher.

After setup, edits to the local catalog take effect at the next Linux boot.
The panel, or `lmh stage --catalog FILE`, can explicitly stage a portable catalog
for that boot. Staging accepts typed adapters and pinned Hugging Face files
with a hash and size, but not host-only paths or arbitrary runtime arguments.
The staged catalog takes priority at the next boot and its submitted `boot`
list is cleared. If you want boot actions, configure them locally.

## Runtime policy

Local policy controls which native executables may run. Approve only trusted
binaries, matching the runtime ID and adapter from the catalog. Create
`~/.config/localmodelhub/policy.json` as `ai` with mode `0600` (`umask 077`
before creating it). For example:

```json
{
  "runtimes": {
    "llama-cpu": {
      "adapter": "llama.cpp",
      "executable": "/opt/llama.cpp/bin/llama-server",
      "version": "b12359"
    }
  }
}
```

Use the actual absolute path, and match any version or checksum pinned by the
catalog. Restart the service to load policy changes. The optional
`lmh onboard --install-cpu-runtime` downloads and verifies the pinned
llama.cpp CPU build (`b12359`) and adds its policy entry for you. This does
not download model weights.

For the starter NVIDIA image profile, install your NVIDIA drivers and a
compatible CUDA build of `sd-server` separately. Approve it under `sd-cuda`
with adapter `stable-diffusion.cpp`, the actual absolute executable path, and
the version pinned in `catalogs/starter.json`. Make sure `ai` can access the
GPU and that enough VRAM and model storage are available.

Locally configured native commands and adopted systemd units are not portable
imports. Adopted system units are read-only by default; an `"allowlisted"`
policy entry does not itself grant control. A privileged integration, if
needed, requires a separately reviewed, root-owned `ai-service-control`
wrapper, sudoers entries restricted to exact units and actions, and an
administrator-approved user-service drop-in allowing privilege elevation.
Never grant unrestricted `sudo` or `systemctl` rights. This is not part of the
normal install. Managed inference units retain `NoNewPrivileges=true`.

## Credentials and remote access

Public Hugging Face downloads do not require a token. For gated or private
models, obtain access upstream and then run `lmh onboard --hf-login` as `ai`
with a minimally scoped read token. It hides token input and leaves existing
credentials in place. The service and onboarding must use the same `HOME`
and, if overridden, the same `HF_HOME`. Never put tokens in a catalog,
command-line argument, or Git. Use mode `0600` for `service.env` if you use it.

Keep the panel and API on loopback. To reach the panel from another machine,
use an SSH tunnel through an account authorized on the host:

```bash
ssh -N -L 5189:127.0.0.1:5189 your-user@your-host
```

Loopback is not a per-user security boundary on a shared host. A Hugging Face
token does not authenticate panel users. Only allow trusted operators to use
the panel; do not expose the backend directly.

The policy's `api_token` protects the ordinary API. Set it in the panel's
Settings or provide it to the CLI/TUI with `LMH_API_TOKEN` in a private
environment. The separate `webhooks` policy object can give each automation
caller its own token, allowed `actions` and allowed `models`. A caller sends
`Authorization: Bearer …` to `POST /api/webhooks/action` with `action`,
`model`, and `profile`. A `switch` also needs `from_model` and `from_profile`,
and both model IDs must be in scope. Use independent random secrets for
callers and the ordinary API; store them privately and rotate exposed tokens.

## Boot, model switching, and diagnostics

The panel starts independently of background boot reconciliation. Boot
actions run at most once per Linux boot for the same state directory.
Restarting or upgrading the service does not run them again. Keep `boot`
empty for no automatic model starts. To opt in to a specific profile, add
`"boot": [{"model": "qwen2.5-chat", "profile": "cpu"}]` to your local catalog.
An already active profile is left alone. If it conflicts with another running
profile, the boot job reports the conflict rather than switching. A model’s
`default_profile` is not a boot instruction. Switch explicitly in the panel
or TUI, or through an authorized webhook.

The `ram_mib` and `vram_mib` settings are estimates. GPU coexistence requires
measured free VRAM and known budgets for other managed GPU profiles on the
same card. Use a CUDA UUID to select a specific GPU. The optional systemd
limits `resources.memory_high_mib`, `resources.memory_max_mib`, and
`resources.swap_max_mib` are separate from those estimates. Rates are based
on runtime-reported counters or measurements and remain unavailable if
there is no usable measurement.

Use `lmh doctor`, `lmh jobs`, `lmh stats`, and `lmh logs --help` to diagnose
issues. If the panel is unreachable, check the user-service journal and
verify that `ai` can read the catalog and access the runtime and model files.

```bash
systemctl --user status localmodelhub.service
journalctl --user -u localmodelhub.service -f
systemctl --user restart localmodelhub.service
```

## Updates and removal

Run `lmh update` as `ai` to check for an update manually. To turn off future
scheduled checks, run `systemctl --user disable --now localmodelhub-update.timer`
as `ai`. Back up the catalog, policy and state before upgrading. To install
an earlier release, verify it and run that release's installer; downgrading
does not undo stored configuration or state changes. Reinstall after a Python
minor-version upgrade to rebuild the environment.

Stop running profiles explicitly before taking the panel offline if you
also want inference stopped: managed models have separate user units.
Run `lmh uninstall` as `ai` to uninstall. Review `lmh uninstall --help` and
back up anything you want to keep before using `lmh uninstall --purge`.
Do not manually delete or unlink the active release while services are running.
