# LocalModelHub

Run and manage local AI models on Linux from a web panel, a terminal UI, or the
`lmh` command line. LocalModelHub runs natively as the unprivileged `ai` user
under systemd. No containers or server-side Node.js required.

## Quick start: run a CPU model

You’ll need Linux with **systemd 254+**, **Python 3.11+**, `curl`, Bash, and
either Python `venv` support or `uv`. The bundled CPU setup supports compatible
x86-64 and ARM64 Linux systems.

1. Download and run the installer. It verifies the latest
   [official release](https://github.com/hamb1y/localmodelhub/releases/latest)
   before installing. The first run may ask for `sudo` to create the `ai`
   account and let its services keep running after logout.

   ```bash
   curl -fsS https://raw.githubusercontent.com/hamb1y/localmodelhub/main/install.sh -o localmodelhub-install.sh && bash localmodelhub-install.sh
   ```

   You can delete `localmodelhub-install.sh` after installation.

2. Sign in as `ai` (for example, `sudo -iu ai`), then install the verified
   llama.cpp CPU runtime and reload its approval:

   ```bash
   export PATH="$HOME/.local/share/localmodelhub/current/venv/bin:$PATH"
   export XDG_RUNTIME_DIR="/run/user/$(id -u)"
   export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
   lmh onboard --install-cpu-runtime
   systemctl --user restart localmodelhub.service
   lmh tui
   ```

   Skip the Hugging Face login prompt for the public starter models. For a
   private or gated model you can access, use a scoped read token at the
   interactive prompt. If `lmh` or `systemctl --user` is unavailable, see
   [Sign in as `ai`](https://github.com/hamb1y/localmodelhub/blob/main/docs/configuration.md#sign-in-as-ai).

3. In the TUI, start **Qwen2.5 0.5B Instruct** on CPU. You can also use
   `lmh start qwen2.5-chat cpu` or open [the web panel](http://localhost:5189)
   on the host. Starting the profile fetches its model weights on first use
   (about 491 MB). Installing the panel or runtime alone does not download
   models.

## Runtimes and startup

- The built-in adapters are **llama.cpp** and **stable-diffusion.cpp**. The
  starter NVIDIA image profile requires NVIDIA drivers and a compatible local
  CUDA build of `sd-server`; the installer does not supply either. Only approve
  runtime binaries and catalogs you trust.
- No models start at boot by default. You can opt into starting specific
  profiles at boot, but the boot job never switches away from a running model.
  Switching requires an explicit action.
- Inference rates come from measurements reported by the runtime. If metrics
  aren’t available, LocalModelHub does not guess a rate.

## Help and uninstall

- To remove LocalModelHub, run `lmh uninstall` as `ai`. The optional
  `--purge` also removes LocalModelHub data; back it up first.
- Run `lmh doctor` to check the installation. Keep the panel on loopback;
  use an SSH tunnel for remote access.

[Configuration and operations](https://github.com/hamb1y/localmodelhub/blob/main/docs/configuration.md) ·
[Build and test](https://github.com/hamb1y/localmodelhub/blob/main/docs/development.md) ·
[MIT license](LICENSE)
