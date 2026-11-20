import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lmh import onboarding
from lmh.catalog import load_catalog
from lmh.controller import _catalog_digest, _check_config


STARTER = Path(__file__).resolve().parents[1] / "catalogs/starter.json"
BOOTSTRAP = STARTER.parents[1] / "install.sh"


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = home / ".local/share/localmodelhub"
        self.state = self.data / "state"
        self.state.mkdir(parents=True)
        bundle = self.data / "current/catalogs"
        bundle.mkdir(parents=True)
        shutil.copyfile(STARTER, bundle / "starter.json")
        self.config = home / ".config/localmodelhub"
        self.config.mkdir(parents=True)
        self.catalog = self.config / "models.json"
        shutil.copyfile(STARTER, self.catalog)
        self.active = _check_config(load_catalog(self.catalog))
        self.snapshot = self.state / "current-models.json"
        self.snapshot.write_text(json.dumps(self.active))
        self.policy = self.config / "policy.json"
        self.events = []
        self.enterContext(patch.object(onboarding, "_ai_home", return_value=home))
        self.enterContext(patch.dict(os.environ, {
            "LMH_API_TOKEN": "", "LMH_ENDPOINT": "", "LMH_INFERENCE_ENDPOINT": "", "LMH_INFERENCE_KEY": "",
        }))
        self.backend = self.enterContext(patch.object(onboarding, "Backend")).return_value
        self.install = self.enterContext(patch.object(onboarding, "install_cpu_runtime", side_effect=self.approve))
        self.systemctl = self.enterContext(patch.object(onboarding.subprocess, "run"))
        self.smoke = self.enterContext(patch.object(
            onboarding, "_smoke", side_effect=lambda endpoint, token: endpoint + "/v1"))
        self.enterContext(patch.object(onboarding, "_free", return_value=10**10))
        self.enterContext(patch.object(onboarding, "_cached", return_value=False))
        self.enterContext(patch.object(onboarding.time, "sleep"))

    def approve(self, *_):
        policy = json.loads(self.policy.read_text()) if self.policy.exists() else {}
        policy.setdefault("runtimes", {})["llama-cpu"] = {"custom": "approval"}
        self.policy.write_text(json.dumps(policy))

    def status(self, state="stopped", *, approved=False, jobs=(), starter=True):
        return {
            "boot": {"state": "complete", "catalog_blake3": _catalog_digest(self.active)},
            "models": [
                {"id": "unrelated", "profiles": [{"id": "cpu", "state": "ready"}]},
                *([{"id": "qwen2.5-chat", "profiles": [
                    {"id": "cpu", "state": state, "controllable": approved, "host": "127.0.0.1",
                     "port": self.active["models"]["qwen2.5-chat"]["profiles"]["cpu"]["port"]},
                ]}] if starter else []),
            ],
            "hardware": {"cpu_logical_count": 4, "ram_available_bytes": 8 * 1024 ** 3},
            "jobs": list(jobs),
        }

    def emit(self, stage, state, message, **addresses):
        self.events.append(dict(stage=stage, state=state, message=message, **addresses))

    def test_fresh_explicit_additive_start_then_ready_rerun(self):
        self.policy.write_text(json.dumps({"runtimes": {"another-runtime": {}}, "gpu_headroom_mib": 900}))
        initial = self.catalog.read_bytes()
        self.backend.status.side_effect = [
            onboarding.BackendError("connection refused"),
            self.status(),
            self.status(approved=True),
            self.status("starting", approved=True, jobs=[{"id": "job", "state": "running", "message": "Downloading"}]),
            self.status("ready", approved=True, jobs=[{"id": "job", "state": "done", "message": "Ready"}]),
            self.status("ready", approved=True),
        ]
        self.backend.action.return_value = {"id": "job"}
        self.assertEqual(onboarding.onboard(self.emit), 0)
        self.assertEqual(onboarding.onboard(self.emit), 0)
        self.install.assert_called_once_with(self.data, self.policy)
        self.assertEqual(self.systemctl.call_count, 1)
        self.assertEqual(self.systemctl.call_args.args, (["systemctl", "--user", "restart", "localmodelhub.service"],))
        self.backend.action.assert_called_once_with("start", "qwen2.5-chat", "cpu")
        self.assertEqual(self.smoke.call_count, 2)
        self.smoke.assert_called_with("http://127.0.0.1:5101", None)
        self.assertEqual(self.events[-1]["dashboard"], "http://127.0.0.1:5100")
        self.assertEqual(self.events[-1]["inference"], "http://127.0.0.1:5101/v1")
        self.assertEqual(self.catalog.read_bytes(), initial)
        self.assertEqual(json.loads(self.policy.read_text())["runtimes"]["another-runtime"], {})
        self.assertEqual(json.loads(self.policy.read_text())["gpu_headroom_mib"], 900)

    def test_custom_without_starter_succeeds_with_panel_only(self):
        self.backend.status.return_value = self.status(starter=False)
        custom = b'{"schema_version":1,"models":{}}'
        self.catalog.write_bytes(custom)
        self.assertEqual(onboarding.onboard(self.emit), 0)
        self.assertEqual(self.catalog.read_bytes(), custom)
        self.assertEqual(self.events[-1]["stage"], "done")
        self.assertNotIn("inference", self.events[-1])
        self.backend.action.assert_not_called()
        self.install.assert_not_called()
        self.systemctl.assert_not_called()
        self.smoke.assert_not_called()

    def test_custom_with_same_ids_but_different_recipe_is_not_started(self):
        self.active["models"]["qwen2.5-chat"]["profiles"]["cpu"]["port"] = 8082
        self.snapshot.write_text(json.dumps(self.active))
        self.catalog.write_text(json.dumps(self.active))
        self.backend.status.return_value = self.status()
        self.assertEqual(onboarding.onboard(self.emit), 0)
        self.assertNotIn("inference", self.events[-1])
        self.backend.action.assert_not_called()
        self.install.assert_not_called()
        self.systemctl.assert_not_called()

    def test_existing_approval_for_stopped_model_skips_download_and_restart(self):
        self.approve()
        self.backend.action.return_value = {"id": "job"}
        self.backend.status.side_effect = [
            self.status(approved=True),
            self.status("ready", approved=True, jobs=[{"id": "job", "state": "done"}]),
        ]
        self.assertEqual(onboarding.onboard(self.emit), 0)
        self.backend.action.assert_called_once_with("start", "qwen2.5-chat", "cpu")
        self.install.assert_not_called()
        self.systemctl.assert_not_called()

    def test_failed_additive_job_does_not_switch_or_run_smoke(self):
        self.approve()
        self.backend.action.return_value = {"id": "job"}
        self.backend.status.side_effect = [
            self.status(approved=True),
            self.status(approved=True, jobs=[{"id": "job", "state": "failed", "message": "Cannot bind port"}]),
        ]
        self.assertEqual(onboarding.onboard(self.emit), 1)
        self.backend.action.assert_called_once_with("start", "qwen2.5-chat", "cpu")
        self.assertEqual((self.events[-1]["stage"], self.events[-1]["state"]), ("start", "error"))
        self.assertIn("Cannot bind port", self.events[-1]["message"])
        self.assertIn("no models were stopped", self.events[-1]["message"])
        self.smoke.assert_not_called()

    def test_jsonl_unauthorized_backend_reports_actionable_error_once(self):
        self.backend.status.side_effect = onboarding.BackendError("HTTP 401", 401)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(onboarding.main(["--jsonl"]), 1)
        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual((events[-1]["stage"], events[-1]["state"]), ("backend", "error"))
        self.assertIn("LMH_API_TOKEN", events[-1]["message"])
        self.backend.status.assert_called_once()

    def test_disk_failure_does_not_mutate_policy_or_start_models(self):
        self.backend.status.return_value = self.status()
        with patch.object(onboarding, "_free", return_value=0):
            self.assertEqual(onboarding.onboard(self.emit), 1)
        self.assertEqual((self.events[-1]["stage"], self.events[-1]["state"]), ("host", "error"))
        self.assertIn("filesystem", self.events[-1]["message"])
        self.install.assert_not_called()
        self.systemctl.assert_not_called()
        self.backend.action.assert_not_called()

    def test_isolated_panel_uses_catalog_port_and_never_restarts_default_service(self):
        self.active["models"]["qwen2.5-chat"]["profiles"]["cpu"]["port"] = 18081
        self.snapshot.write_text(json.dumps(self.active))
        self.catalog.write_text(json.dumps(self.active))
        self.policy.write_text(json.dumps({"api_token": "configured-token-not-for-another-service"}))
        self.backend.status.return_value = self.status()
        with patch.dict(os.environ, {"LMH_ENDPOINT": "http://localhost:5197/",
                                     "LMH_INFERENCE_ENDPOINT": "http://localhost:5198/",
                                     "LMH_INFERENCE_KEY": "isolated-inference-key"}):
            self.assertEqual(onboarding.onboard(self.emit), 1)
            self.assertIn("will not restart", self.events[-1]["message"])
            onboarding.Backend.assert_called_once_with("http://localhost:5197", timeout=4, token=None)
            self.approve()
            self.backend.action.return_value = {"id": "job"}
            self.backend.status.side_effect = [
                self.status(approved=True),
                self.status("ready", approved=True, jobs=[{"id": "job", "state": "done"}]),
            ]
            self.assertEqual(onboarding.onboard(self.emit), 0)
        self.assertEqual(self.events[-1]["dashboard"], "http://localhost:5197")
        self.smoke.assert_called_once_with("http://localhost:5198", "isolated-inference-key")
        self.systemctl.assert_not_called()
        self.install.assert_not_called()

    def test_isolated_panel_rejects_nonloopback_before_sending_token(self):
        with patch.dict(os.environ, {"LMH_ENDPOINT": "http://198.51.100.2:5197", "LMH_API_TOKEN": "secret"}):
            self.assertEqual(onboarding.onboard(self.emit), 1)
        onboarding.Backend.assert_not_called()
        self.assertIn("loopback", self.events[-1]["message"])


