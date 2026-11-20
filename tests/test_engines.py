import os
import unittest
from pathlib import Path
from unittest.mock import patch

from lmh.engines import build_launch


GPU = "GPU-a1234567-1234-1234-1234-0123456789ab"
LLAMA = {"adapter": "llama.cpp", "executable": "/opt/llama-server"}
LLAMA_PIN = {**LLAMA, "version": "b12359"}
SD = {"adapter": "stable-diffusion.cpp", "executable": "/opt/sd-server"}
NATIVE = {"adapter": "native", "executable": "/opt/native"}
MODEL = {"model": Path("/models/name with spaces.gguf")}


class EngineTests(unittest.TestCase):
    def test_llama_cpu_enforces_device_and_drops_ambient_engine_overrides(self):
        with patch.dict(os.environ, {
            "CUDA_VISIBLE_DEVICES": GPU, "LLAMA_ARG_N_GPU_LAYERS": "99",
            "MTMD_BACKEND_DEVICE": "CUDA0", "GGML_VK_VISIBLE_DEVICES": "0",
        }):
            launch = build_launch(
                LLAMA,
                {"device": "cpu", "port": 8899, "options": {"mmap": False, "ctx_size": 4096}},
                {**MODEL, "mmproj": Path("/models/mmproj.gguf")},
            )
        self.assertEqual(launch.env["CUDA_VISIBLE_DEVICES"], "")
        for key in ("LLAMA_ARG_N_GPU_LAYERS", "MTMD_BACKEND_DEVICE", "GGML_VK_VISIBLE_DEVICES"):
            self.assertNotIn(key, launch.env)
        self.assertEqual(launch.argv, (
            "/opt/llama-server", "--host", "127.0.0.1", "--port", "8899",
            "--device", "none", "--n-gpu-layers", "0", "--no-op-offload", "--no-kv-offload",
            "--model", str(MODEL["model"]), "--mmproj", "/models/mmproj.gguf",
            "--no-mmproj-offload", "--load-mode", "none", "--ctx-size", "4096",
        ))
        self.assertEqual(launch.health_url, "http://127.0.0.1:8899/health")
        self.assertEqual(launch.health_timeout, 120.0)
        self.assertEqual(launch.version_argv, ("/opt/llama-server", "--version"))
        self.assertEqual(launch.log_streams, ("stdout", "stderr"))

    def test_llama_gpu_uses_resolved_uuid_and_only_one_visible_cuda_device(self):
        launch = build_launch(
            {**LLAMA, "options": {"threads": 3, "gpu_layers": 30}},
            {"device": "gpu", "gpu": "1", "options": {"threads": 5, "mmap": True}},
            MODEL, gpu_id=GPU,
        )
        self.assertEqual(launch.env["CUDA_VISIBLE_DEVICES"], GPU)
        flags = dict(zip(launch.argv[1::2], launch.argv[2::2]))
        self.assertEqual(flags["--device"], "CUDA0")
        self.assertEqual(flags["--split-mode"], "none")
        self.assertEqual(flags["--n-gpu-layers"], "30")
        self.assertEqual(flags["--load-mode"], "mmap")
        self.assertEqual(flags["--threads"], "5")

    def test_gpu_selector_is_required_and_single(self):
        for selector in (None, "", "-1", "0,1", "GPU-a123", 0):
            with self.subTest(selector=selector), self.assertRaisesRegex(ValueError, "single CUDA"):
                build_launch(LLAMA, {"device": "gpu", "gpu": selector}, MODEL)
        for kwargs in ({"gpu_id": GPU}, {}):
            with self.assertRaisesRegex(ValueError, "CPU profile"):
                build_launch(LLAMA, {"device": "cpu", "gpu": "0"}, MODEL, **kwargs)
        with self.assertRaisesRegex(ValueError, "differs"):
            build_launch(LLAMA, {"device": "gpu", "gpu": GPU}, MODEL, gpu_id="0")

    def test_llama_embedding_pooling(self):
        launch = build_launch(
            LLAMA, {"device": "cpu", "options": {"embedding": True, "pooling": "mean"}}, MODEL,
        )
        self.assertEqual(launch.argv[-3:], ("--pooling", "mean", "--embedding"))

    def test_llama_b12359_cache_and_batch_options(self):
        launch = build_launch(
            LLAMA_PIN, {"device": "cpu", "options": {
                "cache_type_k": "q8_0", "cache_type_v": "f8_e4m3", "cache_ram_mib": 0,
                "batch_size": 2048, "ubatch_size": 512, "threads_batch": 6,
            }}, MODEL,
        )
        for flag, value in {
            "--cache-type-k": "q8_0", "--cache-type-v": "f8_e4m3", "--cache-ram": "0",
            "--batch-size": "2048", "--ubatch-size": "512", "--threads-batch": "6",
        }.items():
            self.assertEqual(launch.argv[launch.argv.index(flag) + 1], value)

    def test_llama_fit_uses_auto_layers_only_when_not_fixed(self):
        for options, flags in (
            ({"fit_target_mib": 1536, "fit_ctx": 2048},
             {"--fit": "on", "--fit-target": "1536", "--fit-ctx": "2048", "--n-gpu-layers": "auto"}),
            ({"fit": True, "gpu_layers": 12}, {"--fit": "on", "--n-gpu-layers": "12"}),
            ({"fit": False}, {"--fit": "off", "--n-gpu-layers": "all"}),
        ):
            with self.subTest(options=options):
                launch = build_launch(LLAMA_PIN, {"device": "gpu", "options": options}, MODEL, GPU)
                for flag, value in flags.items():
                    self.assertEqual(launch.argv[launch.argv.index(flag) + 1], value)

    def test_llama_gpu_can_keep_vision_on_cpu_and_use_template_artifact(self):
        launch = build_launch(
            LLAMA_PIN, {"device": "gpu", "options": {"mmproj_offload": False}},
            {**MODEL, "mmproj": Path("/models/mmproj.gguf"),
             "chat_template": Path("/models/template with spaces.jinja")}, GPU,
        )
        self.assertIn("--no-mmproj-offload", launch.argv)
        self.assertNotIn("--mmproj-offload", launch.argv)
        self.assertEqual(launch.argv[launch.argv.index("--jinja"):], (
            "--jinja", "--chat-template-file", "/models/template with spaces.jinja",
        ))
        disabled = build_launch(LLAMA_PIN, {"device": "cpu", "options": {"jinja": False}}, MODEL)
        self.assertIn("--no-jinja", disabled.argv)

    def test_llama_b12359_draft_model_is_pinned_and_uses_current_flags(self):
        for device, gpu, backend, layers in (("cpu", None, "none", "0"), ("gpu", GPU, "CUDA0", "auto")):
            with self.subTest(device=device):
                launch = build_launch(
                    LLAMA_PIN, {"device": device, "options": {
                        "spec_draft_n_min": 1, "spec_draft_n_max": 5,
                    }},
                    {**MODEL, "draft_model": Path("/models/draft.gguf")}, gpu,
                )
                self.assertEqual(launch.argv[-12:], (
                    "--spec-type", "draft-simple", "--spec-draft-model", "/models/draft.gguf",
                    "--spec-draft-device", backend, "--spec-draft-ngl", layers,
                    "--spec-draft-n-min", "1", "--spec-draft-n-max", "5",
                ))
        ngram = build_launch(LLAMA_PIN, {"device": "cpu", "options": {"spec_type": "ngram-simple"}}, MODEL)
        self.assertEqual(ngram.argv[-2:], ("--spec-type", "ngram-simple"))

    def test_llama_rejects_unsupported_settings_and_conflicting_roles(self):
        draft = {**MODEL, "draft_model": Path("/models/draft.gguf")}
        template = {**MODEL, "chat_template": Path("/models/chat.jinja")}
        for options, files in (
            ({"cache_type_k": "turbo3"}, MODEL),
            ({"cache_type_v": "q4_0", "flash_attn": False}, MODEL),
            ({"cache_ram_mib": True}, MODEL),
            ({"cache_ram_mib": -2}, MODEL),
            ({"threads_batch": "8"}, MODEL),
            ({"fit": False, "fit_target_mib": 1024}, MODEL),
            ({"fit_target_mib": [1024, 2048]}, MODEL),
            ({"mmproj_offload": False}, MODEL),
            ({"mmproj_offload": True}, {**MODEL, "mmproj": Path("/models/mmproj.gguf")}),
            ({"jinja": False}, template),
            ({"spec_type": "draft-simple"}, MODEL),
            ({"spec_type": "draft-dflash2"}, draft),
            ({"spec_type": "none"}, draft),
            ({"spec_draft_n_max": 3}, MODEL),
            ({"spec_draft_n_min": 4}, draft),
            ({"draft_gpu_layers": 3}, draft),
        ):
            with self.subTest(options=options, files=files), self.assertRaises(ValueError):
                build_launch(LLAMA_PIN, {"device": "cpu", "options": options}, files)

    def test_llama_shards_require_full_ordered_set(self):
        first = Path("/models/gguf-00001-of-00002.gguf")
        second = Path("/models/gguf-00002-of-00002.gguf")
        launch = build_launch(LLAMA, {"device": "cpu"}, {"model": [first, second]})
        self.assertEqual(launch.argv[-1], str(first))
        for invalid in (first, [second, first], [first, Path("/other") / second.name]):
            with self.assertRaisesRegex(ValueError, "complete, ordered"):
                build_launch(LLAMA, {"device": "cpu"}, {"model": invalid})

    def test_diffusion_separates_mmap_from_weight_offload(self):
        files = {
            "diffusion_model": Path("/models/flux.gguf"),
            "clip_l": Path("/models/clip.safetensors"),
            "vae": Path("/models/vae.safetensors"),
        }
        for options, wanted, unwanted in (
            ({"mmap": True, "offload_to_cpu": False}, "--mmap", "--offload-to-cpu"),
            ({"mmap": False, "offload_to_cpu": True}, "--offload-to-cpu", "--mmap"),
        ):
            with self.subTest(options=options):
                launch = build_launch(SD, {"device": "gpu", "gpu": GPU, "options": options}, files)
                self.assertIn(wanted, launch.argv)
                self.assertNotIn(unwanted, launch.argv)
                self.assertEqual(launch.env["CUDA_VISIBLE_DEVICES"], GPU)
                self.assertEqual(launch.argv, (
                    "/opt/sd-server", "--listen-ip", "127.0.0.1", "--listen-port", "1234",
                    "--diffusion-model", "/models/flux.gguf", "--vae", "/models/vae.safetensors",
                    "--clip_l", "/models/clip.safetensors",
                    "--backend", "cuda0", "--auto-fit", "off", wanted,
                ))
                self.assertEqual(launch.health_url, "http://127.0.0.1:1234/v1/models")

    def test_diffusion_pinned_qwen_profile(self):
        files = {
            "diffusion_model": Path("/models/diffusion.gguf"),
            "llm": Path("/models/llm.gguf"),
            "llm_vision": Path("/models/mmproj.gguf"),
            "vae": Path("/models/vae.safetensors"),
        }
        launch = build_launch(
            {**SD, "version": "532d2c15c430a785a712b2d94281f04e733008a4"},
            {"device": "gpu", "options": {
                "steps": 8, "cfg_scale": 1.0, "sampling_method": "euler",
                "sigmas": "qwen-image-2.1-turbo", "flash_attn": "off,diffusion=on",
                "offload_to_cpu": True, "mmap": True, "vae_tiling": True,
                "width": 1024, "height": 1024, "threads": 8, "max_pending_jobs": 2,
            }},
            files, gpu_id=GPU,
        )
        for flag, value in {
            "--llm_vision": "/models/mmproj.gguf",
            "--sampling-method": "euler", "--sigmas": "qwen-image-2.1-turbo",
            "--flash-attn": "off,diffusion=on", "--width": "1024", "--height": "1024",
            "--threads": "8", "--max-pending-jobs": "2",
        }.items():
            self.assertEqual(launch.argv[launch.argv.index(flag) + 1], value)

    def test_diffusion_cpu_pins_backend_even_with_gpu_build(self):
        launch = build_launch(
            SD, {"device": "cpu", "options": {"mmap": True, "cfg_scale": 1.5}}, MODEL,
        )
        backend = launch.argv.index("--backend")
        self.assertEqual(launch.argv[backend + 1], "cpu")
        self.assertEqual(launch.env["CUDA_VISIBLE_DEVICES"], "")
        self.assertNotIn("--offload-to-cpu", launch.argv)
        self.assertIn("--mmap", launch.argv)

    def test_typed_adapters_reject_unknown_or_incompatible_options(self):
        for runtime, opts in (
            (LLAMA, {"mmap": "false"}),
            (LLAMA, {"gpu_layers": 1}),
            (LLAMA, {"kv_offload": True}),
            (LLAMA, {"threads": True}),
            (LLAMA, {"pooling": "unknown"}),
            (LLAMA, {"offload_to_cpu": True}),
            (LLAMA, {"args": ["--n-gpu-layers", "99"]}),
            (SD, {"offload_to_cpu": True}),
            (SD, {"ctx_size": 1024}),
            (SD, {"cfg_scale": float("nan")}),
            (SD, {"width": 0}),
            (SD, {"max_pending_jobs": "2"}),
            (SD, {"sigmas": "1,0"}),
            (SD, {"sampling_method": "--offload-to-cpu"}),
            (SD, {"flash_attn": "off,unknown=on"}),
        ):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                build_launch(runtime, {"device": "cpu", "options": opts}, MODEL)
        with self.assertRaisesRegex(ValueError, "unsupported runtime fields"):
            build_launch({**LLAMA, "env": {}}, {"device": "cpu"}, MODEL)

    def test_invalid_artifact_roles_and_paths_are_not_silently_ignored(self):
        for runtime, files in (
            (LLAMA, {"checkpoint": MODEL["model"]}),
            (LLAMA, {"model": Path("relative.gguf")}),
            (SD, {**MODEL, "diffusion_model": Path("/models/flux.gguf")}),
            (SD, {"model": [Path("/models/a"), Path("/models/b")]}),
        ):
            with self.subTest(files=files), self.assertRaises(ValueError):
                build_launch(runtime, {"device": "cpu"}, files)

    def test_native_requires_out_of_band_approval_and_never_tokenizes_argv(self):
        runtime = {
            **NATIVE, "argv": ["{artifact:weights}", "{host}", "{port}", "x; $(whoami)"],
            "cwd": "/opt", "env": {"EXAMPLE": "literal value"},
        }
        profile = {"device": "cpu", "port": 9999, "health": {"path": "/ready"}}
        files = {"weights": [Path("/models/a model"), Path("/models/b")]}
        with self.assertRaisesRegex(ValueError, "controller approval"):
            build_launch({**runtime, "trusted_native": True}, profile, files)
        launch = build_launch(runtime, profile, files, trusted_native=True)
        self.assertEqual(launch.argv, (
            "/opt/native", "/models/a model", "/models/b", "127.0.0.1", "9999", "x; $(whoami)",
        ))
        self.assertEqual(launch.env["EXAMPLE"], "literal value")
        self.assertEqual(launch.env["CUDA_VISIBLE_DEVICES"], "")
        self.assertEqual(launch.cwd, Path("/opt"))
        self.assertEqual(launch.health_url, "http://127.0.0.1:9999/ready")
        self.assertIsNone(launch.version_argv)

    def test_native_does_not_allow_configured_visibility_override(self):
        with self.assertRaisesRegex(ValueError, "managed by the adapter"):
            build_launch(
                {**NATIVE, "env": {"CUDA_VISIBLE_DEVICES": "0,1"}},
                {"device": "cpu"}, {}, trusted_native=True,
            )

    def test_native_has_no_default_http_probe(self):
        launch = build_launch(NATIVE, {"device": "cpu"}, {}, trusted_native=True)
        self.assertIsNone(launch.health_url)

    def test_health_probe_formats_ipv6_and_wildcard(self):
        for host, expected in (("::", "[::1]"), ("::1", "[::1]"), ("0.0.0.0", "127.0.0.1")):
            with self.subTest(host=host):
                launch = build_launch(LLAMA, {
                    "device": "cpu", "host": host,
                    "health": {"path": "/ready", "timeout_seconds": 42},
                }, MODEL)
                self.assertEqual(launch.health_url, f"http://{expected}:18080/ready")
                self.assertEqual(launch.health_timeout, 42.0)

    def test_invalid_probe_options(self):
        for fields in (
            {"host": "user@example.com"}, {"port": True}, {"port": 65536},
            {"health": {"path": "//elsewhere"}}, {"health": {"path": "/ok\nbad"}},
            {"health": {"timeout_seconds": 0}}, {"health": {"timeout_seconds": False}},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                build_launch(LLAMA, {"device": "cpu", **fields}, MODEL)

    def test_systemd_and_relative_executables_are_not_launched(self):
        with self.assertRaisesRegex(ValueError, "adopted by the controller"):
            build_launch({"adapter": "systemd", "unit": "foo.service"}, {"device": "cpu"}, {})
        with self.assertRaisesRegex(ValueError, "executable must be an absolute path"):
            build_launch({**LLAMA, "executable": "llama-server"}, {"device": "cpu"}, MODEL)
