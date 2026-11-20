# LocalModelHub

Run and manage local AI models on Linux from a web panel, a Ratatui terminal
UI, or the Rust `lmh` CLI. LocalModelHub runs natively under systemd as the
unprivileged `ai` user. No containers, Node.js, or Rust toolchain needed on
the target host.

## Quick start: run a CPU model

You’ll need modern Linux (x86_64 or aarch64), **systemd 254+**, Bash, `curl`,
and internet access. Run as a non-root administrator with `sudo` on a fresh
machine. The installer can add Python and venv support when the system package
manager is supported.

```bash
curl -fsS https://raw.githubusercontent.com/hamb1y/localmodelhub/main/install.sh -o localmodelhub-install.sh && bash localmodelhub-install.sh
```

**No follow-up commands needed.** The installer creates `ai` if needed and
enables linger so its services stay up after logout. It installs the verified
[latest stable release](https://github.com/hamb1y/localmodelhub/releases/latest),
its dependencies, and the verified llama.cpp CPU runtime. It downloads and
verifies the public **Qwen2.5 0.5B Instruct** starter (about 491 MB), starts
it without stopping any running model, and checks that a real CPU chat
request succeeds. Existing custom catalogs are preserved.

On an interactive terminal, the installer opens the Ratatui onboarding UI.
Without a terminal it performs the same setup and reports progress in the
logs. No Hugging Face login is needed for the starter; authentication is
optional for other models that require it.

On the same host, open the [web panel](http://127.0.0.1:5189) or point your
OpenAI-compatible client at `http://127.0.0.1:5189/v1` with model
`qwen2.5-chat`. The same URL serves every ready model.

## Add your own models

Model types are defined in `models.json` by input and output. See the
[`models.json` reference](docs/models-json.md) for examples, supported fields,
and how to validate a draft before using it.

Copy this prompt into your AI agent:

```text
Help me build a LocalModelHub models.json draft. Follow the reference:
https://github.com/hamb1y/localmodelhub/blob/main/docs/models-json.md
Use the starter as a working example:
https://github.com/hamb1y/localmodelhub/blob/main/catalogs/starter.json

Ask me which models I want, what each accepts and produces, what CPU/GPU
hardware I have, and whether I have an existing models.json to preserve.
For each model, declare only supported input/output pairs in its io field.
Verify the exact files, revision, size, and available checksums from a
trusted source. Ask me for missing facts; don't guess pins, paths, or ports.
Keep tokens and runtime approvals out of models.json. Leave boot empty
for new models unless I ask for boot starts.

Return the complete UTF-8 JSON as a draft. If you have access to my
LocalModelHub daemon, run lmh preview --catalog on the draft and report
the result. Don't overwrite or stage my live config, start or stop models,
or install runtimes without asking.
```

## Runtimes and startup

- The built-in adapters are **llama.cpp** and **stable-diffusion.cpp**. The
  starter NVIDIA image profile requires NVIDIA drivers and a compatible local
  CUDA build of `sd-server`; the installer does not supply either. Only approve
  runtime binaries and catalogs you trust.
- The installer starts the CPU starter during setup, not on every boot.
  You can opt into starting specific profiles at boot; switching away from
  a running model always requires an explicit action.
- Inference rates come from measurements reported by the runtime. If metrics
  aren’t available, LocalModelHub does not guess a rate.

## Help and uninstall

- The command is `lmh`. If it isn’t on your `PATH`, run
  `/home/ai/.local/share/localmodelhub/current/bin/lmh` as `ai` (adjust the
  path if `ai` has a different home).
- To remove LocalModelHub, run `lmh uninstall`. The optional
  `--purge` also removes LocalModelHub data; back it up first.
- Run `lmh doctor` to check the installation. Keep the panel on loopback;
  use an SSH tunnel for remote access.

[Configuration and operations](https://github.com/hamb1y/localmodelhub/blob/main/docs/configuration.md) ·
[Build and test](https://github.com/hamb1y/localmodelhub/blob/main/docs/development.md) ·
[MIT license](LICENSE)