class OnboardingHTTPTests(unittest.TestCase):
    def test_registered_account_required_even_for_isolated_home(self):
        ai = SimpleNamespace(pw_name="ai", pw_dir="/home/ai")
        with (patch.object(onboarding.os, "getuid", return_value=1100),
              patch.object(onboarding.os, "geteuid", return_value=1100),
              patch.object(onboarding.pwd, "getpwuid", return_value=ai),
              patch.object(onboarding.Path, "home", return_value=Path("/isolated/ai"))):
            self.assertEqual(onboarding._ai_home(isolated=True), Path("/isolated/ai"))
            with self.assertRaisesRegex(ValueError, "registered home"):
                onboarding._ai_home()
            ai.pw_name = "other"
            with self.assertRaisesRegex(ValueError, "dedicated Linux user ai"):
                onboarding._ai_home(isolated=True)

    def test_additive_body_and_control_auth(self):
        response = Mock(status=202)
        response.read.return_value = b'{"id":"job"}'
        with patch.object(onboarding, "HTTPConnection") as connection:
            conn = connection.return_value
            conn.getresponse.return_value = contextlib.nullcontext(response)
            result = onboarding.Backend(onboarding.DASHBOARD, 4, "local-token").action("start", "name", "cpu")
        self.assertEqual(result, {"id": "job"})
        connection.assert_called_once_with("127.0.0.1", 5100, timeout=4)
        self.assertEqual(conn.request.call_args.args, ("POST", "/api/actions"))
        self.assertEqual(json.loads(conn.request.call_args.kwargs["body"]),
                         {"action": "start", "model": "name", "profile": "cpu"})
        self.assertEqual(conn.request.call_args.kwargs["headers"]["Authorization"], "Bearer local-token")

    def test_smoke_uses_unified_inference_and_catalog_model_id(self):
        with patch.object(onboarding, "Backend") as backend:
            client = backend.return_value
            client.request.side_effect = [
                {"data": [{"id": onboarding.MODEL}]},
                {"choices": [{"message": {"content": "Hello"}}]},
            ]
            self.assertEqual(onboarding._smoke(onboarding.INFERENCE, "local-token"), onboarding.INFERENCE + "/v1")
        backend.assert_called_once_with(onboarding.INFERENCE, timeout=60, token="local-token")
        self.assertEqual(client.request.call_args_list[0].args, ("/v1/models",))
        path, request = client.request.call_args.args
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(request["model"], onboarding.MODEL)
        self.assertFalse(request["stream"])


