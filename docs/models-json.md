# `models.json` reference

`models.json` tells LocalModelHub what each model accepts and produces, which
files it needs, and how to run it. Edit it under the service account’s home:
`~ai/.config/localmodelhub/models.json`. The panel and TUI display model types;
the file is the only source of those declarations.

## Add a model

1. Copy your current file to a draft, preserving the models and boot entries
   you still want. For a new file, start with the [bundled starter](../catalogs/starter.json).
2. Pick a stable model ID and declare its input/output pairs. Reference the
   runtime and artifacts from a profile. Choose a free loopback port for
   each engine you want to run alongside another.
3. Run `lmh preview --catalog ./models.draft.json`. This checks the JSON
   structure and references through the running daemon. It does not approve
   a runtime, verify the artifact bytes, or prove that the model can start.
4. Back up the active file and install the reviewed draft as `models.json`.
   The running daemon uses the accepted snapshot until the next **Linux
   boot**; restarting the daemon is not a catalog reload. After boot, use
   `lmh list` and explicitly start the profile if it has no boot entry.

For a shareable catalog, use typed adapters, loopback bindings, and pinned
Hugging Face files with a positive `size` and at least one verified hash.
`lmh stage --catalog FILE` checks those requirements and schedules the
reviewed catalog to **replace**, not merge with, the local file at the next
boot. Staging clears its `boot` entries. For a host-only file with local
paths, edit `models.json` directly instead.

## Top-level fields

| Key | What it contains |
| --- | --- |
| `schema_version` | Required integer `1`. |
| `runtimes` | Required object mapping runtime IDs to runtime definitions. |
| `artifacts` | Required object mapping artifact IDs to file sources and integrity data. |
| `models` | Required object mapping model IDs to definitions. This ID is the `model` value clients send to `/v1`. |
| `boot` | Optional array of `{"model":"id","profile":"id"}` entries; defaults to `[]`. |

Write UTF-8 JSON, with no comments, duplicate keys, or trailing commas. IDs
start with a letter or digit and can also contain `_`, `-`, and `.`. A boot
entry can omit `profile` if the model has a `default_profile` or only one
profile. `default_profile` alone does not enable startup. Boot and explicit
Start never evict another running model.

## Complete example: one local CPU chat model

Replace the file path with a readable, llama.cpp-compatible GGUF. This
example assumes `llama-cpu` is already approved in your local policy.

```json
{
  "schema_version": 1,
  "runtimes": {
    "llama-cpu": { "adapter": "llama.cpp" }
  },
  "artifacts": {
    "chat-weights": {
      "source": { "kind": "local", "path": "/absolute/path/to/chat-model.gguf" }
    }
  },
  "models": {
    "my-chat": {
      "name": "My chat model",
      "io": [{ "input": ["text"], "output": "text" }],
      "default_profile": "cpu",
      "profiles": {
        "cpu": {
          "runtime": "llama-cpu",
          "device": "cpu",
          "artifacts": { "model": "chat-weights" },
          "host": "127.0.0.1",
          "port": 8091
        }
      }
    }
  },
  "boot": []
}
```

## Model input and output

Every entry under `models` requires a display `name`, a nonempty `io` list, and
a nonempty `profiles` object. An optional `default_profile` must name one of
its profiles. Profiles of a model serve the same declared pairs.

Each `io` entry is one exact combination: a nonempty list of distinct
`input` types and one `output` type. Input order does not matter. Do not
repeat a pair. The accepted types are:

- **Input:** `text`, `image`, `audio`, `video`.
- **Output:** `text`, `image`, `audio`, `video`, `decision`, `embedding`.

For example, these two pairs declare text-to-image generation and image
editing with a text prompt, but do not declare image-only input:

```json
{
  "io": [
    { "input": ["text"], "output": "image" },
    { "input": ["text", "image"], "output": "image" }
  ]
}
```

`decision` is structured output produced by the engine, not permission to
perform an action. Request it through chat completions with strict
`response_format.type: "json_schema"`; the engine must support that format.
Declaring a pair does not add a capability to an incompatible engine.
Runtime, CPU/GPU, and quantization are deployment choices, not model types.

