import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "uninstall.sh"


@unittest.skipIf(os.geteuid() == 0, "uninstall deliberately refuses root")
class UninstallTest(unittest.TestCase):
    def test_normal_and_purge_only_remove_app_owned_paths(self):
        for purge in (False, True):
            with self.subTest(purge=purge), tempfile.TemporaryDirectory() as root:
                home = Path(root)
                bin_dir = home / "bin"
                bin_dir.mkdir()
                unit_dir = home / ".config/systemd/user"
                unit_dir.mkdir(parents=True)
                data_dir = home / ".local/share/localmodelhub"
                (data_dir / "releases").mkdir(parents=True)
                (data_dir / "state").mkdir()
                catalog = home / ".config/localmodelhub/models.json"
                catalog.parent.mkdir()
                catalog.write_text("{}")
                units = ("localmodelhub.service", "localmodelhub-update.service", "localmodelhub-update.timer")
                for name in (*units, "adopted.service"):
                    (unit_dir / name).touch()
                (unit_dir / "localmodelhub.service.d").mkdir()
                (data_dir / "current").symlink_to("releases")
                logs = home / "systemctl.log"
                (bin_dir / "id").write_text("#!/bin/sh\nprintf 'ai\\n'\n")
                (bin_dir / "systemctl").write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_LOG"
if [ "$2" = list-units ]; then
    printf 'lmh-example-0123456789ab.service loaded active\\nlmh-unrelated.service loaded active\\n'
fi
""")
                for p in bin_dir.iterdir():
                    p.chmod(0o700)
                subprocess.run(
                    ["bash", str(SCRIPT), *(["--purge"] if purge else [])],
                    env={**os.environ, "HOME": root, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                         "TEST_LOG": str(logs)}, check=True, capture_output=True,
                )
                actual = logs.read_text()
                self.assertIn("disable --now localmodelhub-update.timer", actual)
                self.assertIn("stop lmh-example-0123456789ab.service", actual)
                self.assertNotIn("stop lmh-unrelated.service", actual)
                self.assertTrue((unit_dir / "adopted.service").exists())
                self.assertFalse(any((unit_dir / name).exists() for name in units))
                self.assertFalse((data_dir / "releases").exists())
                self.assertEqual(catalog.exists(), not purge)
                self.assertEqual((data_dir / "state").exists(), not purge)
