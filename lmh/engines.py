"""Translate local runtime configuration into container and probe specifications.

Every managed engine runs in a rootless Podman container approved by local
policy: the generic ``container`` adapter plus the typed llama.cpp and
stable-diffusion.cpp adapters share one launch mechanism. Model files are
bind-mounted read-only at their host paths. The controller resolves artifacts,
checks the image and runs the specification with shell=False. Pass Launch.env as
the complete environment of the Podman client, not as overrides on the
controller environment.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .podman import image_reference

LLAMA_ENGINE_PORT = 18080
# Typed adapters plus the generic OpenAI-compatible server adapter. All of them
# launch through the same digest-pinned Podman path.
CONTAINER_ADAPTERS = frozenset({"container", "llama.cpp", "stable-diffusion.cpp"})
CONTAINER_ENGINE_DEFAULTS = {
    "llama.cpp": (LLAMA_ENGINE_PORT, "/health"),
    "stable-diffusion.cpp": (1234, "/v1/models"),
    "container": (None, "/health"),
}


@dataclass(frozen=True)
class Container:
    """The Podman command and the engine process it starts inside the image."""

    image: str
    name: str
    engine_argv: tuple[str, ...]
    mounts: tuple[tuple[str, str], ...]
    gpus: tuple[str, ...] = ()

    def __post_init__(self):
        if isinstance(self.gpus, str):
            object.__setattr__(self, "gpus", (self.gpus,) if self.gpus else ())
        elif not isinstance(self.gpus, tuple):
            object.__setattr__(self, "gpus", tuple(self.gpus))

    @property
    def gpu(self) -> str | None:
        """The single attached GPU, for callers that predate multi-GPU."""
        return self.gpus[0] if len(self.gpus) == 1 else None


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: Path | None
    health_url: str | None
    health_timeout: float
    version_argv: tuple[str, ...] | None = None
    # Capture both streams in the controller's log; no engine-managed log file.
    log_streams: tuple[str, ...] = ("stdout", "stderr")
    container: Container | None = None


_UUID = re.compile(
    r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_ORDINAL = re.compile(r"0|[1-9][0-9]{0,8}")
_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")
_SHARD = re.compile(r"(.+)-([0-9]{5})-of-([0-9]{5})\.gguf")
_RUNTIME_FIELDS = {"adapter", "executable", "image", "version", "sha256", "options"}
_NATIVE_FIELDS = _RUNTIME_FIELDS | {"argv", "env", "cwd"}
_GENERIC_FIELDS = _RUNTIME_FIELDS | {"argv", "env"}
_MANAGED_ENV = frozenset({"CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "NVIDIA_VISIBLE_DEVICES"})
_LLAMA_INTS = {
    "threads": "--threads",
    "threads_batch": "--threads-batch",
    "ctx_size": "--ctx-size",
    "batch_size": "--batch-size",
    "ubatch_size": "--ubatch-size",
    "parallel": "--parallel",
}
# ggml names accepted by llama.cpp b12359 common/arg.cpp::kv_cache_types.
_LLAMA_CACHE_TYPES = {"f32", "f16", "bf16", "f8_e4m3", "q8_0", "q4_0", "q4_1",
                      "iq4_nl", "q5_0", "q5_1"}
_LLAMA_SPEC_TYPES = {"none", "draft-simple", "ngram-simple", "ngram-map-k",
                     "ngram-map-k4v", "ngram-mod"}
_LLAMA_DRAFT_OPTIONS = {"spec_draft_n_min", "spec_draft_n_max", "draft_gpu_layers"}
_SD_COMPONENTS = {
    "model": "--model",
    "diffusion_model": "--diffusion-model",
    "vae": "--vae",
    "clip_l": "--clip_l",
    "clip_g": "--clip_g",
    "t5xxl": "--t5xxl",
    "llm": "--llm",
    "llm_vision": "--llm_vision",
}
_SD_INTS = {
    "threads": "--threads",
    "steps": "--steps",
    "width": "--width",
    "height": "--height",
    "max_pending_jobs": "--max-pending-jobs",
}


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    return value


def _known(values: dict, keys, name: str) -> None:
    if unknown := values.keys() - keys:
        raise ValueError(f"unsupported {name}: {', '.join(sorted(unknown))}")


def _text(value: object, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) or "\0" in value:
        raise ValueError(f"{name} must be a valid string")
    return value


def _absolute(value: object, name: str) -> Path:
    path = Path(_text(os.fspath(value) if isinstance(value, Path) else value, name))
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def _integer(value: object, name: str, low: int = 1, high: int = 2**31 - 1) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _choice(value: object, name: str, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(choices))}")
    return value


def _paths(files: dict, role: str) -> list[Path]:
    if role not in files:
        raise ValueError(f"missing artifact role: {role}")
    raw = files[role] if isinstance(files[role], list) else [files[role]]
    if not raw:
        raise ValueError(f"artifact role {role} must not be empty")
    return [_absolute(path, f"artifact {role}") for path in raw]


def _one_path(files: dict, role: str) -> str:
    paths = _paths(files, role)
    if len(paths) != 1:
        raise ValueError(f"artifact role {role} requires exactly one path")
    return str(paths[0])


def _llama_model(files: dict, role: str = "model") -> str:
    paths = _paths(files, role)
    first = paths[0]
    shard = _SHARD.fullmatch(first.name)
    if len(paths) == 1 and shard is None:
        return str(first)
    # llama.cpp discovers the other standard GGUF shards beside shard one.
    if (
        not shard or len(paths) != int(shard[3])
        or any(
            path != first.with_name(f"{shard[1]}-{i:05d}-of-{shard[3]}.gguf")
            for i, path in enumerate(paths, 1)
        )
    ):
        raise ValueError(f"{role} shards must be a complete, ordered, colocated GGUF set")
    return str(first)


def _network(profile: dict, default_port: int | None, default_probe: str | None):
    host = _text(profile.get("host", "127.0.0.1"), "host")
    try:
        addr = ipaddress.ip_address(host) if "%" not in host else None
    except ValueError:
        addr = None
    if addr is None:
        if len(host) > 253 or any(not _LABEL.fullmatch(part) for part in host.rstrip(".").split(".")):
            raise ValueError("host must be a single IP address or hostname")
    port = default_port
    if "port" in profile:
        port = _integer(profile["port"], "port", 1, 65535)

    health = _object(profile.get("health", {}), "health")
    _known(health, {"path", "timeout_seconds"}, "health fields")
    timeout = float(_integer(health.get("timeout_seconds", 120), "health.timeout_seconds"))
    path = health.get("path", default_probe)
    if "path" in health or path is not None:
        path = _text(path, "health.path")
        if not re.fullmatch(r"/(?!/)[A-Za-z0-9_./~!$&'()*+,;=:@%-]*", path):
            raise ValueError("health.path must be an absolute URL path")
        if port is None:
            raise ValueError("a health probe requires a port")
        probe_host = host
        if addr is not None and addr.is_unspecified:
            probe_host = "::1" if addr.version == 6 else "127.0.0.1"
        if addr is not None and addr.version == 6:
            probe_host = f"[{probe_host}]"
        return host, port, f"http://{probe_host}:{port}{path}", timeout
    return host, port, None, timeout


def _device(profile: dict, gpu_ids) -> tuple[str, ...]:
    """Return the CUDA UUIDs visible to this profile, in selection order.

    A GPU profile may select one GPU or several. The controller resolves the
    profile's UUID/ordinal selectors against detected hardware and passes the
    resolved UUIDs; an explicit UUID or a different ordinal cannot be replaced.
    """
    device = profile.get("device")
    if device == "cpu":
        if gpu_ids is not None or "gpu" in profile:
            raise ValueError("a CPU profile cannot select a GPU")
        return ()
    if device != "gpu":
        raise ValueError("device must be 'cpu' or 'gpu'")
    configured = _gpu_selectors(profile["gpu"]) if "gpu" in profile else ()
    selected = _gpu_selectors(gpu_ids) if gpu_ids is not None else configured
    if not selected:
        raise ValueError("GPU profiles require at least one CUDA GPU UUID or ordinal")
    if configured and selected != configured:
        # The controller may resolve ordinals to UUIDs element by element.
        if len(configured) != len(selected) or not all(
            _ORDINAL.fullmatch(requested) and _UUID.fullmatch(resolved)
            for requested, resolved in zip(configured, selected)
        ):
            raise ValueError("resolved GPUs differ from the requested GPUs")
    return selected


def _gpu_selector(value: object) -> str:
    if isinstance(value, str):
        if _UUID.fullmatch(value):
            return "GPU-" + value[4:].lower()
        if _ORDINAL.fullmatch(value):
            return value
    raise ValueError("GPU selectors must be a CUDA GPU UUID or ordinal")


def _gpu_selectors(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raise ValueError("GPU selectors must be a CUDA GPU UUID, ordinal, or list of them")
    if not raw:
        raise ValueError("GPU selectors must not be empty")
    selectors = tuple(_gpu_selector(item) for item in raw)
    if len(set(selectors)) != len(selectors):
        raise ValueError("GPU selectors must not repeat a device")
    return selectors


def _llama_speculation(args: list[str], options: dict, files: dict, gpu: str | None) -> None:
    has_draft = "draft_model" in files
    if "spec_type" not in options and not has_draft and not (_LLAMA_DRAFT_OPTIONS & options.keys()):
        return
    mode = _choice(options.get("spec_type", "draft-simple" if has_draft else "none"),
                   "spec_type", _LLAMA_SPEC_TYPES)
    if mode != "draft-simple":
        if has_draft or _LLAMA_DRAFT_OPTIONS & options.keys():
            raise ValueError("draft_model and draft options require spec_type 'draft-simple'")
        args.extend(("--spec-type", mode))
        return

    model = _llama_model(files, "draft_model")
    args.extend(("--spec-type", mode, "--spec-draft-model", model))
    layers = options.get("draft_gpu_layers", "auto" if gpu else 0)
    if gpu is None:
        _integer(layers, "draft_gpu_layers", 0, 0)
    elif layers not in ("auto", "all"):
        _integer(layers, "draft_gpu_layers", 0)
    args.extend(("--spec-draft-device", "CUDA0" if gpu and layers != 0 else "none",
                 "--spec-draft-ngl", str(layers)))

    # b12359 defaults: minimum 0, maximum 3.
    minimum = _integer(options.get("spec_draft_n_min", 0), "spec_draft_n_min", 0)
    maximum = _integer(options.get("spec_draft_n_max", 3), "spec_draft_n_max", 0)
    if minimum > maximum:
        raise ValueError("spec_draft_n_min cannot exceed spec_draft_n_max")
    if "spec_draft_n_min" in options:
        args.extend(("--spec-draft-n-min", str(minimum)))
    if "spec_draft_n_max" in options:
        args.extend(("--spec-draft-n-max", str(maximum)))


def _llama(args: list[str], options: dict, files: dict, gpu: str | None) -> None:
    _known(options, set(_LLAMA_INTS) | _LLAMA_DRAFT_OPTIONS | {
        "gpu_layers", "mmap", "kv_offload", "flash_attn", "embedding", "metrics", "pooling",
        "cache_type_k", "cache_type_v", "cache_ram_mib", "fit", "fit_target_mib", "fit_ctx",
        "mmproj_offload", "jinja", "spec_type",
    }, "llama.cpp options")
    _known(files, {"model", "mmproj", "chat_template", "draft_model"}, "llama.cpp artifact roles")
    has_fit_targets = "fit_target_mib" in options or "fit_ctx" in options
    fit = _boolean(options.get("fit", has_fit_targets), "fit")
    if has_fit_targets and not fit:
        raise ValueError("fit_target_mib and fit_ctx require fit to be enabled")
    if gpu is None:
        args.extend(("--device", "none", "--n-gpu-layers", "0", "--no-op-offload"))
        if "gpu_layers" in options:
            _integer(options["gpu_layers"], "gpu_layers", 0, 0)
        if _boolean(options.get("kv_offload", False), "kv_offload"):
            raise ValueError("kv_offload cannot be enabled on CPU")
        args.append("--no-kv-offload")
    else:
        # Fitting can adjust the layer count only when it is left at auto.
        layers = options.get("gpu_layers", "auto" if fit else "all")
        if layers not in ("auto", "all"):
            _integer(layers, "gpu_layers")
        args.extend(("--device", "CUDA0", "--split-mode", "none", "--main-gpu", "0",
                     "--n-gpu-layers", str(layers)))
        if "kv_offload" in options:
            args.append("--kv-offload" if _boolean(options["kv_offload"], "kv_offload")
                        else "--no-kv-offload")
    if "fit" in options or has_fit_targets:
        args.extend(("--fit", "on" if fit else "off"))
    if "fit_target_mib" in options:
        args.extend(("--fit-target", str(_integer(options["fit_target_mib"], "fit_target_mib", 0))))
    if "fit_ctx" in options:
        args.extend(("--fit-ctx", str(_integer(options["fit_ctx"], "fit_ctx"))))
    args.extend(("--model", _llama_model(files)))
    mmproj_offload = _boolean(options.get("mmproj_offload", gpu is not None), "mmproj_offload")
    if "mmproj_offload" in options:
        if "mmproj" not in files:
            raise ValueError("mmproj_offload requires an mmproj artifact")
        if mmproj_offload and gpu is None:
            raise ValueError("mmproj_offload cannot be enabled on CPU")
    if "mmproj" in files:
        args.extend(("--mmproj", _one_path(files, "mmproj")))
        if not mmproj_offload:
            args.append("--no-mmproj-offload")
        elif "mmproj_offload" in options:
            args.append("--mmproj-offload")
    if "mmap" in options:
        args.extend(("--load-mode", "mmap" if _boolean(options["mmap"], "mmap") else "none"))
    for key, flag in _LLAMA_INTS.items():
        if key in options:
            args.extend((flag, str(_integer(options[key], key, 0 if key == "ctx_size" else 1))))
    flash_attn = None
    if "flash_attn" in options:
        flash_attn = _boolean(options["flash_attn"], "flash_attn")
        args.extend(("--flash-attn", "on" if flash_attn else "off"))
    for key, flag in (("cache_type_k", "--cache-type-k"), ("cache_type_v", "--cache-type-v")):
        if key in options:
            cache_type = _choice(options[key], key, _LLAMA_CACHE_TYPES)
            is_quantized = cache_type not in {"f32", "f16", "bf16", "f8_e4m3"}
            if key == "cache_type_v" and flash_attn is False and is_quantized:
                raise ValueError("quantized cache_type_v requires flash_attn")
            args.extend((flag, cache_type))
    if "cache_ram_mib" in options:
        args.extend(("--cache-ram", str(_integer(options["cache_ram_mib"], "cache_ram_mib", -1))))
    if "jinja" in options or "chat_template" in files:
        jinja = _boolean(options.get("jinja", True), "jinja")
        if not jinja and "chat_template" in files:
            raise ValueError("a chat_template artifact requires jinja to be enabled")
        args.append("--jinja" if jinja else "--no-jinja")
    if "chat_template" in files:
        args.extend(("--chat-template-file", _one_path(files, "chat_template")))
    _llama_speculation(args, options, files, gpu)
    if "pooling" in options:
        args.extend(("--pooling", _choice(options["pooling"], "pooling",
                                         {"none", "mean", "cls", "last", "rank"})))
    for key, flag in {"embedding": "--embedding", "metrics": "--metrics"}.items():
        if key in options and _boolean(options[key], key):
            args.append(flag)


def _diffusion(args: list[str], options: dict, files: dict, gpu: str | None) -> None:
    _known(options, set(_SD_INTS) | {
        "mmap", "offload_to_cpu", "vae_tiling", "flash_attn", "cfg_scale",
        "sampling_method", "sigmas",
    }, "stable-diffusion.cpp options")
    _known(files, _SD_COMPONENTS.keys(), "stable-diffusion.cpp artifact roles")
    if ("model" in files) == ("diffusion_model" in files):
        raise ValueError("stable-diffusion.cpp requires exactly one of model or diffusion_model")
    for role, flag in _SD_COMPONENTS.items():
        if role in files:
            args.extend((flag, _one_path(files, role)))
    # Do not let automatic fitting spill onto another device or silently
    # choose an offload strategy different from the configured one.
    args.extend(("--backend", "cuda0" if gpu else "cpu", "--auto-fit", "off"))
    for key, flag in {"mmap": "--mmap", "offload_to_cpu": "--offload-to-cpu",
                      "vae_tiling": "--vae-tiling"}.items():
        if key in options and _boolean(options[key], key):
            if key == "offload_to_cpu" and gpu is None:
                raise ValueError("offload_to_cpu requires a GPU profile")
            args.append(flag)
    for key, flag in _SD_INTS.items():
        if key in options:
            args.extend((flag, str(_integer(options[key], key))))
    if "flash_attn" in options:
        mode = options["flash_attn"]
        if type(mode) is bool:
            mode = "on" if mode else "off"
        args.extend(("--flash-attn", _choice(mode, "flash_attn",
                                             {"on", "off", "auto", "off,diffusion=on"})))
    if "sampling_method" in options:
        args.extend(("--sampling-method", _choice(options["sampling_method"], "sampling_method",
                                                  {"euler", "euler_a"})))
    if "sigmas" in options:
        args.extend(("--sigmas", _choice(options["sigmas"], "sigmas", {
            "qwen-image-2.1-turbo", "p-h3-image", "minimax-h3-image", "hyperflow-h3-flash-4",
            "hyperflow-h3-flash-6", "hyperflow-h3-flash-8",
        })))
    if "cfg_scale" in options:
        scale = options["cfg_scale"]
        if type(scale) not in (int, float) or not 0 <= scale <= 3.4028234e38 or not math.isfinite(scale):
            raise ValueError("cfg_scale must be a finite nonnegative float")
        args.extend(("--cfg-scale", str(scale)))


def _template_args(
    raw: object, files: dict, host: str, port: int | None, gpus: tuple[str, ...], name: str,
):
    """Expand whole-token arguments without a shell.

    ``{artifact:ROLE}`` expands to one argument per resolved path. ``{gpu}``
    expands to the comma-separated CUDA UUID list, matching
    CUDA_VISIBLE_DEVICES, and is an error for CPU profiles.
    """
    if not isinstance(raw, list):
        raise ValueError(f"{name} argv must be an array of argument strings")
    for arg in raw:
        token = _text(arg, f"{name} argv item", empty=True)
        if token.startswith("{artifact:") and token.endswith("}"):
            yield from map(str, _paths(files, token[10:-1]))
        elif token == "{host}":
            yield host
        elif token == "{port}":
            if port is None:
                raise ValueError(f"{name} {{port}} requires a profile port")
            yield str(port)
        elif token == "{gpu}":
            if not gpus:
                raise ValueError(f"{name} {{gpu}} requires a GPU profile")
            yield ",".join(gpus)
        else:
            yield token


def _container_env(raw: object) -> dict[str, str]:
    env = {}
    for key, value in _object(raw, "env").items():
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key):
            raise ValueError("invalid container environment key")
        if key in _MANAGED_ENV:
            raise ValueError(f"{key} is managed by the adapter")
        env[key] = _text(value, f"env.{key}", empty=True)
    return env


def _container_name(identity: str) -> str:
    label = re.sub(r"[^a-z0-9-]+", "-", identity.split("\0", 1)[0].lower()).strip("-")[:30] or "model"
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"lmh-{label}-{suffix}"


def _mounts(files: dict) -> tuple[tuple[str, str], ...]:
    """Map each engine-visible path to its verified host file.

    Hugging Face cache entries are symlinks into a blob directory. The bind
    source is the resolved file, while the destination keeps the path the
    engine arguments name, so the container sees the model where the catalog
    expects it.
    """
    mounts: dict[str, str] = {}
    for paths in files.values():
        for path in paths if isinstance(paths, list) else [paths]:
            try:
                source = str(Path(path).resolve())
            except OSError:
                raise ValueError("container artifact path cannot be resolved") from None
            target = str(Path(path))
            # Podman's --mount uses commas as field separators.
            if any(char in source + target for char in ",\n\r\0"):
                raise ValueError("container artifact paths cannot contain commas or line breaks")
            if mounts.get(target, source) != source:
                raise ValueError(f"container mount target conflicts with another artifact: {target}")
            mounts[target] = source
    return tuple(sorted(((source, target) for target, source in mounts.items()),
                        key=lambda pair: pair[1]))


def _podman_launch(
    podman: str,
    runtime: dict,
    files: dict,
    gpus: tuple[str, ...],
    identity: str,
    engine: list[str],
    *,
    cpu_count: int | None = None,
    container_env: dict[str, str] | None = None,
) -> tuple[list[str], Container]:
    """Build the single Podman launch shared by every container adapter.

    GPU profiles receive one CDI device per selected GPU UUID, never the
    ``all`` device, so the engine cannot see a GPU the profile did not select.
    CPU profiles receive no device at all and an explicitly empty visibility
    list. Each container gets its own cgroup so a CPU quota can be applied.
    """
    image = image_reference(runtime.get("image"))
    mounts = _mounts(files)
    name = _container_name(identity)
    argv = [
        podman, "run", "--rm", "--replace", f"--name={name}", "--network=host",
        "--pull=never", "--http-proxy=false",
        # The host owns mounts and the image owns its root filesystem; keep
        # SELinux relabeling off so read-only model binds work on enforcing hosts.
        "--security-opt=no-new-privileges", "--security-opt=label=disable",
    ]
    if cpu_count is not None:
        argv.append(f"--cpus={cpu_count}")
    for gpu in gpus:
        # Rootless GPU access uses a CDI device, not the host's device files.
        # One UUID device per selected GPU keeps siblings out of the container.
        argv.append(f"--device=nvidia.com/gpu={gpu}")
    argv.extend(f"--mount=type=bind,src={source},dst={target},readonly=true"
                for source, target in mounts)
    visible = ",".join(gpus)
    argv.extend((
        "--env=CUDA_DEVICE_ORDER=PCI_BUS_ID",
        f"--env=CUDA_VISIBLE_DEVICES={visible}",
        f"--env=NVIDIA_VISIBLE_DEVICES={visible}",
    ))
    argv.extend(f"--env={key}={value}" for key, value in (container_env or {}).items())
    argv.extend((image, *engine))
    return argv, Container(image, name, tuple(engine), mounts, gpus)


def build_launch(
    runtime: dict,
    profile: dict,
    files: dict[str, Path | list[Path]],
    gpu_id: str | list[str] | tuple[str, ...] | None = None,
    *,
    trusted_native: bool = False,
    identity: str = "probe",
    podman: str = "podman",
    cpu_count: int | None = None,
) -> Launch:
    """Build argv without executing the runtime or inspecting its binary.

    Every container adapter requires a digest-pinned ``runtime.image`` approved
    by the controller; every artifact path is bind-mounted read-only at the same
    path inside the container. ``runtime.executable``, if set, is an absolute
    in-container command placed before the engine arguments. ``identity``
    (model/profile) names the container deterministically.

    ``container`` is the generic adapter for any OpenAI-compatible server:
    ``runtime.executable`` plus ``runtime.argv`` form the in-container command,
    and ``runtime.env`` becomes container environment. Typed adapters translate
    their options into the same command stream and share this launch path.
    Portable token substitutions for generic argv: ``{host}``, ``{port}``,
    ``{gpu}`` (comma-separated selected UUIDs), ``{artifact:ROLE}`` (one arg per
    path). ``container`` profiles must declare a port and default to a
    ``/health`` probe.

    A controller-resolved gpu_id (one UUID or a list) takes precedence over
    profile.gpu, which may also be a list. CPU profiles select no GPU. GPU
    profiles get one CDI device per selected UUID; typed adapters pin exactly
    one GPU because their flags address ``CUDA0``. ``cpu_count`` limits a
    CPU-only container; an explicit ``resources.cpu_count`` wins.

    llama.cpp flags target upstream b12359. A fit target is an engine estimate,
    not a resource reservation; the engine does not override fixed layer or
    context sizes. A resolved chat_template enables Jinja, and a draft_model
    selects draft-simple (the only typed mode that accepts a draft_model).
    Native approval must come from the controller, never a catalog field.
    For native, runtime.argv is the argument tail after executable.
    Native runtimes are trusted to honor CUDA visibility and the profile.
    """
    runtime = _object(runtime, "runtime")
    profile = _object(profile, "profile")
    files = _object(files, "files")
    adapter = _text(runtime.get("adapter"), "adapter")
    if adapter == "systemd":
        raise ValueError("systemd runtimes must be adopted by the controller")
    if adapter not in CONTAINER_ADAPTERS | {"native"}:
        raise ValueError(f"unsupported runtime adapter: {adapter}")
    if adapter == "native" and trusted_native is not True:
        raise ValueError("native runtimes require controller approval of local config")
    allowed = ({"native": _NATIVE_FIELDS, "container": _GENERIC_FIELDS}
               .get(adapter, _RUNTIME_FIELDS))
    _known(runtime, allowed, "runtime fields")
    if adapter == "native" and "image" in runtime:
        raise ValueError("native runtimes cannot use container images")
    for key in ("args", "argv", "extra_argv", "extra_args", "env", "environment",
                "cwd", "executable", "ld_library_path"):
        if key in profile:
            raise ValueError(f"{key} is not a profile field")
    executable = (str(_absolute(runtime["executable"], "executable"))
                  if "executable" in runtime else None)
    if adapter == "native" and executable is None:
        raise ValueError("native runtimes require an executable")
    cwd = _absolute(runtime["cwd"], "cwd") if "cwd" in runtime else None
    runtime_opts = _object(runtime.get("options", {}), "runtime options")
    profile_opts = _object(profile.get("options", {}), "profile options")
    options = {**runtime_opts, **profile_opts}
    gpus = _device(profile, gpu_id)
    if adapter in {"llama.cpp", "stable-diffusion.cpp"} and len(gpus) > 1:
        raise ValueError(f"{adapter} profiles support exactly one GPU")
    gpu = gpus[0] if gpus else None
    default_port, default_probe = CONTAINER_ENGINE_DEFAULTS.get(adapter, (None, None))
    if adapter == "container" and "port" not in profile:
        raise ValueError("container profiles require a port")
    host, port, health_url, timeout = _network(profile, default_port, default_probe)
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "XDG_CACHE_HOME",
                    "XDG_RUNTIME_DIR"}
           or k.startswith("LC_")}
    env.update(CUDA_VISIBLE_DEVICES=",".join(gpus), CUDA_DEVICE_ORDER="PCI_BUS_ID")
    # Rootless Podman needs the per-user runtime directory even under env -i.
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    engine: list[str] = []
    if adapter == "native":
        _known(options, set(), "native options")
        engine.append(executable)
        engine.extend(_template_args(runtime.get("argv", []), files, host, port, gpus, "native"))
        for key, value in _object(runtime.get("env", {}), "env").items():
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key):
                raise ValueError("invalid native environment key")
            if key in _MANAGED_ENV:
                raise ValueError(f"{key} is managed by the adapter")
            env[key] = _text(value, f"env.{key}", empty=True)
        return Launch(tuple(engine), env, cwd, health_url, timeout, None)

    image_reference(runtime.get("image"))
    resources = _object(profile.get("resources", {}), "resources")
    limit = resources.get("cpu_count")
    if limit is not None:
        limit = _integer(limit, "resources.cpu_count", 1, 4096)
    elif not gpus:
        limit = cpu_count if isinstance(cpu_count, int) and cpu_count > 0 else None
    container_env: dict[str, str] = {}
    if executable:
        engine.append(executable)
    if adapter == "container":
        _known(options, set(), "container options")
        engine.extend(_template_args(runtime.get("argv", []), files, host, port, gpus, "container"))
        container_env = _container_env(runtime.get("env", {}))
    elif adapter == "llama.cpp":
        engine.extend(("--host", host, "--port", str(port)))
        _llama(engine, options, files, gpu)
    else:
        engine.extend(("--listen-ip", host, "--listen-port", str(port)))
        _diffusion(engine, options, files, gpu)
    argv, container = _podman_launch(
        podman, runtime, files, gpus, identity, engine,
        cpu_count=limit, container_env=container_env,
    )
    return Launch(tuple(argv), env, None, health_url, timeout, None, container=container)