## Runtimes, artifacts, and profiles

**Runtimes.** A runtime requires an `adapter`, normally `llama.cpp` or
`stable-diffusion.cpp`. A definition can pin `version` or `sha256` and set
adapter `options`. The local [runtime policy](configuration.md#runtime-policy)
approves the executable separately. `native` and `systemd` are for explicitly
reviewed host-only integrations, not portable catalogs.

**Artifacts.** Each artifact requires a `source`. A Hugging Face source is
`{"kind":"hf","repo":"org/name","revision":"<40-hex-commit>","file":"path/to/file.gguf"}`.
Use the full Git commit, not a branch or tag. A local source is
`{"kind":"local","path":"/absolute/path/to/file.gguf"}`; `ai` needs read access.
`size` is bytes and `hashes.sha256`/`hashes.blake3` are 64 hex digits. Pin
both when known and never invent hashes. See `catalogs/starter.json` for
working pins. Do not put tokens or executable approvals in a shared file.

**Profiles.** A profile requires `runtime` (an existing runtime ID),
`device` (`cpu` or `gpu`), and `artifacts` (an object mapping adapter roles
to artifact IDs or ordered lists of shard IDs). Adapter roles and options
must match the actual engine. For example, `llama.cpp` uses `model` for the
main file, optionally `mmproj` for vision, and `options.embedding: true`
for its embedding mode. See the starter for a diffusion profile.

| Optional profile keys | Purpose |
| --- | --- |
| `host`, `port` | Engine bind address and port. Use `127.0.0.1` or `::1`; clients only need the unified port. Default engine ports are `8080` for llama.cpp and `1234` for stable-diffusion.cpp. |
| `gpu` | CUDA UUID or ordinal as a string, GPU profiles only. |
| `ram_mib`, `vram_mib` | Resource estimates for safe coexistence; `vram_mib` is GPU-only. |
| `options` | Typed profile-specific runtime settings. |
| `health` | Local `path` and `timeout_seconds` for readiness. |
| `metrics_path` | Local engine metrics endpoint, if available. |
| `resources` | Optional systemd limits: `memory_high_mib`, `memory_max_mib`, `swap_max_mib`. |
| `lifecycle` | Optional `stop_timeout_seconds`. |

## Unified inference

Set your OpenAI client's base URL to `http://127.0.0.1:5189/v1`, then use the
model ID from `models.json`. If `api_token` is set in local policy, use it as
the client API key. Otherwise, use a placeholder like `local` if your SDK
requires a key. `GET /v1/models` lists ready models, with their declared `io`.
LocalModelHub prefers a ready default profile, otherwise a ready profile in ID
order. It never starts or switches models in response to inference requests.

| Routes under `/v1` | Input and output |
| --- | --- |
| `POST /chat/completions` | Declared input combinations to text, audio-only chat output, or a strict JSON decision. Supports SSE where the engine does. Upload chat images/video as inline base64 data URLs, not remote URLs. |
| `POST /embeddings` | Text to embedding. |
| `POST /images/generations`, `POST /images/edits` | Text to image; multipart text and image to image for edits. |
| `POST /audio/speech`, `POST /audio/transcriptions`, `POST /audio/translations` | Text to audio, or multipart audio to text. A transcription prompt is an optional hint, not a separate model input. |
| `POST /videos`, `GET /videos/{id}`, `GET /videos/{id}/content` | Multipart text, optionally with an image `input_reference`, to video. Video status and download use the engine that accepted the job. |

These routes require an engine that implements the corresponding operations.
There is no bundled audio or video engine. Unlisted OpenAI routes and
provider-specific options are not guaranteed, and requests and responses have
size limits. A known but unavailable model returns an error instead of being
started automatically.

```bash
curl -fsS http://127.0.0.1:5189/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-chat","messages":[{"role":"user","content":"Say hello."}]}'
```

See [configuration and operations](configuration.md#inference-statistics-and-logging)
for per-model statistics and optional local request/response body logging.
