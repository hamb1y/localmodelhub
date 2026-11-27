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

For a shareable catalog, use the generic `container` adapter or a typed
adapter, loopback bindings, and pinned Hugging Face files with a positive
`size` and at least one verified hash.
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
repeat a pair. A type is a lowercase token of letters, digits, `_`, `-` and
`.`, up to 32 characters. The hub's built-in routes produce and understand
this core set:

- **Input:** `text`, `image`, `audio`, `video`.
- **Output:** `text`, `image`, `audio`, `video`, `decision`, `embedding`.

Any other token (`document`, `json`, `3d`, …) is declarative metadata: it is
shown in the panel and `GET /v1/models`, but no built-in route produces or
accepts it, so leave those entries out unless a client reads them.

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

**Runtimes.** Every managed runtime runs in a rootless Podman container. The
generic `container` adapter runs any OpenAI-compatible HTTP server, including
vLLM, TGI, and a llama.cpp server. The typed `llama.cpp` and
`stable-diffusion.cpp` adapters translate their `options` into engine flags and
use the same container launch path.

All managed runtimes need a digest-pinned `image` (or one supplied by local
policy), for example `ghcr.io/ggml-org/llama.cpp:server@sha256:<64 hex>`.
Mutable tags alone are rejected. `executable` is optional and names an
absolute command *inside* the container; it is never a host binary. A typed
runtime can set adapter `options` and pin `version` or `sha256` for policy
matching. The local [runtime policy](configuration.md#runtime-policy) approves
the image separately. `native` and `systemd` remain explicitly approved
host-only escape hatches and cannot be used in portable catalogs; prefer
`container` for any engine that can run from a pinned image.

A generic runtime adds an `argv` array and an optional `env` object. `argv` is
the in-container argument tail, and whole tokens are substituted without a
shell:

| Token | Expands to |
| --- | --- |
| `{host}` | The profile's engine bind host. |
| `{port}` | The profile's engine port. |
| `{gpu}` | Comma-separated CUDA UUIDs selected for the profile, as in `CUDA_VISIBLE_DEVICES`. A CPU profile rejects it. |
| `{artifact:ROLE}` | One argument per resolved path in that profile artifact role. |

`env` values are literal and become container environment variables. Do not
put tokens or secrets in a shared catalog; keep runtime credentials in the
approved image or a locally reviewed catalog instead. This runtime exposes a
vLLM server on the profile port and defaults to a `/health` readiness probe:

```json
{
  "vllm": {
    "adapter": "container",
    "image": "vllm/vllm-openai:latest@sha256:<64 hex>",
    "argv": ["--host", "{host}", "--port", "{port}", "--model", "org/model"],
    "env": { "VLLM_LOGGING_LEVEL": "INFO" }
  }
}
```

A `container` profile must declare an explicit `port`. Its `health.path`
defaults to `/health`; override it if the server answers readiness elsewhere.
Typed adapters keep their existing defaults.

Model files are bind-mounted read-only into the container at their host
absolute paths, so engine arguments and artifact roles stay the same. A
container uses the host network namespace and must bind a loopback `host`; the
controller and readiness probe reach it at `127.0.0.1:<port>` exactly as
before.

### CPU and GPU passthrough

Device passthrough follows the profile, not the image:

- **CPU profiles** get no GPU device at all. Both
  `CUDA_VISIBLE_DEVICES` and `NVIDIA_VISIBLE_DEVICES` are explicitly empty, so
  an engine cannot fall back to a host GPU. CPU-only containers are limited to
  the host's logical CPU count by default; set `resources.cpu_count` to reserve
  fewer.
- **GPU profiles** select one GPU, or several, in `gpu`. Every selected UUID
  becomes its own `nvidia.com/gpu=<uuid>` CDI device, and the engine's
  `CUDA_VISIBLE_DEVICES` contains exactly that UUID list. The container never
  receives `nvidia.com/gpu=all`, so it cannot see an unselected sibling.
  `vram_mib` is the budget applied to each selected GPU.
- With no `gpu` selector, a GPU profile uses the host's only GPU. On a
  multi-GPU host the profile must name its GPUs, so a model never silently
  receives the wrong card.

A two-GPU profile for a tensor-parallel server:

```json
{
  "gpu": ["GPU-11111111-1111-1111-1111-111111111111",
          "GPU-22222222-2222-2222-2222-222222222222"],
  "vram_mib": 16000
}
```

The engine must also be told to use both devices when it needs an explicit
parallelism argument; add it to the generic runtime's `argv` (for example
`"--tensor-parallel-size", "2"`). Typed `llama.cpp` and
`stable-diffusion.cpp` profiles address a single `CUDA0` and reject multiple
selectors.

