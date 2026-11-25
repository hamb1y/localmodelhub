import unittest
from unittest.mock import patch

from lmh.engines import Container, Launch
from lmh.supervisor import Supervisor


IMAGE = "ghcr.io/example/llama.cpp:server@sha256:" + "a" * 64


class SupervisorTests(unittest.TestCase):
    def container_launch(self):
        engine = ("--host", "127.0.0.1", "--port", "8123")
        argv = ("/usr/bin/podman", "run", "--rm", "--pull=never", IMAGE, *engine)
        return Launch(argv, {"PATH": "/usr/bin", "HOME": "/home/ai",
                             "XDG_RUNTIME_DIR": "/run/user/1000"},
                      None, "http://127.0.0.1:8123/health", 120.0,
                      container=Container(IMAGE, "lmh-chat-abc", engine, ()))

    def native_launch(self):
        return Launch(("/opt/engine", "--port", "8123"), {"PATH": "/usr/bin", "HOME": "/home/ai"},
                      None, "http://127.0.0.1:8123/health", 120.0)

    def start(self, launch, options=None):
        supervisor = Supervisor()
        with patch.object(supervisor, "observe", return_value={"state": "stopped"}), \
                patch.object(supervisor, "_checked") as checked:
            supervisor.start("chat", "cpu", {"adapter": "llama.cpp"}, launch, options or {})
        return checked.call_args.args[0]

    def test_container_runs_in_a_delegated_transient_unit(self):
        argv = self.start(self.container_launch())
        self.assertEqual(argv[:2], ["systemd-run", "--user"])
        self.assertIn("--property=Delegate=yes", argv)
        self.assertIn("--property=NoNewPrivileges=no", argv)
        self.assertNotIn("--property=NoNewPrivileges=yes", argv)
        self.assertEqual(argv[argv.index("--") + 1:argv.index("--") + 3], ["/usr/bin/env", "-i"])
        self.assertIn("/usr/bin/podman", argv)
        self.assertIn("--pull=never", argv)
        self.assertEqual(argv[-1], "8123")
        self.assertEqual([item for item in argv if item.startswith("PATH=")], ["PATH=/usr/bin"])
        self.assertEqual([item for item in argv if item.startswith("HOME=")], ["HOME=/home/ai"])
        self.assertIn("XDG_RUNTIME_DIR=/run/user/1000", argv)

    def test_native_launch_keeps_no_new_privileges(self):
        argv = self.start(self.native_launch())
        self.assertIn("--property=NoNewPrivileges=yes", argv)
        self.assertNotIn("--property=Delegate=yes", argv)
        self.assertNotIn("--property=NoNewPrivileges=no", argv)
        self.assertIn("/opt/engine", argv)

    def test_memory_limits_apply_to_the_container_unit(self):
        argv = self.start(self.container_launch(), {
            "resources": {"memory_high_mib": 256, "memory_max_mib": 512, "swap_max_mib": 0,
                          "cpu_count": 3},
            "lifecycle": {"stop_timeout_seconds": 45},
        })
        self.assertIn("--property=MemoryHigh=256M", argv)
        self.assertIn("--property=MemoryMax=512M", argv)
        self.assertIn("--property=MemorySwapMax=0M", argv)
        self.assertIn("--property=CPUQuota=300%", argv)
        self.assertIn("--property=TimeoutStopSec=45", argv)

    def test_missing_podman_is_reported_before_starting_a_unit(self):
        supervisor = Supervisor()
        launch = Launch(("podman", "run", "image"), {"PATH": "/nonexistent"},
                        None, None, 120.0, container=Container(IMAGE, "lmh-chat-abc", (), ()))
        with patch.object(supervisor, "observe", return_value={"state": "stopped"}), \
                patch("lmh.supervisor.shutil.which", return_value=None), \
                patch.object(supervisor, "_checked") as checked:
            with self.assertRaisesRegex(Exception, "Podman is required"):
                supervisor.start("chat", "cpu", {"adapter": "llama.cpp"}, launch, {})
        checked.assert_not_called()
