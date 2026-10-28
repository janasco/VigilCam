from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.config import BASE_DIR, DATA_DIR, RECORDING_DIR


def _read_proc_loadavg() -> tuple[Optional[float], Optional[float], Optional[float]]:
    # Prefer os.getloadavg (stdlib, Unix).
    try:
        la = os.getloadavg()
        return float(la[0]), float(la[1]), float(la[2])
    except Exception:  # noqa: BLE001
        pass
    try:
        with open("/proc/loadavg", "r") as f:
            parts = f.read().split()
        return float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:  # noqa: BLE001
        return None, None, None



def _cpu_percent_sample(interval: float = 0.1) -> Optional[float]:
    try:
        def read():
            with open("/proc/stat", "r") as f:
                line = f.readline()
            parts = line.split()
            # user, nice, system, idle, iowait, irq, softirq, steal
            vals = [int(x) for x in parts[1:9]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals)
            return idle, total
        idle1, total1 = read()
        time.sleep(interval)
        idle2, total2 = read()
        d_idle = idle2 - idle1
        d_total = total2 - total1
        if d_total <= 0:
            return None
        return round((1.0 - d_idle / d_total) * 100.0, 1)
    except Exception:  # noqa: BLE001
        return None

def _read_meminfo() -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {
        "total": None,
        "available": None,
        "free": None,
        "buffers": None,
        "cached": None,
    }
    try:
        info: Dict[str, int] = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, rest = line.split(":", 1)
                val_parts = rest.strip().split()
                if not val_parts:
                    continue
                try:
                    num = int(val_parts[0])
                except ValueError:
                    continue
                unit = val_parts[1].lower() if len(val_parts) > 1 else "kb"
                # /proc/meminfo values are in kB by default.
                multiplier = 1024
                if unit == "mb":
                    multiplier = 1024 * 1024
                elif unit == "gb":
                    multiplier = 1024 * 1024 * 1024
                elif unit == "b":
                    multiplier = 1
                info[key.strip()] = num * multiplier
        out["total"] = info.get("MemTotal")
        out["available"] = info.get("MemAvailable")
        if out["available"] is None:
            free = info.get("MemFree", 0)
            buffers = info.get("Buffers", 0)
            cached = info.get("Cached", 0)
            out["available"] = free + buffers + cached
        out["free"] = info.get("MemFree")
        out["buffers"] = info.get("Buffers")
        out["cached"] = info.get("Cached")
    except Exception:  # noqa: BLE001
        pass
    return out


def _disk_usage(path: Optional[Path] = None) -> Dict[str, Any]:
    candidates = []
    if path is not None:
        candidates.append(path)
    candidates.extend([RECORDING_DIR, DATA_DIR, Path("/")])
    last_error = None
    for cand in candidates:
        try:
            p = Path(cand)
            try:
                stat = shutil.disk_usage(str(p) if p.exists() else str(Path("/")))
            except Exception:
                stat = shutil.disk_usage("/")
            total = int(getattr(stat, "total", 0) or 0)
            used = int(getattr(stat, "used", 0) or 0)
            free = int(getattr(stat, "free", 0) or 0)
            percent = (used / total * 100.0) if total else 0.0
            return {
                "path": str(p),
                "total": total,
                "total_bytes": total,
                "used": used,
                "used_bytes": used,
                "free": free,
                "free_bytes": free,
                "available": free,
                "available_bytes": free,
                "percent": round(percent, 1),
                "usage_percent": round(percent, 1),
                "percent_used": round(percent, 1),
            }
        except Exception as e:  # noqa: BLE001
            last_error = e
            continue
    return {
        "path": None,
        "total": None,
        "total_bytes": None,
        "used": None,
        "used_bytes": None,
        "free": None,
        "free_bytes": None,
        "available": None,
        "available_bytes": None,
        "percent": None,
        "usage_percent": None,
        "percent_used": None,
        "error": str(last_error) if last_error else None,
    }


def _count_proc_processes() -> Optional[int]:
    try:
        count = 0
        for name in os.listdir("/proc"):
            if name.isdigit():
                count += 1
        return count
    except Exception:  # noqa: BLE001
        return None


def _uptime_seconds() -> Optional[float]:
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.read().split()[0])
    except Exception:  # noqa: BLE001
        return None


def _recording_stats() -> Dict[str, Any]:
    active = 0
    running_processes = 0
    active_names = []
    process_names = []
    try:
        from app.services.recording_manager import recording_manager

        active_names = list(getattr(recording_manager, "active", {}).keys())
        active = len(active_names)
        ingestor = getattr(recording_manager, "ingestor", None)
        processes = getattr(ingestor, "processes", {}) if ingestor is not None else {}
        process_names = list(processes.keys())
        running_processes = len(process_names)
    except Exception:  # noqa: BLE001
        pass
    return {
        "active": active,
        "active_count": active,
        "active_cameras": active,
        "active_camera_count": active,
        "running_cameras": active_names,
        "running": running_processes,
        "running_count": running_processes,
        "processes": running_processes,
        "process_count": running_processes,
        "camera_processes": running_processes,
        "process_names": process_names,
        "active_names": active_names,
    }