**Artifacts.** Each artifact requires a `source`. A Hugging Face source is
`{"kind":"hf","repo":"org/name","revision":"<40-hex-commit>","file":"path/to/file.gguf"}`.
Use the full Git commit, not a branch or tag. A local source is
`{"kind":"local","path":"/absolute/path/to/file.gguf"}`; `ai` needs read access.
`size` is bytes and `hashes.sha256`/`hashes.blake3` are 64 hex digits. Pin
both when known and never invent hashes. See `catalogs/starter.json` for
working pins. Do not put tokens or local image approvals in a shared file.

**Profiles.** A profile requires `runtime` (an existing runtime ID),
`device` (`cpu` or `gpu`), and `artifacts` (an object mapping adapter roles
to artifact IDs or ordered lists of shard IDs). Adapter roles and options
must match the actual engine. For example, `llama.cpp` uses `model` for the
main file, optionally `mmproj` for vision, and `options.embedding: true`
for its embedding mode. See the starter for a diffusion profile.

| Optional profile keys | Purpose |
| --- | --- |
| `host`, `port` | Internal engine bind address and port. Use `127.0.0.1` or `::1`. Clients only need the shared inference port, `5101`. |
| `gpu` | CUDA UUID or ordinal, or a list of them, for GPU profiles only. Multiple values attach exactly those CDI devices. |
| `ram_mib`, `vram_mib` | Resource estimates for safe coexistence; `vram_mib` is GPU-only and applies to each selected GPU. |
| `options` | Typed profile-specific runtime settings. |
| `health` | Local `path` and `timeout_seconds` for readiness. |
| `metrics_path` | Local engine metrics endpoint, if available. |
| `metrics_port` | Port of a metrics listener separate from the engine port. Defaults to `port`. |
| `metrics` | Counter names for token, request and error telemetry (see below). |
| `resources` | Optional cgroup limits for the transient unit and its container: `memory_high_mib`, `memory_max_mib`, `swap_max_mib`, `cpu_count`. |
| `lifecycle` | Optional `stop_timeout_seconds`. |
| `upstream` | Per-route engine model IDs (see below). |

### Engine model IDs

LocalModelHub normally forwards its own model ID after checking what the
engine advertises on `/v1/models`. A profile's `upstream` map overrides that
per route. `"*"` sets the default, a route key overrides it, and `null` omits
the `model` field for engines that infer their model.

```json
"upstream": {
  "*": "hamb1y-swift-1.5-27b-abl-16g",
  "/v1/audio/transcriptions": "qwen3-asr-1.7b",
  "/v1/audio/translations": "qwen3-asr-1.7b"
}
```

This keeps one engine that serves several model IDs (a chat model plus a
speech model on the same port) as one catalog entry. Mapped values are also
accepted as request model IDs, so a client can send `whisper-1` when a profile
declares it. An undeclared ID still returns 404.

### Metrics counters

`metrics` maps hub counters to the engine's Prometheus metric names. Without
it the hub reads llama.cpp's defaults. Names may appear with several label
sets; values are summed. Unreported counters stay `null`, never zero.

```json
"metrics_path": "/metrics",
"metrics_port": 9464,
"metrics": {
  "prompt_tokens": "ignis_prompt_tokens_total",
  "generation_tokens": "ignis_generated_tokens_total",
  "cached_tokens": "ignis_prefix_reused_tokens_total",
  "requests": "ignis_requests_accepted_total",
  "errors": "ignis_requests_rejected_total"
}
```

`prompt_tokens`, `generation_tokens` and `cached_tokens` drive the token
panel; `requests` and `errors` add totals, a rate and an error rate. `errors`
should count requests the engine rejected, not client cancellations.

## Unified inference

Set your OpenAI client's base URL to `http://127.0.0.1:5101/v1`, then use the
model ID from `models.json`. If inference keys are enabled, use a key
generated with `lmh api-key create NAME`. Otherwise, use a placeholder like
`local` if your SDK requires a key. `GET /v1/models` lists ready models,
with their declared `io`.
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
curl -fsS http://127.0.0.1:5101/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-chat","messages":[{"role":"user","content":"Say hello."}]}'
```

See [configuration and operations](configuration.md#inference-statistics-and-logging)
for per-model statistics and optional local request/response body logging.