@unittest.skipIf(os.geteuid() == 0, "public bootstrap rejects a root caller")
class BootstrapHandoffTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = self.home / "test-bin"
        self.bin.mkdir()
        self.sandbox = self.home / "sandbox"
        self.sandbox.mkdir()
        self.onboard_log = self.home / "onboard"
        self.exec_script("getent", f"echo 'ai:x:{os.geteuid()}:1000::'{shlex.quote(str(self.home))}':/bin/bash'")
        self.exec_script("systemctl", '[[ $1 != --version ]] || echo "systemd 254"')
        self.exec_script("loginctl", "echo yes")
        self.exec_script("id", "echo ai")
        self.exec_script("curl", "exit 0")
        self.exec_script("sudo", 'echo "unexpected package provisioning" >&2; exit 98')
        version = next(line.split("=", 1)[1] for line in BOOTSTRAP.read_text().splitlines()
                       if line.startswith("updater_sha256="))
        self.exec_script("sha256sum", f"echo '{version}  updater'")
        native = self.home / ".local/share/localmodelhub/current/bin"
        native.mkdir(parents=True)
        self.exec_script(native / "lmh", f"echo \"$1\" > {shlex.quote(str(self.onboard_log))}\n"
                         f"cat >> {shlex.quote(str(self.onboard_log))}")
        self.script = self.sandbox / "install.sh"
        original = BOOTSTRAP.read_text()
        path_line = "export PATH=/usr/sbin:/usr/bin:/sbin:/bin"
        self.assertIn(path_line, original)
        self.script.write_text(original.replace(path_line, f"export PATH={shlex.quote(str(self.bin))}:/usr/bin:/bin"))

    def exec_script(self, name, text):
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + text + "\n")
        path.chmod(0o700)

    def stub_python(self, updater_exit, *, requires_packages=False):
        prerequisite = f"[[ -f {shlex.quote(str(self.home / 'packages-installed'))} ]]" if requires_packages else ":"
        self.exec_script("python3", f"""
case "$2" in
    -c) {prerequisite} ;;
    -m) if [[ $3 == venv ]]; then mkdir -p "$4/bin"; cp -- "$0" "$4/bin/python"; fi ;;
    *) exit {updater_exit} ;;
esac
""")

    def run_bootstrap(self):
        return subprocess.run(["/bin/bash", self.script], input="original stdin\n", capture_output=True,
                              text=True, timeout=10)

    def test_success_passes_original_stdin_to_native_onboarding(self):
        self.stub_python(0)
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.onboard_log.read_text(), "onboard\noriginal stdin\n")

    def test_failed_verified_updater_does_not_onboard(self):
        self.stub_python(87)
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 87, result.stderr)
        self.assertFalse(self.onboard_log.exists())

    def test_missing_python_venv_is_installed_before_verified_release(self):
        self.stub_python(0, requires_packages=True)
        self.exec_script("apt-get", "exit 99")
        sudo_log = self.home / "sudo"
        self.exec_script("sudo", f"""
printf '%s\\n' "$*" >> {shlex.quote(str(sudo_log))}
[[ $1 == -- && $2 == apt-get ]]
case "$3" in
    update) ;;
    install) touch {shlex.quote(str(self.home / 'packages-installed'))} ;;
    *) exit 99 ;;
esac
""")
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sudo_log.read_text().splitlines(),
                         ["-- apt-get update", "-- apt-get install -y python3 python3-venv"])
        self.assertTrue(self.onboard_log.exists())
