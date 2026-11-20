from __future__ import annotations

import json
import fcntl
import tempfile
import unittest
from pathlib import Path

from lmh.controller import Controller, MIB


def _gpu(free=16000):
    return {"cpu": "test", "ram_total_bytes": 32 * 1024 * MIB, "ram_available_bytes": 16 * 1024 * MIB,
            "gpus": [{"id": "GPU-test", "name": "test GPU", "total_bytes": 16000 * MIB, "free_bytes": free * MIB}]}


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
