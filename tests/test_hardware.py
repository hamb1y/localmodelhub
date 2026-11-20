import subprocess
import unittest
from unittest.mock import patch

from lmh import hardware


GPU_0 = "GPU-a1234567-1234-1234-1234-0123456789ab"
GPU_1 = "GPU-b1234567-1234-1234-1234-0123456789ab"
MIB = 1024 * 1024


class HardwareTests(unittest.TestCase):
    def test_snapshot_reports_host_memory_and_stable_gpu_ids(self):
        gpu_output = (
            f'4, {GPU_0.upper()}, "NVIDIA Test, Revision 2", 12000, 8000\n'
            "broken row\n"
            f"6, {GPU_1}, NVIDIA Second, 10000, 2000\n"
        )
        with (
            patch.object(hardware.Path, "read_text", side_effect=[
                "MemTotal:  100000 kB\nMemAvailable:  60000 kB\n",
                "processor\t: 0\nmodel name\t: Example CPU\n",
            ]),
            patch.object(hardware.os, "cpu_count", return_value=8),
            patch.object(hardware.subprocess, "run", return_value=subprocess.CompletedProcess(
                args=(), returncode=0, stdout=gpu_output,
            )) as run,
        ):
            result = hardware.snapshot()
        self.assertEqual(result, {
            "cpu": "Example CPU",
            "cpu_logical_count": 8,
            "ram_total_bytes": 100000 * 1024,
            "ram_available_bytes": 60000 * 1024,
            "gpus": [
                {"id": GPU_0, "index": 4, "name": "NVIDIA Test, Revision 2",
                 "total_bytes": 12000 * MIB, "free_bytes": 8000 * MIB},
                {"id": GPU_1, "index": 6, "name": "NVIDIA Second",
                 "total_bytes": 10000 * MIB, "free_bytes": 2000 * MIB},
            ],
        })
        args, kwargs = run.call_args
        self.assertEqual(args[0], (
            "nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ))
        self.assertFalse(kwargs["shell"])
        self.assertGreater(kwargs["timeout"], 0)
        self.assertTrue(kwargs["check"])

    def test_optional_nvidia_probe_failure_produces_no_gpus(self):
        for failure in (
            FileNotFoundError(), PermissionError(), subprocess.TimeoutExpired("nvidia-smi", 2),
            subprocess.CalledProcessError(1, "nvidia-smi"),
        ):
            with self.subTest(failure=failure), patch.object(
                hardware.subprocess, "run", side_effect=failure,
            ):
                self.assertEqual(hardware.snapshot()["gpus"], [])

    def test_fallback_when_proc_unavailable(self):
        sysconf = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 100, "SC_AVPHYS_PAGES": 50}
        with (
            patch.object(hardware.Path, "read_text", side_effect=FileNotFoundError),
            patch.object(hardware.os, "sysconf", side_effect=sysconf.__getitem__),
            patch.object(hardware.platform, "processor", return_value=""),
            patch.object(hardware.platform, "machine", return_value="test-arch"),
            patch.object(hardware.subprocess, "run", side_effect=FileNotFoundError),
        ):
            result = hardware.snapshot()
        self.assertEqual(result["cpu"], "test-arch")
        self.assertEqual(result["ram_total_bytes"], 409600)
        self.assertEqual(result["ram_available_bytes"], 204800)

    def test_malformed_or_unusable_gpus_are_ignored(self):
        rows = [
            f"0, {GPU_0}, NVIDIA, [N/A], [N/A]",
            f"0, {GPU_0}, NVIDIA, 100, 101",
            "1, GPU-short, NVIDIA, 4000, 2000",
            f"2, {GPU_1}, NVIDIA, 2000, 0",
            f"2, {GPU_1}, NVIDIA, 2000, 0",
        ]
        with patch.object(
            hardware.subprocess, "run",
            return_value=subprocess.CompletedProcess((), 0, stdout="\n".join(rows)),
        ):
            self.assertEqual(hardware.snapshot()["gpus"], [
                {"id": GPU_1, "index": 2, "name": "NVIDIA",
                 "total_bytes": 2000 * MIB, "free_bytes": 0},
            ])
