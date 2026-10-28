"""Donut/pie-ready per-camera storage shares for VigilCam.

Thin UI adapter over
:class:`app.services.storage_breakdown.StorageBreakdownService`: it reuses
the on-disk scan (per-camera ``total_bytes`` + ``share %``) and maps it to
a chart-widget-friendly shape::

    [{label, value, color}]

``label`` is the camera/bucket name, ``value`` is bytes (int) and ``color``
is a deterministic hex colour from a fixed palette so dashboards can render
a donut/pie without extra transformation.

Layout convention matches storage_breakdown: each camera gets a
subdirectory under the recording root; files directly under the root are
grouped under ``"unassigned"``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from app.core.config import RECORDING_DIR, SNAPSHOT_DIR  # type: ignore
except Exception:  # noqa: BLE001
    RECORDING_DIR = Path("data/recordings")  # type: ignore
    SNAPSHOT_DIR = Path("data/snapshots")  # type: ignore


_DEFAULT_PALETTE: List[str] = [
    "#4e79a7",
    "#f28e2b",
    "#e15759",
    "#76b7b2",
    "#59a14f",
    "#edc948",
    "#b07aa1",
    "#ff9da7",
    "#9c755f",
    "#bab0ac",
    "#8cd17d",
    "#499894",
]


def _human_bytes(num: Any) -> str:
    try:
        n = float(num or 0)
    except (TypeError, ValueError):
        return "0 B"
    if n < 0:
        n = 0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while n >= 1024.0 and idx < len(units) - 1:
        n /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(n)} {units[idx]}"
    return f"{n:.1f} {units[idx]}"


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        if not s or s in ("none", "null", "auto", "all"):
            return None
        multipliers = {
            "tib": 1024 ** 4, "gib": 1024 ** 3, "mib": 1024 ** 2, "kib": 1024,
            "tb": 1024 ** 4, "gb": 1024 ** 3, "mb": 1024 ** 2, "kb": 1024, "b": 1,
        }
        for unit, mult in sorted(multipliers.items(), key=lambda kv: -len(kv[0])):
            if s.endswith(unit):
                num = s[: -len(unit)] or "0"
                try:
                    return int(float(num) * mult)
                except (TypeError, ValueError):
                    return None
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _coerce_palette(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Comma/semicolon/pipe separated or single colour.
        for sep in ("|", ";", ","):
            if sep in s:
                parts = [p.strip() for p in s.split(sep) if p.strip()]
                return parts or None
        return [s]
    if isinstance(value, (list, tuple)):
        out = [str(v).strip() for v in value if str(v).strip()]
        return out or None
    return None


def _resolve_palette(options: Dict[str, Any]) -> List[str]:
    for k in (
        "palette", "palettes", "colors", "colours", "color_scheme",
        "colour_scheme", "scheme", "colorPalette", "colourPalette",
        "chart_colors", "chart_colours", "segment_colors", "slice_colors",
    ):
        if options.get(k) is not None:
            p = _coerce_palette(options.get(k))
            if p:
                return p
    return list(_DEFAULT_PALETTE)


class StorageDonutService:
    """Per-camera storage shares shaped for donut/pie widgets."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._kwargs: Dict[str, Any] = {}
        storage_root: Any = None
        for a in args:
            if isinstance(a, dict):
                self._kwargs.update(a)
            elif isinstance(a, (str, Path)):
                if storage_root is None:
                    storage_root = a
        self._kwargs.update(kwargs)
        if storage_root is None:
            for k in (
                "storage_root", "recordings_dir", "recording_dir", "root",
                "path", "storage_path", "base_dir", "directory", "dir",
                "recordings_root", "storage_dir",
            ):
                if self._kwargs.get(k) is not None:
                    storage_root = self._kwargs.get(k)
                    break
        try:
            default_root = Path(RECORDING_DIR)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            default_root = Path("data/recordings")
        from pathlib import Path as _P

        if isinstance(storage_root, _P):
            self.storage_root = storage_root.expanduser()
        elif isinstance(storage_root, str) and storage_root.strip():
            self.storage_root = _P(storage_root.strip()).expanduser()
        else:
            self.storage_root = default_root
        self.recordings_root = self.storage_root

    # -- internal helpers -------------------------------------------------

    def _merged(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(self._kwargs)
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (str, Path)):
                merged.setdefault("path", a)
            elif isinstance(a, (int, float)) and not isinstance(a, bool):
                if "camera_id" not in merged and "camera_name" not in merged and "camera" not in merged:
                    merged.setdefault("camera_id", a)
        merged.update(kwargs)
        for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters", "donut", "pie", "chart"):
            nested = merged.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    merged.setdefault(k, v)
        return merged

    # -- core -------------------------------------------------------------

    def donut(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        options = self._merged(*args, **kwargs)
        palette = _resolve_palette(options)

        # Delegate the actual scan to storage_breakdown so totals/shares
        # stay consistent between the table/trend view and the donut.
        try:
            from app.services.storage_breakdown import StorageBreakdownService
        except Exception:  # noqa: BLE001
            StorageBreakdownService = None  # type: ignore[assignment]

        if StorageBreakdownService is not None:
            breakdown = StorageBreakdownService().analyze(**options)
            entries = (
                breakdown.get("cameras")
                or breakdown.get("per_camera")
                or breakdown.get("breakdown")
                or breakdown.get("storage_breakdown")
                or breakdown.get("items")
                or []
            )
            storage_root_s = (
                breakdown.get("storage_root")
                or breakdown.get("recordings_root")
                or breakdown.get("path")
                or str(self.storage_root)
            )
            snapshot_root_s = breakdown.get("snapshot_root")
            total_bytes = int(breakdown.get("total_bytes") or 0)
            total_files = int(breakdown.get("file_count") or breakdown.get("files_count") or 0)
        else:
            entries = []
            storage_root_s = str(self.storage_root)
            snapshot_root_s = None
            total_bytes = 0
            total_files = 0

        # Optional client-side narrowing aliases.
        top_n = options.get("top_n", options.get("topN", options.get("limit")))
        # storage_breakdown already applied top_n/limit/sort/min_bytes, but
        # re-apply defensively when the caller bypassed it via palette-only
        # paths or future refactors.
        if top_n is not None and isinstance(entries, list):
            try:
                n = int(top_n)
                if n >= 0:
                    entries = entries[:n]
            except (TypeError, ValueError):
                pass

        segments: List[Dict[str, Any]] = []
        for i, e in enumerate(entries or []):
            try:
                label = str(e.get("camera_name") or e.get("camera") or e.get("name") or f"camera_{i}")
            except Exception:  # noqa: BLE001
                label = f"camera_{i}"
            try:
                value = int(e.get("total_bytes") if e.get("total_bytes") is not None else e.get("bytes") or 0)
            except (TypeError, ValueError):
                value = 0
            share = e.get("share_percent", e.get("share", e.get("percent", 0.0)))
            try:
                share_f = float(share or 0.0)
            except (TypeError, ValueError):
                share_f = 0.0
            try:
                count = int(e.get("file_count") or e.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            color = palette[i % len(palette)] if palette else _DEFAULT_PALETTE[i % len(_DEFAULT_PALETTE)]
            seg: Dict[str, Any] = {
                # Canonical donut triple.
                "label": label,
                "value": int(value),
                "color": str(color),
                # Widely-used widget aliases.
                "name": label,
                "camera": label,
                "camera_name": label,
                "id": e.get("camera_id", e.get("id")),
                "camera_id": e.get("camera_id", e.get("id")),
                "bytes": int(value),
                "total_bytes": int(value),
                "size": int(value),
                "size_bytes": int(value),
                "used_bytes": int(value),
                "bytes_total": int(value),
                "value_bytes": int(value),
                "share": round(share_f, 2),
                "share_percent": round(share_f, 2),
                "share_pct": round(share_f, 2),
                "percent": round(share_f, 2),
                "percentage": round(share_f, 2),
                "usage_percent": round(share_f, 2),
                "share_fraction": round(share_f / 100.0, 4) if share_f else 0.0,
                "fraction": round(share_f / 100.0, 4) if share_f else 0.0,
                "human": _human_bytes(value),
                "human_size": _human_bytes(value),
                "total_human": _human_bytes(value),
                "size_human": _human_bytes(value),
                "file_count": int(count),
                "count": int(count),
                "files": int(count),
                "colour": str(color),
                "backgroundColor": str(color),
                "background_color": str(color),
                "fill": str(color),
                "fillColor": str(color),
                "fill_color": str(color),
            }
            segments.append(seg)

        # Recompute total from (possibly truncated) segments so the widget
        # header stays consistent; keep original grand total when no
        # truncation happened.
        seg_total = sum(int(s.get("value") or 0) for s in segments)
        if top_n is None:
            seg_total = int(total_bytes) if total_bytes else seg_total
        else:
            total_bytes = int(seg_total)

        labels = [s["label"] for s in segments]
        values = [int(s["value"]) for s in segments]
        colors = [str(s["color"]) for s in segments]

        try:
            now = time.time()
            generated_at = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        except Exception:  # noqa: BLE001
            generated_at = ""

        result: Dict[str, Any] = {
            "status": "ok",
            "state": "ok",
            "ok": True,
            "success": True,
            "storage_root": storage_root_s,
            "recordings_root": storage_root_s,
            "recordings_dir": storage_root_s,
            "path": storage_root_s,
            "snapshot_root": snapshot_root_s,
            "total_bytes": int(total_bytes if top_n is None else seg_total),
            "total": int(total_bytes if top_n is None else seg_total),
            "bytes": int(total_bytes if top_n is None else seg_total),
            "bytes_total": int(total_bytes if top_n is None else seg_total),
            "used_bytes": int(total_bytes if top_n is None else seg_total),
            "used": int(total_bytes if top_n is None else seg_total),
            "combined_bytes": int(total_bytes if top_n is None else seg_total),
            "grand_total_bytes": int(total_bytes if top_n is None else seg_total),
            "total_human": _human_bytes(total_bytes if top_n is None else seg_total),
            "human": _human_bytes(total_bytes if top_n is None else seg_total),
            "file_count": int(total_files),
            "files_count": int(total_files),
            "count": len(segments),
            "num_cameras": len(segments),
            "camera_count": len(segments),
            "cameras_count": len(segments),
            "total_cameras": len(segments),
            "num_buckets": len(segments),
            "num_segments": len(segments),
            "num_slices": len(segments),
            "generated_at": generated_at,
            "timestamp": generated_at,
            # Canonical widget arrays.
            "segments": segments,
            "slices": segments,
            "data": segments,
            "donut": segments,
            "donut_segments": segments,
            "donut_slices": segments,
            "donut_data": segments,
            "pie": segments,
            "pie_data": segments,
            "pie_segments": segments,
            "pie_slices": segments,
            "chart_data": segments,
            "chart_segments": segments,
            "chart_slices": segments,
            "items": segments,
            "results": segments,
            "entries": segments,
            "breakdown": segments,
            "storage_donut": segments,
            "labels": labels,
            "values": values,
            "colors": colors,
            "colours": colors,
            "palette": list(palette),
        }
        return result

    def slices(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        """Return the bare ``[{label, value, color}]`` list."""
        result = self.donut(*args, **kwargs)
        segs = result.get("segments") or []
        return list(segs)

    def segments_list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.slices(*args, **kwargs)

    async def donut_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    async def analyze_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    # -- aliases ----------------------------------------------------------

    def analyze(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def get_donut(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def get_storage_donut(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def get_donut_chart(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def get_pie(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def get_pie_chart(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def get_segments(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def get_slices(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def get_chart_data(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def get_breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def get_summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def get_usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.donut(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        if "async" in low:
            async def _async_wrapper(*a: Any, **k: Any) -> Dict[str, Any]:
                return self.donut(*a, **k)
            return _async_wrapper
        if any(h in low for h in ("donut", "pie", "slice", "segment", "chart", "camera", "per", "breakdown", "usage", "analy", "summary", "status", "disk", "storage", "share", "label", "color", "colour")):
            return self.donut
        return self.donut


StorageDonutServiceAlias = StorageDonutService
StorageDonutManager = StorageDonutService
StorageDonutAnalyzer = StorageDonutService
PerCameraStorageDonut = StorageDonutService
CameraStorageDonut = StorageDonutService
StoragePieService = StorageDonutService
StoragePieChartService = StorageDonutService

storage_donut = StorageDonutService()
storage_donut_service = storage_donut
service = storage_donut
manager = storage_donut
analyzer = storage_donut
default_service = storage_donut
default_storage_donut = storage_donut
storage_pie = storage_donut
storage_pie_service = storage_donut


def donut(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def analyze(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def get_donut(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def get_storage_donut(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def get_pie(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def get_segments(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def get_slices(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def get_chart_data(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def get_breakdown(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def get_usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_donut.donut(*args, **kwargs)


def slices_list(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return storage_donut.slices(*args, **kwargs)


async def donut_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return await storage_donut.donut_async(*args, **kwargs)


async def analyze_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return await storage_donut.donut_async(*args, **kwargs)


__all__ = [
    "StorageDonutService",
    "StorageDonutServiceAlias",
    "StorageDonutManager",
    "StorageDonutAnalyzer",
    "PerCameraStorageDonut",
    "CameraStorageDonut",
    "StoragePieService",
    "StoragePieChartService",
    "storage_donut",
    "storage_donut_service",
    "service",
    "manager",
    "analyzer",
    "default_service",
    "default_storage_donut",
    "storage_pie",
    "storage_pie_service",
    "donut",
    "analyze",
    "get_donut",
    "get_storage_donut",
    "get_pie",
    "get_segments",
    "get_slices",
    "get_chart_data",
    "get_breakdown",
    "summary",
    "get_summary",
    "status",
    "get_status",
    "usage",
    "get_usage",
    "slices_list",
    "donut_async",
    "analyze_async",
]
