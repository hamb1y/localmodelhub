# Configuration and operations

For the normal install, start with the [README](../README.md). The commands
below assume the service runs under the dedicated, unprivileged `ai` account.
The one-step bootstrap already installs the verified CPU runtime and public
starter, starts the profile without stopping others, and verifies a CPU chat
request. The steps below are for administration, not required setup.

## Sign in as `ai`

Use a normal login or `sudo -iu ai` from an administrator shell. If `lmh` is
not on your `PATH`, add the installed CLI for the current shell:

```bash
export PATH="$HOME/.local/share/localmodelhub/current/bin:$PATH"
```

If `systemctl --user` cannot connect after `sudo -iu ai`, set the user-bus
environment inside the `ai` shell:

```bash
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
systemctl --user show-environment >/dev/null
```

## Manual source install (advanced)

The host needs systemd 254+, Podman with rootless subordinate UID/GID ranges,
Git, a C toolchain, Python 3.11+, Bash, `curl`, coreutils and `flock`.
Managed runtime units use `systemd-run --expand-environment=no` so systemd
does not expand model arguments. Install Python's `venv`/`ensurepip` support
(for example `python3-venv` on Debian/Ubuntu), or install `uv` alongside
Python. The installer uses `uv` if present, otherwise `venv` and `pip`.
It builds the app locally with Cargo and Bun, provisioning them privately
as needed. Dependencies come from the checked-in lockfiles.

If you aren't using the quick-start installer, prepare the account as an
administrator:

```bash
id ai >/dev/null 2>&1 || sudo useradd --create-home --shell /bin/bash ai
sudo loginctl enable-linger ai
```

Linger starts the user manager at boot and keeps it running after logout.
Give `ai` only the access needed to read model files, run approved binaries,
and use configured devices. Do not give the service account root privileges.

As `ai`, clone the official source and install it:

```bash
git clone --depth 1 https://github.com/hamb1y/localmodelhub.git
cd localmodelhub
bash deploy/install.sh
curl --fail --retry 5 --retry-connrefused http://127.0.0.1:5100/api/health
```

You can remove the checkout after installation.

This low-level installer does not run the bootstrap’s starter onboarding.
On first install, the starter catalog provides profiles but has no boot
actions. Pass `--catalog FILE` to seed a reviewed local catalog instead, or
`--catalog deploy/example-catalog.json` for an empty panel. Reinstalling
does not overwrite the existing catalog, credentials, policy, unit drop-ins,
or application state. A normal install enables the user service and restarts
it when activating a different Git commit. For staging checks in a disposable
home, `--no-systemd` installs files without talking to the service manager.

| Purpose | Default path under `ai`’s home |
| --- | --- |
| Catalog | `.config/localmodelhub/models.json` |
| Optional policy | `.config/localmodelhub/policy.json` |
| Optional environment file | `.config/localmodelhub/service.env` |
| User unit | `.config/systemd/user/localmodelhub.service` |
| Persistent state | `.local/share/localmodelhub/state/` |
| Active release | `.local/share/localmodelhub/current/` |
| Rust CLI and terminal UI | `.local/share/localmodelhub/current/bin/lmh` |

## Catalogs

See the [`models.json` reference](models-json.md) for the schema, model
input/output pairs, and how to preview, stage, or edit a file.

Boot status reports a BLAKE3 fingerprint of the accepted catalog, independent
of whitespace and key order. Checksums verify file integrity; they are not a
substitute for reviewing a catalog or trusting its publisher.

## Runtime policy

Local policy approves the container image each runtime may execute. Approve
only trusted, digest-pinned images, matching the runtime ID and adapter from
the catalog. Create `~/.config/localmodelhub/policy.json` as `ai` with mode
`0600` (`umask 077` before creating it). For example:

```json
{
  "runtimes": {
    "llama-cpu": {
      "adapter": "llama.cpp",
      "image": "ghcr.io/ggml-org/llama.cpp:server@sha256:b0cd5490eefff30b40dcd47f7249a7d7211136908f73fe440a2a86b35a22b23b"
    },
    "sd-cuda": {
      "adapter": "stable-diffusion.cpp",
      "image": "ghcr.io/leejet/stable-diffusion.cpp:master-cuda@sha256:4fd8b723a56c06c421cd299febb393ff9563d448eaa5f713cd98c2017f417542",
      "executable": "/sd-server"
    }
  }
}
```

