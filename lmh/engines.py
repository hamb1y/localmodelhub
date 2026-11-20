"""Translate local runtime configuration into process and probe specifications.

The controller resolves artifacts, checks executable integrity and runs the
specification with shell=False. Pass Launch.env as the complete environment,
not as overrides on the controller environment.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

LLAMA_ENGINE_PORT = 18080

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


_UUID = re.compile(
    r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_ORDINAL = re.compile(r"0|[1-9][0-9]{0,8}")
_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")
_SHARD = re.compile(r"(.+)-([0-9]{5})-of-([0-9]{5})\.gguf")
_RUNTIME_FIELDS = {"adapter", "executable", "version", "sha256", "options", "cwd"}
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


def _device(profile: dict, gpu_id: str | None) -> str | None:
    device = profile.get("device")
    if device == "cpu":
        if gpu_id is not None or "gpu" in profile:
            raise ValueError("a CPU profile cannot select a GPU")
        return None
    if device != "gpu":
        raise ValueError("device must be 'cpu' or 'gpu'")
    configured = _gpu_selector(profile["gpu"]) if "gpu" in profile else None
    selected = _gpu_selector(gpu_id if gpu_id is not None else configured)
    if configured and selected != configured:
        # The controller may resolve an index to a UUID, but cannot replace an
        # explicit UUID or substitute a different ordinal.
        if not (_ORDINAL.fullmatch(configured) and _UUID.fullmatch(selected)):
            raise ValueError("resolved GPU differs from the requested GPU")
    return selected


def _gpu_selector(value: object) -> str:
    if isinstance(value, str):
        if _UUID.fullmatch(value):
            return "GPU-" + value[4:].lower()
        if _ORDINAL.fullmatch(value):
            return value
    raise ValueError("GPU profiles require a single CUDA GPU UUID or ordinal")


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


def _native_args(raw: object, files: dict, host: str, port: int | None, gpu: str | None):
    if not isinstance(raw, list):
        raise ValueError("native argv must be an array of argument strings")
    for arg in raw:
        token = _text(arg, "native argv item", empty=True)
        if token.startswith("{artifact:") and token.endswith("}"):
            yield from map(str, _paths(files, token[10:-1]))
        elif token == "{host}":
            yield host
        elif token == "{port}":
            if port is None:
                raise ValueError("native {port} requires a profile port")
            yield str(port)
        elif token == "{gpu}":
            if gpu is None:
                raise ValueError("native {gpu} requires a GPU profile")
            yield gpu
        else:
            yield token


def build_launch(
    runtime: dict,
    profile: dict,
    files: dict[str, Path | list[Path]],
    gpu_id: str | None = None,
    *,
    trusted_native: bool = False,
) -> Launch:
    """Build argv without executing the runtime or inspecting its binary.

    A controller-resolved gpu_id takes precedence over profile.gpu. Prefer a
    physical CUDA UUID; numeric selectors are CUDA PCI-bus-order ordinals.
    llama.cpp flags target upstream b12359. A fit target is an engine estimate,
    not a resource reservation; the engine does not override fixed layer or
    context sizes. A resolved chat_template enables Jinja, and a draft_model
    selects draft-simple (the only typed mode that accepts a draft_model).
    Native approval must come from the controller, never a catalog field.
    For native, runtime.argv is the argument tail after executable. Whole-token
    substitutions: {host}, {port}, {gpu}, {artifact:ROLE} (one arg per path).
    Native runtimes are trusted to honor CUDA visibility and the profile.
    """
    runtime = _object(runtime, "runtime")
    profile = _object(profile, "profile")
    files = _object(files, "files")
    adapter = _text(runtime.get("adapter"), "adapter")
    if adapter == "systemd":
        raise ValueError("systemd runtimes must be adopted by the controller")
    if adapter not in {"llama.cpp", "stable-diffusion.cpp", "native"}:
        raise ValueError(f"unsupported runtime adapter: {adapter}")
    if adapter == "native" and trusted_native is not True:
        raise ValueError("native runtimes require controller approval of local config")
    _known(runtime, _RUNTIME_FIELDS | ({"argv", "env"} if adapter == "native" else set()),
           "runtime fields")
    for key in ("args", "argv", "extra_argv", "extra_args", "env", "environment",
                "cwd", "executable", "ld_library_path"):
        if key in profile:
            raise ValueError(f"{key} is not a profile field")
    executable = str(_absolute(runtime.get("executable"), "executable"))
    cwd = _absolute(runtime["cwd"], "cwd") if "cwd" in runtime else None
    runtime_opts = _object(runtime.get("options", {}), "runtime options")
    profile_opts = _object(profile.get("options", {}), "profile options")
    options = {**runtime_opts, **profile_opts}
    gpu = _device(profile, gpu_id)
    default_port, default_probe = {
        "llama.cpp": (LLAMA_ENGINE_PORT, "/health"),
        "stable-diffusion.cpp": (1234, "/v1/models"),
        "native": (None, None),
    }[adapter]
    host, port, health_url, timeout = _network(profile, default_port, default_probe)
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "XDG_CACHE_HOME"}
           or k.startswith("LC_")}
    args = [executable]
    version_argv = None
    if adapter == "native":
        _known(options, set(), "native options")
        args.extend(_native_args(runtime.get("argv", []), files, host, port, gpu))
        for key, value in _object(runtime.get("env", {}), "env").items():
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key):
                raise ValueError("invalid native environment key")
            if key in {"CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER"}:
                raise ValueError(f"{key} is managed by the adapter")
            env[key] = _text(value, f"env.{key}", empty=True)
    else:
        version_argv = (executable, "--version")
        if adapter == "llama.cpp":
            args.extend(("--host", host, "--port", str(port)))
            _llama(args, options, files, gpu)
        else:
            args.extend(("--listen-ip", host, "--listen-port", str(port)))
            _diffusion(args, options, files, gpu)
    env.update(CUDA_VISIBLE_DEVICES=gpu or "", CUDA_DEVICE_ORDER="PCI_BUS_ID")
    return Launch(tuple(args), env, cwd, health_url, timeout, version_argv)
