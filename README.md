# LocalModelHub

Run local AI models on Linux. Manage them with the web panel or the `lmh`
terminal UI, and call them through one OpenAI-compatible API.

## Install

On Linux (x86_64 or aarch64, systemd 254+), run this from Bash as a non-root
user with `sudo`. You’ll need `curl` and an internet connection.

```bash
curl -fsS https://raw.githubusercontent.com/hamb1y/localmodelhub/main/install.sh -o localmodelhub-install.sh && bash localmodelhub-install.sh
```

On a fresh install, the script sets up the `ai` user, installs llama.cpp,
downloads and starts Qwen2.5 0.5B (about 491 MB), and verifies a real chat
request. No GPU, Hugging Face login, or follow-up setup required.

- **Web:** [http://127.0.0.1:5100](http://127.0.0.1:5100)
- **OpenAI API:** `http://127.0.0.1:5101/v1`, model `qwen2.5-chat`
- **Webhooks:** `http://127.0.0.1:5102/api/webhooks/action`
- **Terminal:** `lmh`

Create a key with `lmh api-key create laptop`. To expose only the inference
API, see [Cloudflare Tunnel setup](docs/configuration.md#inference-keys-and-cloudflare-tunnel).

## Add models

Define model inputs and outputs in [`models.json`](docs/models-json.md).
Paste this into your AI agent to get started:

```text
Draft a LocalModelHub models.json using this reference:
https://github.com/hamb1y/localmodelhub/blob/main/docs/models-json.md
Ask which models I want, what inputs and outputs they need, and what
hardware and config I already have. Preserve existing entries and verify
artifact pins; leave out secrets. If available, run lmh preview --catalog
on the draft. Don't apply it or start anything without asking.
```

Troubleshoot with `lmh doctor`. Remove with `lmh uninstall`.
The web panel includes Agave Nerd Font; select it in your terminal settings
if you want the same font in the TUI.

[Configuration and operations](docs/configuration.md) ·
[Releases](https://github.com/hamb1y/localmodelhub/releases) ·
[Build and test](https://github.com/hamb1y/localmodelhub/blob/main/docs/development.md) ·
[MIT license](LICENSE)