async def collect_system_metrics(db: Any = None) -> Dict[str, Any]:
    load1, load5, load15 = _read_proc_loadavg()
    cpu_percent = _cpu_percent_sample(0.05)
    mem = _read_meminfo()
    mem_total = mem.get("total")
    mem_available = mem.get("available")
    mem_used = None
    mem_percent = None
    if mem_total is not None and mem_available is not None:
        mem_used = mem_total - mem_available
        if mem_total:
            mem_percent = round(mem_used / mem_total * 100.0, 1)

    disk = _disk_usage()
    rec = _recording_stats()
    proc_count = _count_proc_processes()
    uptime = _uptime_seconds()
    cpu_count = os.cpu_count()

    total_cameras = None
    enabled_cameras = None
    try:
        if db is not None:
            from sqlalchemy import func, select

            from app.models.entities import Camera

            result = await db.execute(select(func.count(Camera.id)))
            total_cameras = int(result.scalar() or 0)
            result2 = await db.execute(select(func.count(Camera.id)).where(Camera.enabled.is_(True)))
            enabled_cameras = int(result2.scalar() or 0)
    except Exception:  # noqa: BLE001
        pass

    cameras_obj = {
        "total": total_cameras,
        "total_count": total_cameras,
        "count": total_cameras,
        "enabled": enabled_cameras,
        "enabled_count": enabled_cameras,
        "active": rec["active"],
        "active_count": rec["active_count"],
        "active_cameras": rec["active_cameras"],
        "running": rec["running"],
    }

    processes_obj = {
        "active": rec["processes"],
        "active_count": rec["process_count"],
        "count": proc_count if proc_count is not None else rec["process_count"],
        "running": rec["running"],
        "running_count": rec["running_count"],
        "camera_processes": rec["camera_processes"],
        "system": proc_count,
        "system_count": proc_count,
        "system_process_count": proc_count,
    }

    cpu_obj = {
        "load_1m": load1,
        "load_1min": load1,
        "load_1": load1,
        "load_5m": load5,
        "load_5min": load5,
        "load_5": load5,
        "load_15m": load15,
        "load_15min": load15,
        "load_15": load15,
        "loadavg": [load1, load5, load15],
        "load_average": [load1, load5, load15],
        "count": cpu_count,
        "cpu_count": cpu_count,
        "cores": cpu_count,
        "percent": cpu_percent,
        "usage_percent": cpu_percent,
        "cpu_percent": cpu_percent,
    }

    memory_obj = {
        "total": mem_total,
        "total_bytes": mem_total,
        "used": mem_used,
        "used_bytes": mem_used,
        "available": mem_available,
        "available_bytes": mem_available,
        "free": mem.get("free"),
        "free_bytes": mem.get("free"),
        "percent": mem_percent,
        "usage_percent": mem_percent,
        "percent_used": mem_percent,
        "buffers": mem.get("buffers"),
        "cached": mem.get("cached"),
    }

    now = datetime.utcnow().isoformat()

    result: Dict[str, Any] = {
        "timestamp": now,
        "time": now,
        "sampled_at": now,
        "scope": "host",
        "cpu": cpu_obj,
        "cpu_load": load1,
        "cpu_load_1m": load1,
        "cpu_percent": cpu_percent,
        "loadavg": [load1, load5, load15],
        "memory": memory_obj,
        "mem": memory_obj,
        "memory_usage": mem_percent,
        "memory_percent": mem_percent,
        "memory_used": mem_used,
        "disk": disk,
        "disk_usage": disk.get("percent"),
        "disk_percent": disk.get("percent"),
        "cameras": cameras_obj,
        "camera": cameras_obj,
        "camera_count": total_cameras,
        "active_cameras": rec["active"],
        "active_camera_count": rec["active_count"],
        "processes": processes_obj,
        "process": processes_obj,
        "process_count": rec["process_count"],
        "running_processes": rec["running_count"],
        "system_process_count": proc_count,
        "uptime_seconds": uptime,
        "uptime": uptime,
        "cpu_count": cpu_count,
    }
    return result


# Compatibility aliases for alternate naming.
get_system_metrics = collect_system_metrics
get_metrics = collect_system_metrics
system_metrics = collect_system_metrics
metrics = collect_system_metrics
read_system_metrics = collect_system_metrics
gather_system_metrics = collect_system_metrics
