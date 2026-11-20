"""Best-effort host capacity detection; no scheduling or admission policy."""

from __future__ import annotations

import csv
import os
import platform
import re
import subprocess
from pathlib import Path


_PROC_CPUINFO = Path("/proc/cpuinfo")
_PROC_MEMINFO = Path("/proc/meminfo")
_MIB = 1024 * 1024
_KIB = 1024
_GPU_UUID = re.compile(
    r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_NVIDIA_QUERY = (
    "nvidia-smi",
    "--query-gpu=index,uuid,name,memory.total,memory.free",
    "--format=csv,noheader,nounits",
)


def _proc_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _cpu_name() -> str:
    for line in _proc_text(_PROC_CPUINFO).splitlines():
        key, _, name = line.partition(":")
        if key.strip() in {"model name", "Hardware", "Processor"} and name.strip():
            return name.strip()
    return platform.processor() or platform.machine() or "unknown"


def _sysconf(name: str) -> int | None:
    try:
        value = os.sysconf(name)
        return value if value > 0 else None
    except (AttributeError, OSError, ValueError):
        return None


def _memory() -> tuple[int | None, int | None]:
    info = {}
    for line in _proc_text(_PROC_MEMINFO).splitlines():
        match = re.fullmatch(r"(MemTotal|MemAvailable):\s*([0-9]+)\s+kB\s*", line)
        if match:
            info[match[1]] = int(match[2]) * _KIB
    total = info.get("MemTotal")
    available = info.get("MemAvailable")
    if total is None or available is None:
        pagesize = _sysconf("SC_PAGE_SIZE")
        if pagesize is not None:
            if total is None and (pages := _sysconf("SC_PHYS_PAGES")) is not None:
                total = pages * pagesize
            if available is None and (pages := _sysconf("SC_AVPHYS_PAGES")) is not None:
                available = pages * pagesize
    if available is not None and total is not None:
        available = min(total, available)
    return total, available


def _nvidia_gpus() -> list[dict]:
    try:
        result = subprocess.run(
            _NVIDIA_QUERY, check=True, capture_output=True, timeout=2,
            text=True, encoding="utf-8", errors="replace", shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    seen = set()
    for line in result.stdout.splitlines():
        try:
            index, uuid, *columns = next(csv.reader([line], skipinitialspace=True, strict=True))
            *name_parts, total, free = columns
            index, uuid, total, free = (field.strip() for field in (index, uuid, total, free))
            name = ",".join(name_parts).strip()
            if not (
                _GPU_UUID.fullmatch(uuid) and name
                and all(re.fullmatch(r"[0-9]+", value) for value in (index, total, free))
            ):
                continue
            total_mib, free_mib = int(total), int(free)
            if total_mib <= 0 or free_mib > total_mib:
                continue
            gpu_id = "GPU-" + uuid[4:].lower()
            if gpu_id not in seen:
                seen.add(gpu_id)
                gpus.append({
                    "id": gpu_id,
                    "index": int(index),
                    "name": name,
                    "total_bytes": total_mib * _MIB,
                    "free_bytes": free_mib * _MIB,
                })
        except (csv.Error, ValueError):
            # A broken row should not hide the other GPUs.
            continue
    return gpus


def snapshot() -> dict:
    """Report host RAM and NVIDIA GPUs; unavailable RAM values are None.

    GPU IDs are full CUDA UUIDs; index is informational (nvidia-smi index,
    not necessarily the CUDA ordinal). An absent or failed nvidia-smi query
    yields no selectable GPUs. These values are point-in-time host estimates,
    not reservation guarantees or cgroup limits.
    """
    total, available = _memory()
    return {
        "cpu": _cpu_name(),
        "cpu_logical_count": os.cpu_count(),
        "ram_total_bytes": total,
        "ram_available_bytes": available,
        "gpus": _nvidia_gpus(),
    }


def detected_accelerators(cuda_available=False, pci=Path("/sys/bus/pci/devices"),
                          accel=Path("/sys/class/accel")) -> list[str]:
    """List detected GPUs/accelerators without claiming runtime compatibility."""
    vendors = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel",
               "0x13b5": "Arm", "0x17cb": "Qualcomm"}
    result = []
    for device in sorted(pci.glob("*")):
        kind = _proc_text(device / "class").strip()
        if kind[:4] not in {"0x03", "0x12"}:
            continue
        vendor = _proc_text(device / "vendor").strip()
        if vendor == "0x10de" and cuda_available:
            continue  # NVIDIA cards with measured VRAM are already listed by CUDA UUID.
        model = _proc_text(device / "device").strip()
        category = "GPU" if kind.startswith("0x03") else "accelerator"
        result.append(f"{vendors.get(vendor, vendor or 'Unknown')} {category} {model} ({device.name})")
    for device in sorted(accel.glob("accel*")):
        driver = (device / "device/driver").resolve().name
        result.append(f"Linux accelerator {device.name} ({driver})")
    return result