An `image` must include an `@sha256:<64 hex>` digest; tags are not enough.
The digest in the catalog, when present, must match this approval. On an
explicit Start the controller pulls the pinned image if it is missing, then
runs it with `--pull=never`, so a later tag move cannot change the engine.
`executable` is an optional absolute command inside the container; omit it to
use the image entrypoint. Restart the service to load manually edited policy.
The normal bootstrap handles Podman setup and CPU image approval itself.
An older policy entry that approves only a host `executable` no longer
authorizes a typed adapter; replace it with the pinned image. The explicit
`native` and `systemd` adapters keep their existing host-only approval rules.

Rootless Podman must work for `ai`: the account needs `/etc/subuid` and
`/etc/subgid` ranges plus `newuidmap`/`newgidmap` (`uidmap` on Debian and
Arch). Container storage is `~ai/.local/share/containers`. The one-step
bootstrap installs these; for a manual setup run `podman info` as `ai` before
starting a model.

GPU profiles use rootless NVIDIA CDI: install the NVIDIA driver and
`nvidia-container-toolkit`, generate a CDI spec (`nvidia-ctk cdi generate
--output=/etc/cdi/nvidia.yaml`), and verify `podman run --device
nvidia.com/gpu=all … nvidia-smi` works as `ai`. The starter's `sd-cuda`
profile pulls the pinned stable-diffusion.cpp CUDA image; make sure enough
VRAM and model storage are available. Without a working CDI spec, start CPU
profiles only.

Locally configured native commands and adopted systemd units are not portable
imports. Adopted system units are read-only by default; an `"allowlisted"`
policy entry does not itself grant control. A privileged integration, if
needed, requires a separately reviewed, root-owned `ai-service-control`
wrapper, sudoers entries restricted to exact units and actions, and an
administrator-approved user-service drop-in allowing privilege elevation.
Never grant unrestricted `sudo` or `systemctl` rights. This is not part of the
normal install. Managed Podman units keep `NoNewPrivileges=no` only inside the
transient unit so rootless `newuidmap` can run; the panel service itself keeps
`NoNewPrivileges=true`.

## Credentials and remote access

The public CPU starter does not require a token. For gated or private
models, obtain access upstream and then run `lmh hf-login` as `ai`
with a minimally scoped read token. It hides token input and leaves existing
credentials in place. The service and onboarding must use the same `HOME`
and, if overridden, the same `HF_HOME`. Never put tokens in a catalog,
command-line argument, or Git. Use mode `0600` for `service.env` if you use it.

On the host, the panel is at `http://127.0.0.1:5100`, inference is at
`http://127.0.0.1:5101/v1`, and webhooks listen on `127.0.0.1:5102`.
Keep the panel and engines on loopback. To reach the panel remotely, use an
SSH tunnel through an authorized account:

```bash
ssh -N -L 5100:127.0.0.1:5100 your-user@your-host
```

Loopback is not a per-user security boundary on a shared host. A Hugging Face
token does not authenticate panel users. Only allow trusted operators to use
the panel; do not expose the backend directly.

The policy's `api_token` protects the panel's management API. Give it to the panel
in Settings or to the CLI/TUI with `LMH_API_TOKEN` in a private environment.
The separate `webhooks` policy object can give each automation
caller its own token, allowed `actions` and allowed `models`. A caller sends
`Authorization: Bearer …` to `POST http://127.0.0.1:5102/api/webhooks/action` with `action`,
`model`, and `profile`. A `switch` also needs `from_model` and `from_profile`,
and both model IDs must be in scope. Use independent random secrets for
callers and the ordinary API; store them privately and rotate exposed tokens.

## Inference keys and Cloudflare Tunnel

```bash
lmh api-key create laptop
lmh api-key list
lmh api-key revoke laptop
```

Save a newly generated key when it's printed; the service stores only its
hash. Set it as the API key in your OpenAI client. Creating the first key
enables bearer authentication for inference, including on loopback.
Revoking the last key does not reopen access. Panel and webhook tokens do
not grant inference access. If you rerun onboarding with keys enabled, set
`LMH_INFERENCE_KEY` in a private environment so its chat check can authenticate.

