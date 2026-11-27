from __future__ import annotations

import copy
import json
import fcntl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmh.catalog import CatalogError, validate_catalog
from lmh.controller import Controller, MIB, _check_config

IMAGE = "ghcr.io/example/llama.cpp:server@sha256:" + "a" * 64
GPU_0 = "GPU-00000000-0000-0000-0000-000000000000"
GPU_1 = "GPU-11111111-1111-1111-1111-111111111111"


def _gpu(free=16000):
    return {"cpu": "test", "cpu_logical_count": 8,
            "ram_total_bytes": 32 * 1024 * MIB, "ram_available_bytes": 16 * 1024 * MIB,
            "gpus": [{"id": "GPU-test", "index": 0, "name": "test GPU",
                      "total_bytes": 16000 * MIB, "free_bytes": free * MIB}]}


def _gpus(*free):
    ids = (GPU_0, GPU_1, "GPU-22222222-2222-2222-2222-222222222222")
    return {"cpu": "test", "cpu_logical_count": 16,
            "ram_total_bytes": 64 * 1024 * MIB, "ram_available_bytes": 32 * 1024 * MIB,
            "gpus": [{"id": ids[index], "index": index, "name": f"GPU {index}",
                      "total_bytes": 16000 * MIB, "free_bytes": value * MIB}
                     for index, value in enumerate(free)]}


class FakeSupervisor:
    def __init__(self):
        self.states = {}
        self.actions = []
        self.fail_start = None

    def observe(self, model, profile, runtime):
        return {"state": self.states.get((model, profile), "stopped")}

    def start(self, model, profile, runtime, launch, options):
        self.actions.append(("start", model, profile))
        self.states[model, profile] = "failed" if self.fail_start == model else "running"

    def stop(self, model, profile, runtime):
        self.actions.append(("stop", model, profile))
        self.states[model, profile] = "stopped"

    def logs(self, model, profile, runtime):
        return ["test log"]


def catalog(boot=()):
    profiles = {}
    runtimes = {}
    for name, device, vram in [("alpha", "gpu", 9500), ("beta", "gpu", 8500),
                                ("small", "gpu", 2500), ("cpu-model", "cpu", None)]:
        runtime = {"adapter": "systemd", "unit": f"{name}.service"}
        runtimes[name] = runtime
        options = {"runtime": name, "artifacts": {}, "device": device}
        if vram:
            options["vram_mib"] = vram
        profiles[name] = {"name": name, "io": [{"input": ["text"], "output": "text"}],
                          "default_profile": "default", "profiles": {"default": options}}
    return {"schema_version": 1, "runtimes": runtimes, "artifacts": {}, "models": profiles,
            "boot": [{"model": key, "profile": "default"} for key in boot]}


class FakeContainer:
    executable = "/usr/bin/podman"

    def __init__(self):
        self.ensured = []

    def ensure(self, reference, progress=lambda _: None):
        self.ensured.append(reference)
        return reference


class FakeStore:
    def resolve(self, artifact, download=False):
        return Path("/models/model.gguf")


