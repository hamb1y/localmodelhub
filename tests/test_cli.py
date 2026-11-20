import asyncio
import io
import json
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lmh.api import make_server
from lmh.cli import Backend, _hf_setup, main


class Controller:
    def __init__(self):
        self.actions = []
        self.staged = []

    def status(self):
        return {
            "models": [
                {"id": "small", "name": "Small", "default_profile": "cpu", "profiles": [
                    {"id": "cpu", "device": "cpu", "state": "stopped"},
                    {"id": "gpu", "device": "gpu", "state": "running", "port": 8081},
                    {"id": "blocked", "device": "gpu", "state": "running", "controllable": False},
                ]},
            ],
            "hardware": {"cpu": "Test CPU", "ram_total_bytes": 2**30, "gpus": []},
            "boot": {"state": "ready", "message": "backend online"},
            "jobs": self.actions,
        }

    def logs(self, model, profile):
        return [f"[INFO] logs for {model}/{profile}"]

    def stage_catalog(self, catalog):
        self.staged.append(catalog)
        return {"state": "staged", "message": "Catalog staged for the next OS boot."}

    def stats(self):
        return {
            "samples": [{"ram_used_bytes": 2**20, "vram_used_bytes": None}],
            "jobs_by_model": [{"model": "small", "finished": 1, "failed": 0, "start_seconds": None}],
            "metrics": {"tokens": 15, "prompt_cache_hit_rate": 0.25, "requests": None},
            "profiles": [{"model": "small", "profile": "cpu", "cached_prompt_tokens": 3}],
        }

    def submit(self, action, model, profile, **kwargs):
        job = {"id": len(self.actions) + 1, "action": action, "model": model, "profile": profile,
               "state": "queued", **kwargs}
        self.actions.append(job)
        return job


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.controller = Controller()
        cls.server = make_server(cls.controller, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.05})
        cls.thread.start()
        cls.endpoint = f"http://localhost:{cls.server.server_port}"
        cls.addClassCleanup(cls.thread.join)
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def setUp(self):
        self.controller.actions.clear()
        self.controller.staged.clear()

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--endpoint", self.endpoint, *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_list_start_jobs_stop_and_logs_use_shared_api(self):
        code, output, _ = self.invoke("list")
        self.assertEqual(code, 0)
        self.assertIn("small/cpu (default)", output)
        self.assertIn("small/gpu", output)
        self.assertEqual(self.invoke("start", "small")[0], 0)
        self.assertEqual(self.invoke("stop", "small", "gpu")[0], 0)
        self.assertEqual([(item["action"], item["profile"]) for item in self.controller.actions],
                         [("start", "cpu"), ("stop", "gpu")])
        self.assertIn("queued", self.invoke("jobs")[1])
        self.assertIn("[INFO] logs for small/cpu", self.invoke("logs", "small")[1])

    def test_no_one_shot_switch(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["--endpoint", self.endpoint, "switch", "small", "cpu"])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.controller.actions, [])

    def test_doctor_and_catalog_preview(self):
        code, output, _ = self.invoke("doctor")
        self.assertEqual(code, 0)
        for expected in ("Native account:", "Boot: ready", "Test CPU", "RAM:", "GPU:", "Disk ("):
            self.assertIn(expected, output)
        with TemporaryDirectory() as directory:
            catalog = Path(directory) / "catalog.json"
            data = {"schema_version": 1, "runtimes": {}, "artifacts": {}, "models": {}, "boot": []}
            catalog.write_text(json.dumps(data))
            code, output, _ = self.invoke("preview", "--catalog", str(catalog))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["catalog"], data)

    def test_catalog_stage_only_sends_the_reviewed_file(self):
        with TemporaryDirectory() as directory:
            catalog = Path(directory) / "catalog.json"
            data = {"schema_version": 1, "runtimes": {}, "artifacts": {}, "models": {}, "boot": []}
            catalog.write_text(json.dumps(data))
            code, output, _ = self.invoke("stage", "--catalog", str(catalog))
        self.assertEqual(code, 0)
        self.assertIn("next OS boot", output)
        self.assertEqual(self.controller.staged, [data])
        self.assertEqual(self.controller.actions, [])

    def test_stats_preserves_real_zero_and_omits_unknown_inference_values(self):
        code, output, _ = self.invoke("stats")
        self.assertEqual(code, 0)
        for expected in ("Test CPU", "RAM: 1.0 MiB", "VRAM: unknown", "failed: 0",
                         "tokens: 15", "prompt cache hit rate: 25.0%", "cached prompt tokens: 3"):
            self.assertIn(expected, output)
        self.assertNotIn("requests:", output)
        code, output, _ = self.invoke("stats", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), self.controller.stats())

    def test_cpu_runtime_install_is_opt_in_and_uses_xdg_paths(self):
        installer = Mock(return_value=Path("/tmp/runtime/bin/llama-server"))
        with (
            patch.dict("sys.modules", {"lmh.runtime_install": SimpleNamespace(install_cpu_runtime=installer)}),
            patch.dict("os.environ", {"XDG_DATA_HOME": "/tmp/lmh-data", "XDG_CONFIG_HOME": "/tmp/lmh-config"}),
            patch("lmh.cli._user", return_value="ai"),
            patch("lmh.cli._hf_setup", return_value=True),
        ):
            self.assertEqual(self.invoke("onboard", "--no-hf-login")[0], 0)
            installer.assert_not_called()
            code, output, _ = self.invoke("onboard", "--no-hf-login", "--install-cpu-runtime")
        self.assertEqual(code, 0)
        installer.assert_called_once_with(
            Path("/tmp/lmh-data/localmodelhub"),
            Path("/tmp/lmh-config/localmodelhub/policy.json"),
        )
        self.assertIn("systemctl --user restart localmodelhub.service", output)
        self.assertIn("does not repeat reconciliation", output)

    @unittest.skipUnless(find_spec("textual"), "Textual is not installed")
    def test_switch_requires_explicit_source_and_confirmation(self):
        from textual.widgets import Button, DataTable, Select, Static, TabbedContent

        from lmh.tui import LocalModelHubApp, LogsScreen, SwitchScreen

        async def scenario():
            app = LocalModelHubApp(Backend(self.endpoint))
            async with app.run_test(size=(80, 24)) as pilot:
                await app.workers.wait_for_complete()
                table = app.query_one("#profiles", DataTable)
                self.assertEqual(table.row_count, 3)
                await pilot.click("#switch")
                self.assertIsInstance(app.screen, SwitchScreen)
                self.assertTrue(app.screen.query_one("#confirm-switch", Button).disabled)
                self.assertEqual(self.controller.actions, [])
                await pilot.click("#cancel-switch")
                self.assertEqual(self.controller.actions, [])

                await pilot.click("#switch")
                screen = app.screen
                self.assertEqual([profile.id for profile in screen.sources.values()], ["gpu"])
                screen.query_one("#switch-source", Select).value = next(iter(screen.sources))
                await pilot.pause()
                self.assertEqual(self.controller.actions, [])
                await pilot.click("#confirm-switch")
                await app.workers.wait_for_complete()
                self.assertEqual(len(self.controller.actions), 1)
                job = self.controller.actions[0]
                self.assertEqual((job["action"], job["profile"], job["from_profile"]), ("switch", "cpu", "gpu"))
                await pilot.click("#logs")
                await app.workers.wait_for_complete()
                self.assertIsInstance(app.screen, LogsScreen)
                self.assertIn("small/cpu", app.screen.lines[0])
                await pilot.press("escape")

                table.move_cursor(row=2)
                await pilot.pause()
                for action in ("start", "stop", "switch"):
                    self.assertTrue(app.query_one(f"#{action}", Button).disabled)
                self.assertFalse(app.query_one("#logs", Button).disabled)

                app.query_one(TabbedContent).active = "stats-tab"
                await pilot.pause()
                await app.workers.wait_for_complete()
                self.assertIn("prompt cache hit rate: 25.0%", str(app.query_one("#stats-body", Static).content))

        asyncio.run(scenario())


class LoginTest(unittest.TestCase):
    def test_existing_hf_credentials_are_not_read_again_or_displayed(self):
        hub = SimpleNamespace(get_token=Mock(return_value="never-display-me"), login=Mock())
        with patch.dict("sys.modules", {"huggingface_hub": hub}), redirect_stdout(io.StringIO()) as out:
            self.assertTrue(_hf_setup(True))
        hub.login.assert_not_called()
        self.assertNotIn("never-display-me", out.getvalue())

    def test_explicit_login_uses_hidden_input_and_avoids_git_store(self):
        hub = SimpleNamespace(get_token=Mock(return_value=None), login=Mock())
        with (
            patch.dict("sys.modules", {"huggingface_hub": hub}),
            patch("lmh.cli._user", return_value="ai"),
            patch("sys.stdin.isatty", return_value=True),
            redirect_stdout(io.StringIO()) as out,
            patch.object(out, "isatty", return_value=True),
            patch("lmh.cli.getpass.getpass", return_value="very-secret") as read,
        ):
            self.assertTrue(_hf_setup(True))
        read.assert_called_once()
        hub.login.assert_called_once_with(token="very-secret", add_to_git_credential=False)
        self.assertNotIn("very-secret", out.getvalue())
