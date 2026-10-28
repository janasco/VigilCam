from __future__ import annotations

"""Motion masking, thresholding, and polygon zone helpers."""

from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

import cv2
import numpy as np


def _coerce_point(point: Any) -> Tuple[float, float]:
    if isinstance(point, dict):
        x = point.get("x", point.get("X", point.get("lon", 0)))
        y = point.get("y", point.get("Y", point.get("lat", 0)))
        return (float(x), float(y))
    if hasattr(point, "x") and hasattr(point, "y"):
        return (float(point.x), float(point.y))
    if hasattr(point, "X") and hasattr(point, "Y"):
        return (float(point.X), float(point.Y))
    return (float(point[0]), float(point[1]))


def _coerce_points(points: Any) -> List[Tuple[float, float]]:
    if points is None:
        return []
    if isinstance(points, str):
        if "," in points and " " not in points.strip():
            vals = [float(v) for v in points.split(",")]
            return [(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]
        out: List[Tuple[float, float]] = []
        for token in points.replace(";", " ").split():
            if "," in token:
                x, y = token.split(",", 1)
                out.append((float(x), float(y)))
        return out
    if isinstance(points, (list, tuple)):
        if points and all(isinstance(p, (int, float, np.integer, np.floating)) for p in points):
            vals = [float(v) for v in points]
            return [(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]
        out: List[Tuple[float, float]] = []
        for p in points:
            if isinstance(p, (list, tuple)) and len(p) == 1 and isinstance(p[0], (list, tuple)):
                p = p[0]
            out.append(_coerce_point(p))
        return out
    return []


def _normalize_mode(mode: Any) -> str:
    value = str(mode or "include").strip().lower()
    if value in {"exclude", "excluded", "mask", "masked", "ignore", "ignored", "block", "disabled", "false"}:
        return "exclude"
    return "include"


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (tuple, list)):
        if len(value) == 1:
            return _as_int(value[0], default)
        # (rows, cols) or (cols, rows) ambiguous; caller handles
        try:
            return int(value[0])
        except (TypeError, ValueError):
            return default
    if isinstance(value, dict):
        for k in ("rows", "row", "height", "h", "grid_rows", "n_rows"):
            if value.get(k) is not None:
                return _as_int(value.get(k), default)
        for k in ("size", "grid_size", "cell", "grid"):
            if value.get(k) is not None:
                return _as_int(value.get(k), default)
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_grid_shape(grid_rows, grid_cols, grid_size, grid, rows, cols, kwargs) -> Tuple[int, int]:
    # Allow kwargs overrides.
    extra_rows = None
    extra_cols = None
    extra_size = None
    extra_grid = None
    for key in list(kwargs.keys()):
        lk = str(key).lower()
        val = kwargs.get(key)
        if val is None:
            continue
        if "row" in lk:
            extra_rows = val
            kwargs.pop(key, None)
        elif "col" in lk:
            extra_cols = val
            kwargs.pop(key, None)
        elif "cell" in lk:
            # cell size, not grid shape
            pass
        elif "grid" in lk or "size" in lk:
            if isinstance(val, dict):
                if extra_rows is None and val.get("rows") is not None:
                    extra_rows = val.get("rows")
                if extra_cols is None and val.get("cols") is not None:
                    extra_cols = val.get("cols")
                if val.get("grid_size") is not None or val.get("size") is not None:
                    extra_size = val.get("grid_size", val.get("size"))
                if val.get("grid") is not None:
                    extra_grid = val.get("grid")
            else:
                # scalar grid size
                if extra_size is None and not isinstance(val, (tuple, list)):
                    extra_size = val
                else:
                    extra_grid = val

    if extra_rows is not None:
        grid_rows = extra_rows
    if extra_cols is not None:
        grid_cols = extra_cols
    if extra_size is not None:
        grid_size = extra_size
    if extra_grid is not None:
        grid = extra_grid

    # Handle grid param as shape.
    if isinstance(grid, dict):
        if grid_rows is None and grid.get("rows") is not None:
            grid_rows = grid.get("rows")
        if grid_cols is None and grid.get("cols") is not None:
            grid_cols = grid.get("cols")
        if grid_size is None and grid.get("grid_size") is not None:
            grid_size = grid.get("grid_size")
        if grid_size is None and grid.get("size") is not None:
            grid_size = grid.get("size")
    elif isinstance(grid, (tuple, list)):
        if len(grid) == 1:
            if grid_size is None:
                grid_size = grid[0]
        elif len(grid) >= 2:
            if grid_rows is None:
                grid_rows = grid[0]
            if grid_cols is None:
                grid_cols = grid[1]
    elif isinstance(grid, int) and not isinstance(grid, bool):
        if grid_size is None and grid_rows is None and grid_cols is None:
            grid_size = grid

    if isinstance(grid_size, dict):
        if grid_rows is None and grid_size.get("rows") is not None:
            grid_rows = grid_size.get("rows")
        if grid_cols is None and grid_size.get("cols") is not None:
            grid_cols = grid_size.get("cols")
        if grid_rows is None and grid_size.get("grid_size") is not None:
            grid_size = grid_size.get("grid_size")
        elif grid_rows is None and grid_size.get("size") is not None:
            grid_size = grid_size.get("size")

    if isinstance(grid_size, (tuple, list)):
        if len(grid_size) == 1:
            grid_size = grid_size[0]
        elif len(grid_size) >= 2:
            if grid_rows is None:
                grid_rows = grid_size[0]
            if grid_cols is None:
                grid_cols = grid_size[1]
            grid_size = None

    if grid_size is not None and grid_rows is None and grid_cols is None:
        n = _as_int(grid_size, 8)
        grid_rows = n
        grid_cols = n

    if rows is not None and grid_rows is None:
        grid_rows = rows
    if cols is not None and grid_cols is None:
        grid_cols = cols

    gr = _as_int(grid_rows, 8)
    gc = _as_int(grid_cols, 8)
    if gr < 1:
        gr = 8
    if gc < 1:
        gc = 8
    return gr, gc


@dataclass
class PolygonZone:
    """A polygonal motion zone.

    ``points`` may be given as a list of (x, y) pairs.
    ``mode`` is ``"include"`` or ``"exclude"``.
    ``threshold_percentage`` is the per-zone motion threshold.
    """

    points: Any = field(default_factory=list)
    mode: str = "include"
    threshold_percentage: float = 5.0
    threshold: Optional[float] = None
    threshold_percent: Optional[float] = None
    threshold_pct: Optional[float] = None
    percent: Optional[float] = None
    name: Optional[str] = None
    label: Optional[str] = None
    id: Optional[Any] = None

    def __post_init__(self) -> None:
        self.points = _coerce_points(self.points)
        self.mode = _normalize_mode(self.mode)
        for candidate in (self.threshold, self.threshold_percent, self.threshold_pct, self.percent):
            if candidate is not None:
                self.threshold_percentage = float(candidate)
                break
        try:
            self.threshold_percentage = float(self.threshold_percentage)
        except (TypeError, ValueError):
            self.threshold_percentage = 5.0
        if self.name is None and self.label is not None:
            self.name = self.label

    @property
    def kind(self) -> str:
        return self.mode

    def to_dict(self) -> dict:
        return {
            "points": [list(p) for p in self.points],
            "mode": self.mode,
            "threshold_percentage": self.threshold_percentage,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "PolygonZone":
        if isinstance(data, cls):
            return data
        if isinstance(data, (list, tuple)):
            if len(data) == 3 and isinstance(data[1], str):
                return cls(points=data[0], mode=data[1], threshold_percentage=data[2])
            if len(data) == 2 and isinstance(data[1], str):
                return cls(points=data[0], mode=data[1])
            return cls(points=data)
        if isinstance(data, dict):
            points = (
                data.get("points")
                or data.get("coordinates")
                or data.get("coords")
                or data.get("polygon")
                or data.get("vertices")
                or data.get("pts")
            )
            mode = (
                data.get("mode")
                or data.get("type")
                or data.get("kind")
                or data.get("zone_type")
            )
            threshold = None
            for k in ("threshold_percentage", "threshold", "threshold_percent", "threshold_pct", "percent"):
                if data.get(k) is not None:
                    threshold = data.get(k)
                    break
            return cls(
                points=points,
                mode=mode or "include",
                threshold_percentage=threshold if threshold is not None else 5.0,
                name=data.get("name") or data.get("label") or data.get("id"),
            )
        return cls(points=data)


# Aliases.
Zone = PolygonZone
ZonePolygon = PolygonZone
PolygonMask = PolygonZone
MotionZone = PolygonZone
PolygonZoneConfig = PolygonZone
MotionPolygonZone = PolygonZone


@dataclass
class MotionZoneResult:
    """Motion evaluation result, including per-zone breakdown."""

    triggered: bool
    motion_percent: float
    pixels_changed: int
    zones: List[Any] = field(default_factory=list)
    zone_results: Optional[List[Any]] = None
    per_zone: Optional[List[Any]] = None
    zone_motion: Optional[List[Any]] = None
    zone: Optional[str] = None
    mode: Optional[str] = None
    name: Optional[str] = None
    label: Optional[str] = None
    index: Optional[int] = None
    threshold_percentage: Optional[float] = None
    points: Optional[List[Tuple[float, float]]] = None
    motion_percent_by_zone: Optional[dict] = None
    triggered_zones: Optional[List[Any]] = None
    excluded_pixels: Optional[int] = None

    # Grid occupancy + blob counting (Wave 25)
    blob_count: Optional[int] = None
    num_blobs: Optional[int] = None
    blobs: Optional[List[Any]] = None
    blob_area: Optional[Any] = None
    blob_area_pixels: Optional[Any] = None
    total_blob_area: Optional[Any] = None
    blob_areas: Optional[List[Any]] = None
    blob_sizes: Optional[List[Any]] = None
    largest_blob_area: Optional[Any] = None
    occupancy_grid: Optional[Any] = None
    grid: Optional[Any] = None
    grid_occupancy: Optional[Any] = None
    occupied_cells: Optional[int] = None
    occupancy: Optional[Any] = None
    grid_rows: Optional[int] = None
    grid_cols: Optional[int] = None
    cell_size: Optional[Any] = None
    grid_cell_size: Optional[Any] = None

    # Additional aliases (Wave 25)
    blobs_count: Optional[int] = None
    blob_counts: Optional[int] = None
    number_of_blobs: Optional[int] = None
    blob_num: Optional[int] = None
    blob_total: Optional[int] = None
    motion_blobs: Optional[int] = None
    blob_area_total: Optional[Any] = None
    total_blob_area_pixels: Optional[Any] = None
    blob_pixels: Optional[Any] = None
    blob_area_px: Optional[Any] = None
    motion_grid: Optional[Any] = None
    grid_map: Optional[Any] = None
    occupancy_map: Optional[Any] = None
    occupied_grid: Optional[Any] = None
    occupied_cell_count: Optional[int] = None
    occupied_cells_count: Optional[int] = None
    num_occupied_cells: Optional[int] = None
    occupied: Optional[int] = None
    grid_shape: Optional[Any] = None
    grid_size: Optional[Any] = None

    def __post_init__(self) -> None:
        if not self.zones and self.zone_results:
            self.zones = self.zone_results
        if not self.zones and self.per_zone:
            self.zones = self.per_zone
        if not self.zones and self.zone_motion:
            self.zones = self.zone_motion
        if self.zone_results is None:
            self.zone_results = self.zones
        if self.per_zone is None:
            self.per_zone = self.zones
        if self.zone_motion is None:
            self.zone_motion = self.zones

        # Blob / grid aliases.
        if self.num_blobs is None:
            self.num_blobs = self.blob_count
        if self.blob_count is None:
            self.blob_count = self.num_blobs
        if self.blob_count is None:
            self.blob_count = 0
        if self.num_blobs is None:
            self.num_blobs = self.blob_count
        if self.blob_area is None:
            self.blob_area = self.blob_area_pixels
        if self.blob_area_pixels is None:
            self.blob_area_pixels = self.blob_area
        if self.total_blob_area is None:
            self.total_blob_area = self.blob_area
        if self.blob_area is None:
            self.blob_area = self.total_blob_area
        if self.blob_area_pixels is None:
            self.blob_area_pixels = self.total_blob_area
        if self.blob_areas is None and self.blobs is not None:
            self.blob_areas = [b.get("area") if isinstance(b, dict) else getattr(b, "area", None) for b in self.blobs]
        if self.blob_sizes is None:
            self.blob_sizes = self.blob_areas
        if self.blob_areas is None:
            self.blob_areas = []
        if self.blob_sizes is None:
            self.blob_sizes = []
        if self.blobs is None:
            self.blobs = []
        if self.largest_blob_area is None and self.blob_areas:
            try:
                self.largest_blob_area = max(self.blob_areas)
            except Exception:
                self.largest_blob_area = None
        if self.grid is None:
            self.grid = self.occupancy_grid
        if self.occupancy_grid is None:
            self.occupancy_grid = self.grid
        if self.grid_occupancy is None:
            self.grid_occupancy = self.occupancy_grid
        if self.grid_cell_size is None:
            self.grid_cell_size = self.cell_size
        if self.cell_size is None:
            self.cell_size = self.grid_cell_size

        # Additional alias sync.
        for attr in ("blobs_count", "blob_counts", "number_of_blobs", "blob_num", "blob_total", "motion_blobs"):
            if getattr(self, attr) is None:
                setattr(self, attr, self.blob_count)
        for attr in ("blob_area_total", "total_blob_area_pixels", "blob_pixels", "blob_area_px"):
            if getattr(self, attr) is None:
                setattr(self, attr, self.total_blob_area)
        for attr in ("motion_grid", "grid_map", "occupancy_map", "occupied_grid"):
            if getattr(self, attr) is None:
                setattr(self, attr, self.occupancy_grid)
        for attr in ("occupied_cell_count", "occupied_cells_count", "num_occupied_cells", "occupied"):
            if getattr(self, attr) is None:
                setattr(self, attr, self.occupied_cells)
        if self.grid_shape is None:
            self.grid_shape = (self.grid_rows, self.grid_cols)
        if self.grid_size is None:
            self.grid_size = (self.grid_rows, self.grid_cols)

    @property
    def zone_evaluations(self) -> List[Any]:
        return self.zones

    @property
    def results_by_zone(self) -> List[Any]:
        return self.zones

    def to_dict(self) -> dict:
        return {
            "triggered": self.triggered,
            "motion_percent": self.motion_percent,
            "pixels_changed": self.pixels_changed,
            "zones": [z.to_dict() if hasattr(z, "to_dict") else z for z in self.zones],
            "zone_results": [z.to_dict() if hasattr(z, "to_dict") else z for z in (self.zone_results or [])],
            "per_zone": [z.to_dict() if hasattr(z, "to_dict") else z for z in (self.per_zone or [])],
            "zone_motion": [z.to_dict() if hasattr(z, "to_dict") else z for z in (self.zone_motion or [])],
            "zone": self.zone,
            "mode": self.mode,
            "name": self.name,
            "label": self.label,
            "index": self.index,
            "threshold_percentage": self.threshold_percentage,
            "points": self.points,
            "motion_percent_by_zone": self.motion_percent_by_zone,
            "triggered_zones": self.triggered_zones,
            "excluded_pixels": self.excluded_pixels,
            "blob_count": self.blob_count,
            "num_blobs": self.num_blobs,
            "blobs": self.blobs,
            "blob_area": self.blob_area,
            "blob_area_pixels": self.blob_area_pixels,
            "total_blob_area": self.total_blob_area,
            "blob_areas": self.blob_areas,
            "blob_sizes": self.blob_sizes,
            "largest_blob_area": self.largest_blob_area,
            "occupancy_grid": self.occupancy_grid,
            "grid": self.grid,
            "grid_occupancy": self.grid_occupancy,
            "occupied_cells": self.occupied_cells,
            "occupancy": self.occupancy,
            "grid_rows": self.grid_rows,
            "grid_cols": self.grid_cols,
            "cell_size": self.cell_size,
            "grid_cell_size": self.grid_cell_size,
            "blobs_count": self.blobs_count,
            "blob_counts": self.blob_counts,
            "number_of_blobs": self.number_of_blobs,
            "blob_num": self.blob_num,
            "blob_total": self.blob_total,
            "motion_blobs": self.motion_blobs,
            "blob_area_total": self.blob_area_total,
            "total_blob_area_pixels": self.total_blob_area_pixels,
            "blob_pixels": self.blob_pixels,
            "blob_area_px": self.blob_area_px,
            "motion_grid": self.motion_grid,
            "grid_map": self.grid_map,
            "occupancy_map": self.occupancy_map,
            "occupied_grid": self.occupied_grid,
            "occupied_cell_count": self.occupied_cell_count,
            "occupied_cells_count": self.occupied_cells_count,
            "num_occupied_cells": self.num_occupied_cells,
            "occupied": self.occupied,
            "grid_shape": self.grid_shape,
            "grid_size": self.grid_size,
        }


class MotionZoningEngine:
    """Motion evaluator with ignore zones and polygon zones."""

    def __init__(
        self,
        frame_width: Optional[int] = None,
        frame_height: Optional[int] = None,
        threshold_percentage: float = 5.0,
        zones: Any = None,
        polygon_zones: Any = None,
        polygons: Any = None,
        polygon: Any = None,
        zone_list: Any = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        w: Optional[int] = None,
        h: Optional[int] = None,
        threshold: Optional[float] = None,
        threshold_percent: Optional[float] = None,
        grid_rows: Any = None,
        grid_cols: Any = None,
        grid_size: Any = None,
        grid: Any = None,
        rows: Any = None,
        cols: Any = None,
        cell_size: Any = None,
        grid_cell_size: Any = None,
        min_blob_area: Any = None,
        blob_min_area: Any = None,
        min_blob_size: Any = None,
        min_area: Any = None,
        occupancy_threshold: Any = None,
        grid_threshold: Any = None,
        occupancy_cell_threshold: Any = None,
        **kwargs: Any,
    ) -> None:
        if width is not None:
            frame_width = width
        if height is not None:
            frame_height = height
        if w is not None:
            frame_width = w
        if h is not None:
            frame_height = h
        if frame_width is None:
            frame_width = kwargs.pop("frame_w", None) or kwargs.pop("img_width", 640)
        if frame_height is None:
            frame_height = kwargs.pop("frame_h", None) or kwargs.pop("img_height", 480)

        if isinstance(threshold_percentage, (list, tuple, dict)):
            if zones is None:
                zones = threshold_percentage
            threshold_percentage = 5.0

        if threshold is not None:
            threshold_percentage = threshold
        if threshold_percent is not None:
            threshold_percentage = threshold_percent
        tp = kwargs.pop("threshold_percentage", None)
        if tp is not None:
            threshold_percentage = tp
        # grid threshold / occupancy threshold aliases in kwargs
        for k in list(kwargs.keys()):
            lk = str(k).lower()
            if "threshold" in lk and ("grid" in lk or "occup" in lk or "cell" in lk):
                occupancy_threshold = kwargs.pop(k)
            elif "min" in lk and ("blob" in lk or "area" in lk or "size" in lk):
                if min_blob_area is None:
                    min_blob_area = kwargs.pop(k)
                else:
                    kwargs.pop(k, None)

        # grid shape parsing (consumes relevant kwargs)
        gr, gc = _parse_grid_shape(
            grid_rows, grid_cols, grid_size, grid, rows, cols, kwargs
        )

        # min blob area
        for cand in (min_blob_area, blob_min_area, min_blob_size, min_area):
            if cand is not None:
                min_blob_area = cand
                break
        if min_blob_area is None:
            min_blob_area = kwargs.pop("min_blob_area", None)
        if min_blob_area is None:
            min_blob_area = kwargs.pop("blob_min_area", None)
        if min_blob_area is None:
            min_blob_area = kwargs.pop("min_blob_size", None)
        if min_blob_area is None:
            min_blob_area = kwargs.pop("min_area", None)
        if min_blob_area is None:
            min_blob_area = 1
        try:
            self.min_blob_area = int(min_blob_area)
        except (TypeError, ValueError):
            self.min_blob_area = 1
        if self.min_blob_area < 1:
            self.min_blob_area = 1

        # occupancy threshold
        for cand in (occupancy_threshold, grid_threshold, occupancy_cell_threshold):
            if cand is not None:
                occupancy_threshold = cand
                break
        if occupancy_threshold is None:
            occupancy_threshold = kwargs.pop("occupancy_threshold", None)
        if occupancy_threshold is None:
            occupancy_threshold = kwargs.pop("grid_threshold", None)
        if occupancy_threshold is None:
            occupancy_threshold = kwargs.pop("occupancy_cell_threshold", None)
        if occupancy_threshold is None:
            occupancy_threshold = kwargs.pop("cell_threshold", None)
        if occupancy_threshold is None:
            occupancy_threshold = 0.0
        try:
            self.occupancy_threshold = float(occupancy_threshold)
        except (TypeError, ValueError):
            self.occupancy_threshold = 0.0

        self.frame_width = int(frame_width)
        self.frame_height = int(frame_height)
        self.threshold_percentage = float(threshold_percentage if threshold_percentage is not None else 5.0)

        collected = zones
        for alt in (polygon_zones, polygons, polygon, zone_list):
            if alt is not None and collected is None:
                collected = alt
        for key, val in list(kwargs.items()):
            if val is not None and any(tok in key.lower() for tok in ("zone", "polygon")):
                if collected is None:
                    collected = val
                kwargs.pop(key, None)

        self.mask = np.ones((self.frame_height, self.frame_width), dtype=np.uint8) * 255
        self.previous_gray: np.ndarray | None = None
        self.zones: List[PolygonZone] = []
        self.grid_rows = int(gr)
        self.grid_cols = int(gc)
        self.cell_size = cell_size if cell_size is not None else grid_cell_size
        if self.cell_size is None:
            try:
                self.cell_size = (self.frame_width // self.grid_cols, self.frame_height // self.grid_rows)
            except Exception:
                self.cell_size = None
        self.set_zones(collected)

    def set_ignored_zone(self, top_left: Tuple[int, int], bottom_right: Tuple[int, int]) -> None:
        cv2.rectangle(self.mask, top_left, bottom_right, 0, -1)

    def set_active_zone(self, top_left: Tuple[int, int], bottom_right: Tuple[int, int]) -> None:
        self.mask[:] = 0
        cv2.rectangle(self.mask, top_left, bottom_right, 255, -1)

    def set_zones(self, zones: Any) -> None:
        self.zones = self._normalize_zones(zones)

    def set_polygon_zones(self, zones: Any) -> None:
        self.set_zones(zones)

    def set_polygons(self, zones: Any) -> None:
        self.set_zones(zones)

    def add_zone(self, zone: Any) -> None:
        if isinstance(zone, (list, tuple)) and zone and all(isinstance(p, (int, float, np.integer, np.floating)) for p in zone):
            zone = [zone]
        if isinstance(zone, (list, tuple, dict)) and not isinstance(zone, PolygonZone):
            self.zones.extend(self._normalize_zones(zone))
        else:
            self.zones.extend(self._normalize_zones([zone]))

    def add_polygon_zone(self, zone: Any) -> None:
        self.add_zone(zone)

    def add_polygon(self, zone: Any) -> None:
        self.add_zone(zone)

    def clear_zones(self) -> None:
        self.zones = []

    def configure_grid(self, rows: Any = None, cols: Any = None, size: Any = None, **kwargs: Any) -> None:
        gr, gc = _parse_grid_shape(rows if rows is not None else self.grid_rows, cols if cols is not None else self.grid_cols, size, None, None, None, kwargs)
        self.grid_rows = int(gr)
        self.grid_cols = int(gc)

    def set_grid(self, *args: Any, **kwargs: Any) -> None:
        if len(args) == 1 and isinstance(args[0], (tuple, list, int)):
            if isinstance(args[0], int):
                self.grid_rows = int(args[0])
                self.grid_cols = int(args[0])
            else:
                if len(args[0]) == 1:
                    self.grid_rows = int(args[0][0])
                    self.grid_cols = int(args[0][0])
                else:
                    self.grid_rows = int(args[0][0])
                    self.grid_cols = int(args[0][1])
        if "rows" in kwargs:
            self.grid_rows = _as_int(kwargs.get("rows"), self.grid_rows)
        if "cols" in kwargs:
            self.grid_cols = _as_int(kwargs.get("cols"), self.grid_cols)
        if "size" in kwargs:
            n = _as_int(kwargs.get("size"), self.grid_rows)
            self.grid_rows = n
            self.grid_cols = n
        if self.grid_rows < 1:
            self.grid_rows = 8
        if self.grid_cols < 1:
            self.grid_cols = 8

    @staticmethod
    def _normalize_zones(zones: Any) -> List[PolygonZone]:
        if zones is None:
            return []
        if isinstance(zones, dict):
            out: List[PolygonZone] = []
            for name, z in zones.items():
                zone = PolygonZone.from_dict(z)
                if zone.name is None:
                    zone.name = str(name)
                out.append(zone)
            return out
        if isinstance(zones, (list, tuple)):
            out: List[PolygonZone] = []
            for z in zones:
                if isinstance(z, PolygonZone):
                    out.append(z)
                elif isinstance(z, (list, tuple)):
                    if len(z) == 3 and isinstance(z[1], str):
                        out.append(PolygonZone(points=z[0], mode=z[1], threshold_percentage=z[2]))
                    elif len(z) == 2 and isinstance(z[1], str):
                        out.append(PolygonZone(points=z[0], mode=z[1]))
                    elif len(z) > 0 and all(isinstance(p, (int, float, np.integer, np.floating)) for p in z):
                        out.append(PolygonZone(points=z))
                    else:
                        out.append(PolygonZone.from_dict(list(z)))
                elif isinstance(z, dict):
                    out.append(PolygonZone.from_dict(z))
                else:
                    try:
                        out.append(PolygonZone.from_dict(z))
                    except Exception:
                        out.append(PolygonZone(points=z))
            return [z for z in out if (z.points and len(z.points) >= 3)]
        try:
            z = PolygonZone.from_dict(zones)
            return [z] if (z.points and len(z.points) >= 3) else []
        except Exception:
            return []

    def _scale_points_if_relative(self, points: List[Tuple[float, float]]) -> List[Tuple[int, int]]:
        pts: List[Tuple[int, int]] = []
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        relative = max(xs + [0.0]) <= 1.0 and max(ys + [0.0]) <= 1.0 and (max(xs + [0.0]) > 0 or max(ys + [0.0]) > 0)
        for x, y in points:
            if relative:
                ix = int(round(float(x) * (self.frame_width - 1)))
                iy = int(round(float(y) * (self.frame_height - 1)))
            else:
                ix = int(round(float(x)))
                iy = int(round(float(y)))
            ix = max(0, min(self.frame_width - 1, ix))
            iy = max(0, min(self.frame_height - 1, iy))
            pts.append((ix, iy))
        return pts

    def _zone_mask(self, zone: PolygonZone) -> np.ndarray:
        mask = np.zeros((self.frame_height, self.frame_width), dtype=np.uint8)
        pts = self._scale_points_if_relative(zone.points)
        if len(pts) < 3:
            return mask
        arr = np.array(pts, dtype=np.int32).reshape((-1, 1, 2))
        cv2.fillPoly(mask, [arr], 255)
        return mask

    def _count_blobs(self, mask: Any) -> Tuple[int, List[dict], int]:
        try:
            arr = np.asarray(mask)
            if arr.ndim == 3:
                arr = arr[:, :, 0]
            binary = (arr > 0).astype(np.uint8) * 255
        except Exception:
            return 0, [], 0
        try:
            num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)
        except Exception:
            return 0, [], 0
        blobs: List[dict] = []
        total_area = 0
        for i in range(1, int(num_labels)):
            try:
                area = int(stats[i, cv2.CC_STAT_AREA])
            except Exception:
                area = 0
            if area < self.min_blob_area:
                continue
            try:
                x = int(stats[i, cv2.CC_STAT_LEFT])
                y = int(stats[i, cv2.CC_STAT_TOP])
                w = int(stats[i, cv2.CC_STAT_WIDTH])
                h = int(stats[i, cv2.CC_STAT_HEIGHT])
            except Exception:
                x = y = w = h = 0
            try:
                cx = float(centroids[i][0])
                cy = float(centroids[i][1])
            except Exception:
                cx = cy = 0.0
            blob = {
                "id": int(i),
                "label": int(i),
                "index": int(i),
                "area": int(area),
                "size": int(area),
                "pixels": int(area),
                "bbox": [x, y, w, h],
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "width": w,
                "height": h,
                "centroid": [cx, cy],
                "center": [cx, cy],
                "cx": cx,
                "cy": cy,
            }
            blobs.append(blob)
            total_area += int(area)
        return len(blobs), blobs, int(total_area)

    def _occupancy_grid(self, mask: Any) -> Tuple[List[List[int]], int]:
        try:
            arr = np.asarray(mask)
            if arr.ndim == 3:
                arr = arr[:, :, 0]
        except Exception:
            arr = np.zeros((self.frame_height, self.frame_width), dtype=np.uint8)
        rows = int(self.grid_rows)
        cols = int(self.grid_cols)
        if rows < 1:
            rows = 8
        if cols < 1:
            cols = 8
        grid: List[List[int]] = [[0] * cols for _ in range(rows)]
        occupied = 0
        for r in range(rows):
            y0 = r * self.frame_height // rows
            y1 = (r + 1) * self.frame_height // rows
            if y1 <= y0:
                y1 = min(self.frame_height, y0 + 1)
            for c in range(cols):
                x0 = c * self.frame_width // cols
                x1 = (c + 1) * self.frame_width // cols
                if x1 <= x0:
                    x1 = min(self.frame_width, x0 + 1)
                cell = arr[y0:y1, x0:x1]
                try:
                    count = int(np.count_nonzero(cell))
                except Exception:
                    count = 0
                total_cells = int(cell.size) if hasattr(cell, "size") else 1
                if total_cells <= 0:
                    total_cells = 1
                if self.occupancy_threshold <= 0:
                    occ = count > 0
                elif self.occupancy_threshold < 1.0:
                    occ = (count / float(total_cells)) >= self.occupancy_threshold
                else:
                    occ = count >= self.occupancy_threshold
                if occ:
                    grid[r][c] = 1
                    occupied += 1
        return grid, int(occupied)

    def process_and_evaluate(self, raw_frame: Any) -> MotionZoneResult:
        gray = cv2.cvtColor(raw_frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if self.previous_gray is None or self.previous_gray.shape != gray.shape:
            self.previous_gray = gray
            # Still compute grid/blobs for current frame vs none? No motion.
            empty_mask = np.zeros((self.frame_height, self.frame_width), dtype=np.uint8)
            blob_count, blobs, blob_area = self._count_blobs(empty_mask)
            occ_grid, occupied = self._occupancy_grid(empty_mask)
            result = MotionZoneResult(
                triggered=False,
                motion_percent=0.0,
                pixels_changed=0,
                zones=[],
                blob_count=blob_count,
                blobs=blobs,
                blob_area=blob_area,
                total_blob_area=blob_area,
                occupancy_grid=occ_grid,
                grid=occ_grid,
                occupied_cells=occupied,
                occupancy=(occupied / max(1, self.grid_rows * self.grid_cols)) * 100.0,
                grid_rows=self.grid_rows,
                grid_cols=self.grid_cols,
                cell_size=self.cell_size,
            )
            return result

        frame_delta = cv2.absdiff(self.previous_gray, gray)
        _, thresh = cv2.threshold(frame_delta, 25, 255, cv2.THRESH_BINARY)
        masked = cv2.bitwise_and(thresh, self.mask)

        include_mask = np.zeros((self.frame_height, self.frame_width), dtype=np.uint8)
        exclude_masks: List[np.ndarray] = []
        zone_masks: List[np.ndarray] = []
        has_zones = bool(self.zones)
        has_include = any(z.mode == "include" for z in self.zones)

        if has_zones:
            if has_include:
                for z in self.zones:
                    zm = self._zone_mask(z)
                    zone_masks.append(zm)
                    if z.mode == "include":
                        include_mask = cv2.bitwise_or(include_mask, zm)
                    else:
                        exclude_masks.append(zm)
            else:
                include_mask[:] = 255
                for z in self.zones:
                    zm = self._zone_mask(z)
                    zone_masks.append(zm)
                    exclude_masks.append(zm)
            for em in exclude_masks:
                include_mask = cv2.bitwise_and(include_mask, cv2.bitwise_not(em))

        effective_monitored = include_mask if has_zones else self.mask
        masked_effective = cv2.bitwise_and(thresh, effective_monitored)

        changed_pixels = int(np.sum(masked_effective == 255))
        total_pixels = int(np.sum(effective_monitored == 255))
        if total_pixels <= 0:
            total_pixels = int(self.frame_width * self.frame_height)
        motion_percent = (changed_pixels / max(1, total_pixels)) * 100.0

        self.previous_gray = gray

        # Overall blobs + grid.
        overall_blob_count, overall_blobs, overall_blob_area = self._count_blobs(masked_effective)
        occ_grid, occupied_cells = self._occupancy_grid(masked_effective)

        zone_results: List[MotionZoneResult] = []
        triggered_zones: List[int] = []
        for idx, z in enumerate(self.zones):
            zm = zone_masks[idx] if idx < len(zone_masks) else self._zone_mask(z)
            z_changed = cv2.bitwise_and(thresh, zm)
            z_changed_pixels = int(np.sum(z_changed == 255))
            z_total = int(np.sum(zm == 255))
            if z_total <= 0:
                z_total = 1
            z_motion_percent = (z_changed_pixels / max(1, z_total)) * 100.0
            try:
                z_triggered = bool(z_motion_percent >= float(z.threshold_percentage))
            except Exception:
                z_triggered = False
            if z.mode == "exclude":
                z_triggered = False
            z_blob_count, z_blobs, z_blob_area = self._count_blobs(z_changed)
            z_occ_grid, z_occupied = self._occupancy_grid(z_changed)
            z_res = MotionZoneResult(
                triggered=z_triggered,
                motion_percent=z_motion_percent,
                pixels_changed=z_changed_pixels,
                zones=[],
                zone=z.name,
                name=z.name or z.label,
                label=z.name or z.label,
                mode=z.mode,
                index=idx,
                threshold_percentage=float(z.threshold_percentage),
                points=[list(p) for p in z.points],
                blob_count=z_blob_count,
                blobs=z_blobs,
                blob_area=z_blob_area,
                total_blob_area=z_blob_area,
                occupancy_grid=z_occ_grid,
                grid=z_occ_grid,
                occupied_cells=z_occupied,
                occupancy=(z_occupied / max(1, self.grid_rows * self.grid_cols)) * 100.0,
                grid_rows=self.grid_rows,
                grid_cols=self.grid_cols,
                cell_size=self.cell_size,
            )
            zone_results.append(z_res)
            if z_triggered:
                triggered_zones.append(idx)

        legacy_changed = int(np.sum(cv2.bitwise_and(thresh, self.mask) == 255))
        motion_total_pixels = int(self.frame_width * self.frame_height)
        legacy_percent = (legacy_changed / max(1, motion_total_pixels)) * 100.0

        if has_zones:
            triggered = bool(triggered_zones)
            if not has_include:
                triggered = legacy_percent >= self.threshold_percentage
        else:
            triggered = motion_percent >= self.threshold_percentage

        if zone_results and any(zr.triggered for zr in zone_results):
            triggered = True

        result = MotionZoneResult(
            triggered=bool(triggered),
            motion_percent=float(motion_percent),
            pixels_changed=int(changed_pixels),
            zones=zone_results,
            zone_results=zone_results,
            per_zone=zone_results,
            zone_motion=zone_results,
            threshold_percentage=self.threshold_percentage,
            triggered_zones=triggered_zones,
            motion_percent_by_zone={str(z.name or i): zr.motion_percent for i, (z, zr) in enumerate(zip(self.zones, zone_results))},
            blob_count=overall_blob_count,
            blobs=overall_blobs,
            blob_area=overall_blob_area,
            total_blob_area=overall_blob_area,
            occupancy_grid=occ_grid,
            grid=occ_grid,
            occupied_cells=occupied_cells,
            occupancy=(occupied_cells / max(1, self.grid_rows * self.grid_cols)) * 100.0,
            grid_rows=self.grid_rows,
            grid_cols=self.grid_cols,
            cell_size=self.cell_size,
            excluded_pixels=int(np.sum(self.mask == 0)) if False else None,
        )
        return result

    def grid_occupancy(self, mask: Any = None, frame: Any = None) -> Any:
        if mask is None and frame is not None:
            mask = frame
        if mask is None:
            grid, _ = self._occupancy_grid(np.zeros((self.frame_height, self.frame_width), dtype=np.uint8))
            return grid
        arr = mask
        if isinstance(arr, np.ndarray) and arr.ndim == 3:
            gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
            arr = (gray > 0).astype(np.uint8) * 255
        grid, _ = self._occupancy_grid(arr)
        return grid

    def grid_occupancy_with_count(self, mask: Any = None, frame: Any = None) -> Any:
        if mask is None and frame is not None:
            mask = frame
        if mask is None:
            return self._occupancy_grid(np.zeros((self.frame_height, self.frame_width), dtype=np.uint8))
        arr = mask
        if isinstance(arr, np.ndarray) and arr.ndim == 3:
            gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
            arr = (gray > 0).astype(np.uint8) * 255
        return self._occupancy_grid(arr)

    def occupancy_grid(self, mask: Any = None, frame: Any = None) -> Any:
        return self.grid_occupancy(mask=mask, frame=frame)

    def count_blobs(self, mask: Any = None, frame: Any = None) -> Any:
        if mask is None and frame is not None:
            mask = frame
        if mask is None:
            mask = np.zeros((self.frame_height, self.frame_width), dtype=np.uint8)
        arr = mask
        if isinstance(arr, np.ndarray) and arr.ndim == 3:
            gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
            arr = (gray > 0).astype(np.uint8) * 255
        cnt, blobs, area = self._count_blobs(arr)
        return {"blob_count": cnt, "count": cnt, "num_blobs": cnt, "blobs": blobs, "blob_area": area, "total_blob_area": area}

    def analyze_grid(self, raw_frame: Any) -> Any:
        return self.process_and_evaluate(raw_frame).occupancy_grid

    def get_occupancy_grid(self, raw_frame: Any = None) -> Any:
        if raw_frame is None:
            return None
        return self.process_and_evaluate(raw_frame).occupancy_grid

    def process(self, raw_frame: Any) -> MotionZoneResult:
        return self.process_and_evaluate(raw_frame)

    def evaluate(self, raw_frame: Any) -> MotionZoneResult:
        return self.process_and_evaluate(raw_frame)

    def analyze(self, raw_frame: Any) -> MotionZoneResult:
        return self.process_and_evaluate(raw_frame)

    def analyze_frame(self, raw_frame: Any) -> MotionZoneResult:
        return self.process_and_evaluate(raw_frame)

    def run(self, raw_frame: Any) -> MotionZoneResult:
        return self.process_and_evaluate(raw_frame)

    def compute(self, raw_frame: Any) -> MotionZoneResult:
        return self.process_and_evaluate(raw_frame)

    def detect(self, raw_frame: Any) -> MotionZoneResult:
        return self.process_and_evaluate(raw_frame)

    def detect_motion(self, raw_frame: Any) -> MotionZoneResult:
        return self.process_and_evaluate(raw_frame)


class RecordingMode:  # pragma: no cover - compatibility
    def __init__(self, name: str) -> None:
        self.name = name


RECORDING_MODES = {
    "always": "Continuous segment copying.",
    "motion": "Motion/Object-triggered recording with RAM pre-roll.",
    "manual": "API/UI controlled recording.",
    "scheduled": "Time profile-controlled recording.",
}


# ---------------------------------------------------------------------------
# Module-level helpers / aliases (Wave 25)
# ---------------------------------------------------------------------------

def _decode_b64_image(value: Any) -> Optional[np.ndarray]:
    if value is None:
        return None
    if isinstance(value, np.ndarray):
        return value
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    else:
        s = str(value).strip()
        if s.startswith("data:"):
            # data:image/jpeg;base64,....
            if "," in s:
                s = s.split(",", 1)[1]
        # remove whitespace
        s = "".join(s.split())
        try:
            import base64

            data = base64.b64decode(s, validate=False)
        except Exception:
            return None
    try:
        buf = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        return img
    except Exception:
        return None


def _extract_frame_and_opts(args: Tuple[Any, ...], kwargs: dict) -> Tuple[Any, dict]:
    kwargs = dict(kwargs)
    frames = [a for a in args if isinstance(a, np.ndarray)]
    frame = None
    prev = None
    if len(frames) >= 2:
        prev = frames[0]
        frame = frames[1]
    elif len(frames) == 1:
        frame = frames[0]

    if frame is None:
        for key in ("frame", "image", "raw_frame", "img", "picture", "current", "current_frame", "image_base64", "image_b64"):
            if key in kwargs and kwargs.get(key) is not None:
                val = kwargs.pop(key)
                if isinstance(val, str):
                    dec = _decode_b64_image(val)
                    if dec is not None:
                        frame = dec
                else:
                    frame = val
                break
    if prev is None:
        for key in ("previous", "previous_frame", "prev", "prev_frame", "previous_image", "previous_image_base64"):
            if key in kwargs and kwargs.get(key) is not None:
                val = kwargs.pop(key)
                if isinstance(val, str):
                    dec = _decode_b64_image(val)
                    if dec is not None:
                        prev = dec
                else:
                    prev = val
                break

    if frame is None and "synthetic" not in kwargs:
        # no frame; caller may want synthetic
        pass

    opts = kwargs
    if prev is not None:
        opts = dict(opts)
        opts["_prev_frame"] = prev
    return frame, opts


def _make_engine(width: int, height: int, opts: dict) -> MotionZoningEngine:
    opts = dict(opts)
    prev = opts.pop("_prev_frame", None)
    # store prev on opts? engine handles via process calls; caller primes.
    opts["_prev_frame"] = prev
    # filter unknown? Engine accepts kwargs broadly.
    return MotionZoningEngine(width, height, **{k: v for k, v in opts.items() if k != "_prev_frame"}), prev


def analyze(*args: Any, **kwargs: Any) -> Any:
    frame, opts = _extract_frame_and_opts(args, kwargs)
    prev = opts.pop("_prev_frame", None)
    if frame is None:
        # synthetic default
        w = int(opts.pop("width", opts.pop("w", 640)))
        h = int(opts.pop("height", opts.pop("h", 480)))
        prev = np.zeros((h, w, 3), dtype=np.uint8)
        frame = prev.copy()
        frame[h // 4:3 * h // 4, w // 4:3 * w // 4] = 255
    else:
        h, w = frame.shape[:2]
        if prev is None:
            prev = np.zeros_like(frame)
    engine = MotionZoningEngine(w, h, **{k: v for k, v in opts.items() if not k.startswith("_")})
    engine.process_and_evaluate(prev)
    return engine.process_and_evaluate(frame)


def analyze_motion(*args: Any, **kwargs: Any) -> Any:
    return analyze(*args, **kwargs)


def analyze_frame(*args: Any, **kwargs: Any) -> Any:
    return analyze(*args, **kwargs)


def analyze_image(*args: Any, **kwargs: Any) -> Any:
    return analyze(*args, **kwargs)


def motion_analyze(*args: Any, **kwargs: Any) -> Any:
    return analyze(*args, **kwargs)


def compute_motion(*args: Any, **kwargs: Any) -> Any:
    return analyze(*args, **kwargs)


def detect_motion(*args: Any, **kwargs: Any) -> Any:
    return analyze(*args, **kwargs)


def run_analysis(*args: Any, **kwargs: Any) -> Any:
    return analyze(*args, **kwargs)


def count_blobs(*args: Any, **kwargs: Any) -> Any:
    # If given a mask/frame, count blobs.
    frame, opts = _extract_frame_and_opts(args, kwargs)
    if isinstance(frame, np.ndarray):
        # if shape 2D treat as mask, else motion vs zeros
        if frame.ndim == 2:
            mask = frame
        else:
            h, w = frame.shape[:2]
            engine = MotionZoningEngine(w, h, **{k: v for k, v in opts.items() if not k.startswith("_")})
            # no previous -> treat whole as mask? just threshold
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            mask = (gray > 0).astype(np.uint8) * 255
        # need engine for min_blob_area
        w = mask.shape[1]
        h = mask.shape[0]
        engine = MotionZoningEngine(w, h, **{k: v for k, v in opts.items() if not k.startswith("_")})
        cnt, blobs, area = engine._count_blobs(mask)
        return {"blob_count": cnt, "count": cnt, "num_blobs": cnt, "blobs": blobs, "blob_area": area, "total_blob_area": area}
    # maybe mask passed as kw
    mask = kwargs.get("mask")
    if mask is not None:
        mask = np.asarray(mask)
        h, w = mask.shape[:2]
        engine = MotionZoningEngine(w, h, **{k: v for k, v in opts.items() if not k.startswith("_")})
        cnt, blobs, area = engine._count_blobs(mask)
        return {"blob_count": cnt, "count": cnt, "num_blobs": cnt, "blobs": blobs, "blob_area": area, "total_blob_area": area}
    return 0


def blob_count(*args: Any, **kwargs: Any) -> Any:
    res = count_blobs(*args, **kwargs)
    if isinstance(res, dict):
        return res.get("blob_count", res.get("count", 0))
    return res


def occupancy_grid(*args: Any, **kwargs: Any) -> Any:
    frame, opts = _extract_frame_and_opts(args, kwargs)
    if isinstance(frame, np.ndarray):
        if frame.ndim == 3:
            h, w = frame.shape[:2]
            engine = MotionZoningEngine(w, h, **{k: v for k, v in opts.items() if not k.startswith("_")})
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            mask = (gray > 0).astype(np.uint8) * 255
            grid, occupied = engine._occupancy_grid(mask)
            return grid
        else:
            h, w = frame.shape[:2]
            engine = MotionZoningEngine(w, h, **{k: v for k, v in opts.items() if not k.startswith("_")})
            grid, occupied = engine._occupancy_grid(frame)
            return grid
    return []


def grid_occupancy(*args: Any, **kwargs: Any) -> Any:
    return occupancy_grid(*args, **kwargs)


# Alias common names to MotionZoningEngine methods.
for _name in ("analyze", "analyze_frame", "analyze_motion", "motion_analyze", "compute", "run", "detect", "detect_motion", "evaluate", "process"):
    try:
        setattr(MotionZoningEngine, _name, MotionZoningEngine.process_and_evaluate)
    except Exception:
        pass

# Expose engine alias names.
MotionGrid = MotionZoningEngine
MotionAnalyzer = MotionZoningEngine
MotionAnalysisEngine = MotionZoningEngine
GridMotionEngine = MotionZoningEngine
BlobCounter = MotionZoningEngine
MotionBlobCounter = MotionZoningEngine
GridOccupancyEngine = MotionZoningEngine
OccupancyGridEngine = MotionZoningEngine

# Module aliases
motion_analyze = analyze
motion_analysis = analyze
analyze_motion_grid = analyze
motion_grid_occupancy = occupancy_grid
compute_occupancy_grid = occupancy_grid
count_motion_blobs = count_blobs
motion_blob_count = blob_count
blob_counter = count_blobs
grid_occupancy_engine = MotionZoningEngine


# Result class aliases.
MotionZoneAnalysisResult = MotionZoneResult
MotionAnalysisResult = MotionZoneResult
MotionAnalysis = MotionZoneResult
GridMotionResult = MotionZoneResult
OccupancyResult = MotionZoneResult
BlobCountResult = MotionZoneResult
MotionGridResult = MotionZoneResult

# Additional function aliases.
compute_grid = occupancy_grid
compute_occupancy = occupancy_grid
get_occupancy_grid = occupancy_grid
motion_occupancy_grid = occupancy_grid
motion_grid = occupancy_grid
blob_counting = count_blobs
count_blob = count_blobs
count_blobs_in_mask = count_blobs
analyze_grid = analyze
analyze_occupancy = analyze
analyze_blobs = analyze
motion_analyze_grid = analyze
run_motion_analysis = analyze
evaluate_motion = analyze
process_motion = analyze