def container_catalog(*, image=IMAGE):
    return {
        "schema_version": 1,
        "runtimes": {"llama": {"adapter": "llama.cpp", "image": image}},
        "artifacts": {"weights": {"source": {"kind": "local", "path": "/models/model.gguf"}}},
        "models": {
            "chat": {
                "name": "Chat",
                "io": [{"input": ["text"], "output": "text"}],
                "profiles": {
                    "cpu": {"runtime": "llama", "device": "cpu", "artifacts": {"model": "weights"},
                            "host": "127.0.0.1", "port": 38123},
                },
            },
        },
        "boot": [],
    }


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / "models.json"
        self.driver = FakeSupervisor()
        self.policy = {"adopted": {f"{name}.service": "allowlisted" for name in catalog()["models"]}}

    def controller(self, boot=(), *, boot_id="boot1", free=16000):
        self.cfg.write_text(json.dumps(catalog(boot)))
        controller = Controller(self.cfg, self.root / "state", self.policy, driver=self.driver,
                                hardware=lambda: _gpu(free), artifact_store=object(),
                                boot_reader=lambda: boot_id, ready=lambda url: True)
        self.addCleanup(controller.shutdown)
        return controller

    def test_boot_starts_only_missing_non_displacing_entries_and_runs_once(self):
        c = self.controller(("alpha", "beta", "cpu-model"))
        self.driver.states["alpha", "default"] = "running"
        c.reconcile()
        self.assertEqual(self.driver.actions, [("start", "cpu-model", "default")])
        self.assertIn("1 started, 1 already active, 1 blocked", c.boot["message"])
        self.driver.states["cpu-model", "default"] = "stopped"
        self.controller(("beta",)).reconcile()
        self.assertEqual(self.driver.actions, [("start", "cpu-model", "default")])

    def test_invalid_boot_config_stops_no_services(self):
        c = self.controller()
        self.cfg.write_text("{\"schema_version\": 900}")
        c.reconcile()
        self.assertEqual(c.boot["state"], "failed")
        self.assertEqual(self.driver.actions, [])
        with self.assertRaises(RuntimeError):
            c.submit("start", "alpha")

    def test_start_can_share_gpu_but_cannot_evict(self):
        c = self.controller(free=6500)
        c.reconcile()
        self.driver.states["alpha", "default"] = "running"
        with self.assertRaisesRegex(RuntimeError, "do not fit"):
            c._operate("start", ("beta", "default"), None)
        self.assertEqual(self.driver.actions, [])
        c._operate("start", ("small", "default"), None)
        self.assertEqual(self.driver.actions, [("start", "small", "default")])

    def test_explicit_switch_stops_exact_source(self):
        c = self.controller()
        c.reconcile()
        self.driver.states["alpha", "default"] = "running"
        self.driver.states["cpu-model", "default"] = "running"
        c._operate("switch", ("beta", "default"), ("alpha", "default"))
        self.assertEqual(self.driver.actions, [("stop", "alpha", "default"), ("start", "beta", "default")])
        self.assertEqual(self.driver.states["cpu-model", "default"], "running")

    def test_failed_switch_does_not_auto_restore(self):
        c = self.controller()
        c.reconcile()
        self.driver.states["alpha", "default"] = "running"
        self.driver.fail_start = "beta"
        with self.assertRaisesRegex(RuntimeError, "Restore the previous model manually"):
            c._operate("switch", ("beta", "default"), ("alpha", "default"))
        self.assertEqual(self.driver.actions, [("stop", "alpha", "default"), ("start", "beta", "default")])

    def test_staging_only_applies_on_next_os_boot_without_imported_autostart(self):
        c = self.controller()
        c.reconcile()
        incoming = json.loads((Path(__file__).resolve().parents[1] / "catalogs/starter.json").read_text())
        incoming["boot"] = [{"model": "qwen2.5-chat", "profile": "cpu"}]
        self.assertEqual(c.stage_catalog(incoming)["state"], "staged")
        same = self.controller()
        same.reconcile()
        self.assertIn("alpha", same.config["models"])
        new = self.controller(boot_id="boot2")
        new.reconcile()
        self.assertNotIn("alpha", new.config["models"])
        self.assertIn("qwen2.5-chat", new.config["models"])
        self.assertEqual(self.driver.actions, [])

    def test_queue_deduplicates_and_requires_source_for_switch(self):
        c = self.controller()
        c.reconcile()
        with self.assertRaisesRegex(ValueError, "explicit source"):
            c.submit("switch", "alpha", "default")
        first = c.submit("start", "alpha", "default")
        second = c.submit("start", "alpha", "default")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(c.state.jobs()), 1)

    def test_app_updates_and_lifecycle_jobs_cannot_overlap(self):
        c = self.controller()
        c.reconcile()
        with (c.state_dir / "maintenance.lock").open("a") as update:
            fcntl.flock(update, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "applying an update"):
                c.submit("start", "alpha")
            self.assertEqual(c.state.jobs(), [])
            fcntl.flock(update, fcntl.LOCK_UN)
            c.submit("start", "alpha")
            with self.assertRaises(BlockingIOError):
                fcntl.flock(update, fcntl.LOCK_EX | fcntl.LOCK_NB)
            c.shutdown()
            fcntl.flock(update, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def container_controller(self, *, image=IMAGE, approved=IMAGE):
        self.cfg.write_text(json.dumps(container_catalog(image=image)))
        self.container = FakeContainer()
        policy = {"runtimes": ({"llama": {"adapter": "llama.cpp", "image": approved}}
                               if approved is not None else {})}
        controller = Controller(
            self.cfg, self.root / "state", policy, driver=self.driver,
            hardware=lambda: _gpu(16000), artifact_store=FakeStore(),
            container=self.container, boot_reader=lambda: "boot1", ready=lambda url: True,
        )
        self.addCleanup(controller.shutdown)
        controller.reconcile()
        return controller

    def test_container_start_ensures_image_and_mounts_model(self):
        c = self.container_controller()
        self.assertEqual(c.boot["state"], "complete")
        with patch.object(Controller, "_check_port"):
            profile, runtime, launch = c._prepare(("chat", "cpu"))
        self.assertEqual(self.container.ensured, [IMAGE])
        self.assertEqual(launch.container.image, IMAGE)
        self.assertEqual(launch.container.engine_argv[-2:], ("--model", "/models/model.gguf"))
        self.assertEqual(launch.container.mounts, (("/models/model.gguf", "/models/model.gguf"),))
        self.assertIn("--network=host", launch.argv)
        self.assertEqual(c.status()["models"][0]["profiles"][0]["state"], "stopped")

    def test_profile_metrics_and_upstream_maps_are_validated(self):
        raw = container_catalog()
        profile = raw["models"]["chat"]["profiles"]["cpu"]
        profile.update(metrics_path="/metrics", metrics_port=9464,
                       metrics={"generation_tokens": "ignis_generated_tokens_total",
                                "requests": "ignis_requests_accepted_total"},
                       upstream={"*": "engine-model", "/v1/audio/transcriptions": None})
        checked = _check_config(validate_catalog(raw))
        self.assertEqual(checked["models"]["chat"]["profiles"]["cpu"]["metrics_port"], 9464)
        for change in ({"metrics_port": 0},
                       {"metrics": {}},
                       {"metrics": {"tokens": "ignis_tokens"}},
                       {"metrics": {"requests": "bad metric"}},
                       {"metrics": {"requests": "a_total", "errors": "a_total"}},
                       {"upstream": {}},
                       {"upstream": {"model": "x"}},
                       {"upstream": {"*": "bad id"}}):
            invalid = copy.deepcopy(raw)
            invalid["models"]["chat"]["profiles"]["cpu"].update(change)
            with self.subTest(change=change), self.assertRaises(CatalogError):
                _check_config(validate_catalog(invalid))
        del raw["models"]["chat"]["profiles"]["cpu"]["metrics_path"]
        with self.assertRaisesRegex(CatalogError, "metrics requires metrics_path"):
            _check_config(validate_catalog(raw))

    def test_container_catalog_requires_port_and_defaults_to_health(self):
        raw = container_catalog()
        raw["runtimes"]["llama"] = {
            "adapter": "container", "image": IMAGE, "argv": ["--port", "{port}"]}
        del raw["models"]["chat"]["profiles"]["cpu"]["port"]
        with self.assertRaisesRegex(CatalogError, "port"):
            _check_config(validate_catalog(raw))
        raw["models"]["chat"]["profiles"]["cpu"]["port"] = 38123
        checked = _check_config(validate_catalog(raw))
        self.assertEqual(checked["models"]["chat"]["profiles"]["cpu"]["health"], {"path": "/health"})
        raw["models"]["chat"]["profiles"]["cpu"]["gpu"] = ["0"]
        with self.assertRaisesRegex(CatalogError, "GPU profile"):
            _check_config(validate_catalog(raw))

    def test_container_adapter_is_portable_and_validates_gpu_lists(self):
        raw = {
            "schema_version": 1,
            "runtimes": {"vllm": {"adapter": "container", "image": IMAGE,
                                  "argv": ["--port", "{port}"], "env": {"A": "1"}}},
            "artifacts": {}, "models": {}, "boot": [],
        }
        self.assertEqual(_check_config(validate_catalog(raw), portable=True)["runtimes"]["vllm"],
                         raw["runtimes"]["vllm"])
        typed = copy.deepcopy(raw)
        typed["runtimes"]["vllm"] = {"adapter": "llama.cpp", "image": IMAGE, "argv": []}
        with self.assertRaisesRegex(CatalogError, "portable"):
            _check_config(validate_catalog(typed), portable=True)
        raw["models"] = {
            "chat": {
                "name": "Chat", "io": [{"input": ["text"], "output": "text"}],
                "profiles": {
                    "gpu": {"runtime": "vllm", "device": "gpu", "gpu": [GPU_0, GPU_1],
                            "artifacts": {}, "port": 38123},
                },
            },
        }
        checked = _check_config(validate_catalog(raw))
        self.assertEqual(checked["models"]["chat"]["profiles"]["gpu"]["gpu"], [GPU_0, GPU_1])
        for selector in ([], [GPU_0, GPU_0], "not-a-uuid"):
            invalid = copy.deepcopy(raw)
            invalid["models"]["chat"]["profiles"]["gpu"]["gpu"] = selector
            with self.subTest(selector=selector), self.assertRaisesRegex(CatalogError, "GPU"):
                _check_config(validate_catalog(invalid))

    def test_generic_container_passes_only_selected_gpus_and_cpu_limits(self):
        raw = container_catalog()
        raw["runtimes"]["llama"] = {
            "adapter": "container", "image": IMAGE,
            "argv": ["--host", "{host}", "--port", "{port}"],
        }
        raw["models"]["chat"]["profiles"] = {
            "cpu": {"runtime": "llama", "device": "cpu", "artifacts": {"model": "weights"},
                    "port": 38123},
            "duo": {"runtime": "llama", "device": "gpu", "gpu": [GPU_0, GPU_1],
                    "artifacts": {"model": "weights"}, "port": 38124, "vram_mib": 4000},
        }
        self.cfg.write_text(json.dumps(raw))
        self.container = FakeContainer()
        policy = {"runtimes": {"llama": {"adapter": "container", "image": IMAGE}}}
        hardware = lambda: _gpus(16000, 9000)
        controller = Controller(
            self.cfg, self.root / "state", policy, driver=self.driver,
            hardware=hardware, artifact_store=FakeStore(), container=self.container,
            boot_reader=lambda: "boot1", ready=lambda url: True,
        )
        self.addCleanup(controller.shutdown)
        controller.reconcile()
        with patch.object(Controller, "_check_port"):
            _, _, cpu_launch = controller._prepare(("chat", "cpu"))
            _, _, gpu_launch = controller._prepare(("chat", "duo"))
        self.assertEqual([arg for arg in cpu_launch.argv if arg.startswith("--device=")], [])
        self.assertIn("--cpus=16", cpu_launch.argv)
        self.assertIn("--env=CUDA_VISIBLE_DEVICES=", cpu_launch.argv)
        self.assertEqual(gpu_launch.container.gpus, (GPU_0, GPU_1))
        self.assertEqual([arg for arg in gpu_launch.argv if arg.startswith("--device=")], [
            f"--device=nvidia.com/gpu={GPU_0}", f"--device=nvidia.com/gpu={GPU_1}",
        ])
        self.assertIn(f"--env=CUDA_VISIBLE_DEVICES={GPU_0},{GPU_1}", gpu_launch.argv)
        self.assertFalse(any(arg.startswith("--cpus=") for arg in gpu_launch.argv))
        self.assertEqual(gpu_launch.container.engine_argv[-2:], ("--port", "38124"))

    def test_gpu_selection_resolves_ordinals_and_rejects_ambiguous_sets(self):
        raw = container_catalog()
        controller = Controller(
            self.cfg, self.root / "state", {}, driver=self.driver, hardware=lambda: _gpus(1, 2),
            artifact_store=FakeStore(), container=FakeContainer(),
            boot_reader=lambda: "boot1", ready=lambda url: True,
        )
        self.addCleanup(controller.shutdown)
        self.cfg.write_text(json.dumps(raw))
        hardware = _gpus(1, 2)
        self.assertEqual([gpu["id"] for gpu in controller._gpus(
            {"device": "gpu", "gpu": ["1", "0"]}, hardware)], [GPU_1, GPU_0])
        self.assertEqual([gpu["id"] for gpu in controller._gpus(
            {"device": "gpu", "gpu": [GPU_1]}, hardware)], [GPU_1])
        for profile, message in (
            ({"device": "gpu"}, "Several GPUs"),
            ({"device": "gpu", "gpu": ["9"]}, "No GPU matches"),
            ({"device": "gpu", "gpu": [GPU_0, GPU_0]}, "more than once"),
        ):
            with self.subTest(profile=profile), self.assertRaisesRegex(RuntimeError, message):
                controller._gpus(profile, hardware)
        self.assertEqual(controller._gpus({"device": "cpu"}, hardware), [])

    def test_container_runtime_requires_an_approved_pinned_image(self):
        for image, approved, message in (
            (IMAGE, None, "not approved locally"),
            (IMAGE, "ghcr.io/example/llama.cpp:server@sha256:" + "b" * 64, "does not match"),
            (IMAGE, "ghcr.io/example/llama.cpp:server", "digest-pinned"),
        ):
            with self.subTest(approved=approved):
                c = self.container_controller(image=image, approved=approved)
                profile, runtime = c._profile(("chat", "cpu"))
                with self.assertRaisesRegex(RuntimeError, message):
                    c._trusted_runtime(profile, runtime)