To expose inference through [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/):

1. Create an inference key and run `lmh cloudflare setup ai.example.com`,
   replacing the hostname with one you control.
2. In Cloudflare Zero Trust, create a tunnel and run the provided `cloudflared`
   connector on the LocalModelHub host. Add a **Published application route**
   using the hostname that `lmh` printed, service `http://127.0.0.1:5101`, and
   the public hostname as **HTTP Host Header** under the HTTP settings.
3. Set your client's base URL to `https://ai.example.com/v1` and use the
   inference key. Don't publish the panel (`5100`), webhooks (`5102`), or
   engine ports. `lmh cloudflare disable` stops accepting the public hostname;
   remove the route in Cloudflare too.

The `lmh` command configures the local origin, not your Cloudflare account
or DNS. Never share Cloudflare connector credentials or inference keys.

## Inference statistics and logging

Use the model selector on the panel's Stats page, Left/Right on the TUI Stats
tab, or `lmh stats --model qwen2.5-chat`. The Stats API accepts the same
scope as `GET /api/stats?model=qwen2.5-chat`. Host memory is still host-wide;
model job totals, token telemetry, and language counts are model-specific.

The language heuristic counts **mild profanity**, **strong profanity**, and
**slurs** in generated English text passing through `/v1`. It handles case,
common substitutions and misspellings, and words split across stream chunks.
It can miss words or count quoted or ambiguous language: it is not a general
measure of model quality. Counts survive daemon restarts. Prompts, matched
words, and completions are not stored by this heuristic. Direct engine
requests, audio bytes, and incomplete chat streams are not scored.

To explicitly log inference bodies, add `"inference_logging": true` to the
local `policy.json` and restart `localmodelhub.service` as `ai`. This is **off
by default**. The private `state/inference-logs/` directory gets one folder
per dispatched request with `input.body`, `output.body`, and `metadata.json`.
It can contain full prompts, uploaded media, generated content, and errors.
Headers such as `Authorization` are not stored. Treat these files as
sensitive: they are not sent to the panel or automatically deleted. Monitor
disk use and remove old logs when no longer needed. Disabling the option
stops future capture without deleting existing files.

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
same card. Use a CUDA UUID to select a specific GPU. The optional transient
unit limits `resources.memory_high_mib`, `resources.memory_max_mib`, and
`resources.swap_max_mib` are separate from those estimates. Rates are based
on runtime-reported counters or measurements and remain unavailable if
there is no usable measurement.

Each running profile is a container named `lmh-<model>-<hash>` inside a
transient user unit. `podman ps --filter name=lmh-` lists them, and
`lmh logs` reads the unit journal, which includes the container's output.
If a start fails, check that the pinned image is pullable and, for GPU
profiles, that the CDI spec is visible to `ai`.

Use `lmh doctor`, `lmh jobs`, `lmh stats`, and `lmh logs --help` to diagnose
issues. If the panel is unreachable, check the user-service journal and
verify that `ai` can read the catalog and access the runtime image and model
files.

```bash
systemctl --user status localmodelhub.service
journalctl --user -u localmodelhub.service -f
systemctl --user restart localmodelhub.service
```

## Updates and removal

Run `lmh update` as `ai` to check the official Git `main` manually. The app
builds a changed commit before switching to it and keeps the current copy if
the build fails. It doesn't start or replace models. To stop scheduled checks,
run `systemctl --user disable --now localmodelhub-update.timer` as `ai`.
If your app was installed from an old archive, rerun the public installer
once to switch it to source installs. Rerun it after a Python minor-version
upgrade to rebuild the environment.

Stop running profiles explicitly before taking the panel offline if you
also want inference stopped: managed models have separate user units, each
running its own Podman container. Uninstall stops those units and their
containers; pulled images stay in `ai`'s Podman storage and can be removed
with `podman rmi` when no longer needed.
Run `lmh uninstall` as `ai` to uninstall. Review `lmh uninstall --help` and
back up anything you want to keep before using `lmh uninstall --purge`.
Do not manually delete or unlink the active release while services are running.
