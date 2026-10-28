"""Virtual tripwire service for VigilCam.

Defines per-camera virtual trip *lines* (line segments, not polygons) and
evaluates crossing events from point tracks (e.g. object-centroid tracks).

This is intentionally distinct from:
- ``app.services.motion_zoning`` (polygon include/exclude motion zones), and
- ``app.services.ptz_tracking`` (PTZ velocity / tracking sessions).

A trip line is a directed segment ``p1 -> p2``. A track is an ordered list
of ``(x, y)`` points. A crossing event is emitted when a track segment
``prev -> curr`` intersects the trip line segment. Direction is derived from
the sign change of the cross product ``(p2-p1) x (p-p1)``:

- ``forward``  : negative side -> positive side (left of ``p1 -> p2``)
- ``backward`` : positive side -> negative side
- ``both``     : either direction (default)
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def _coerce_num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_point(value: Any) -> Optional[Tuple[float, float]]:
    """Coerce dict/list/tuple/object forms to an (x, y) tuple."""
    if value is None:
        return None
    if isinstance(value, dict):
        x = value.get("x", value.get("X", value.get("lon", value.get("cx", value.get("center_x")))))
        y = value.get("y", value.get("Y", value.get("lat", value.get("cy", value.get("center_y")))))
        if x is None or y is None:
            # {"p1":...} style is not a point
            return None
        return (_coerce_num(x), _coerce_num(y))
    if hasattr(value, "x") and hasattr(value, "y"):
        try:
            return (float(value.x), float(value.y))
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
            return (float(value[0]), float(value[1]))
        if len(value) == 2:
            try:
                return (float(value[0]), float(value[1]))
            except (TypeError, ValueError):
                return None
    return None


def _coerce_point_list(value: Any) -> List[Tuple[float, float]]:
    if value is None:
        return []
    if isinstance(value, str):
        out: List[Tuple[float, float]] = []
        for token in value.replace(";", " ").split():
            if "," in token:
                x, y = token.split(",", 1)
                try:
                    out.append((float(x), float(y)))
                except ValueError:
                    continue
        return out
    if isinstance(value, dict):
        # {"points": [...]} or {"track": [...]} or {"positions": [...]}
        for key in ("points", "track", "positions", "path", "trajectory", "centroids"):
            if value.get(key) is not None:
                return _coerce_point_list(value.get(key))
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            pt = _coerce_point(item)
            if pt is not None:
                out.append(pt)
        return out
    return []


def _normalize_direction(value: Any) -> str:
    s = str(value or "both").strip().lower().replace("-", "_").replace(" ", "_")
    if s in {"both", "either", "any", "all", "bidirectional", "two_way", "two-way", "*"}:
        return "both"
    if s in {"forward", "forwards", "a_to_b", "a2b", "positive", "pos", "left_to_right"}:
        return "forward"
    if s in {"backward", "backwards", "b_to_a", "b2a", "negative", "neg", "right_to_left"}:
        return "backward"
    return "both"


def _normalize_camera_id(value: Any) -> str:
    if value is None:
        return "default"
    return str(value)


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _side(line_p1: Tuple[float, float], line_p2: Tuple[float, float], p: Tuple[float, float]) -> float:
    """Signed cross product (p2-p1) x (p-p1). Positive = left of p1->p2."""
    return _cross(line_p2[0] - line_p1[0], line_p2[1] - line_p1[1], p[0] - line_p1[0], p[1] - line_p1[1])


def segment_crossing(
    line_p1: Tuple[float, float],
    line_p2: Tuple[float, float],
    seg_a: Tuple[float, float],
    seg_b: Tuple[float, float],
    *,
    eps: float = 1e-9,
) -> Tuple[bool, Optional[str], Optional[Tuple[float, float]]]:
    """Check whether segment seg_a->seg_b crosses line_p1->line_p2.

    Returns (crossed, direction, intersection_point). Direction is
    ``"forward"`` (neg -> pos) or ``"backward"`` (pos -> neg).
    Strict sign change is required: touching/ending exactly on the line
    (side == 0) does not count.
    """
    r = (line_p2[0] - line_p1[0], line_p2[1] - line_p1[1])
    s = (seg_b[0] - seg_a[0], seg_b[1] - seg_a[1])
    denom = _cross(r[0], r[1], s[0], s[1])
    sa = _side(line_p1, line_p2, seg_a)
    sb = _side(line_p1, line_p2, seg_b)
    if abs(sa) <= eps or abs(sb) <= eps:
        return False, None, None
    if sa * sb > 0:
        return False, None, None
    if abs(denom) <= eps:
        # Parallel but on opposite sides cannot happen; collinear handled above.
        return False, None, None
    # Parametric intersection: line_p1 + t*r == seg_a + u*s
    qp = (seg_a[0] - line_p1[0], seg_a[1] - line_p1[1])
    t = _cross(qp[0], qp[1], s[0], s[1]) / denom
    u = _cross(qp[0], qp[1], r[0], r[1]) / denom
    if not (-eps <= t <= 1.0 + eps and -eps <= u <= 1.0 + eps):
        return False, None, None
    ix = line_p1[0] + t * r[0]
    iy = line_p1[1] + t * r[1]
    direction = "forward" if sa < 0 < sb else "backward"
    return True, direction, (ix, iy)


@dataclass
class TripLine:
    """A single virtual trip line belonging to one camera."""

    p1: Tuple[float, float] = field(default_factory=lambda: (0.0, 0.0))
    p2: Tuple[float, float] = field(default_factory=lambda: (0.0, 0.0))
    camera_id: str = "default"
    name: Optional[str] = None
    direction: str = "both"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        c1 = _coerce_point(self.p1)
        c2 = _coerce_point(self.p2)
        self.p1 = c1 if c1 is not None else (0.0, 0.0)
        self.p2 = c2 if c2 is not None else (0.0, 0.0)
        self.camera_id = _normalize_camera_id(self.camera_id)
        self.direction = _normalize_direction(self.direction)
        if not self.id:
            self.id = str(uuid.uuid4())
        self.id = str(self.id)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "line_id": self.id,
            "tripwire_id": self.id,
            "camera_id": self.camera_id,
            "camera": self.camera_id,
            "p1": [self.p1[0], self.p1[1]],
            "p2": [self.p2[0], self.p2[1]],
            "start": [self.p1[0], self.p1[1]],
            "end": [self.p2[0], self.p2[1]],
            "points": [[self.p1[0], self.p1[1]], [self.p2[0], self.p2[1]]],
            "name": self.name,
            "label": self.name,
            "direction": self.direction,
        }

    @classmethod
    def from_dict(cls, data: Any, camera_id: Any = None) -> "TripLine":
        if isinstance(data, cls):
            if camera_id is not None:
                data.camera_id = _normalize_camera_id(camera_id)
            return data
        if isinstance(data, (list, tuple)) and len(data) == 2:
            c1, c2 = _coerce_point(data[0]), _coerce_point(data[1])
            return cls(p1=c1 or (0.0, 0.0), p2=c2 or (0.0, 0.0), camera_id=camera_id or "default")
        if not isinstance(data, dict):
            raise ValueError("TripLine data must be a dict or point pair")
        p1 = data.get("p1", data.get("start", data.get("a", data.get("from"))))
        p2 = data.get("p2", data.get("end", data.get("b", data.get("to"))))
        pts = data.get("points", data.get("line", data.get("segment")))
        if (p1 is None or p2 is None) and isinstance(pts, (list, tuple)) and len(pts) == 2:
            p1, p2 = pts[0], pts[1]
        cid = data.get("camera_id", data.get("camera", data.get("cam", camera_id)))
        return cls(
            p1=_coerce_point(p1) or (0.0, 0.0),
            p2=_coerce_point(p2) or (0.0, 0.0),
            camera_id=cid if cid is not None else "default",
            name=data.get("name", data.get("label")),
            direction=data.get("direction", data.get("dir", "both")),
            id=str(data.get("id", data.get("line_id", data.get("tripwire_id", uuid.uuid4())))),
        )


# Aliases for the line model.
TripWire = TripLine
TripwireLine = TripLine
VirtualTripwire = TripLine
TripLineConfig = TripLine


_lock = threading.Lock()
_TRIP_LINES: Dict[str, Dict[str, TripLine]] = {}


def _store() -> Dict[str, Dict[str, TripLine]]:
    return _TRIP_LINES


def define_trip_line(camera_id: Any = None, p1: Any = None, p2: Any = None, **kwargs: Any) -> dict:
    """Define (create or replace) a trip line for a camera.

    Flexible inputs: ``define_trip_line(camera_id, p1, p2)`` or
    ``define_trip_line({"camera_id":.., "p1":.., "p2":..})`` or kwargs.
    """
    data: Dict[str, Any] = {}
    if isinstance(camera_id, dict) and p1 is None and p2 is None:
        data.update(camera_id)
        data.update(kwargs)
        camera_id = data.pop("camera_id", data.pop("camera", "default"))
        p1 = data.pop("p1", data.pop("start", None))
        p2 = data.pop("p2", data.pop("end", None))
        if p1 is None or p2 is None:
            pts = data.pop("points", data.pop("line", None))
            if isinstance(pts, (list, tuple)) and len(pts) == 2:
                p1, p2 = pts[0], pts[1]
    else:
        data.update(kwargs)
    cid = _normalize_camera_id(data.pop("camera_id", data.pop("camera", camera_id)))
    if p1 is None:
        p1 = data.pop("p1", data.pop("start", data.pop("a", None)))
    if p2 is None:
        p2 = data.pop("p2", data.pop("end", data.pop("b", None)))
    if (p1 is None or p2 is None) and "points" in data:
        pts = data.pop("points")
        if isinstance(pts, (list, tuple)) and len(pts) == 2:
            p1, p2 = pts[0], pts[1]
    line = TripLine(
        p1=_coerce_point(p1) or (0.0, 0.0),
        p2=_coerce_point(p2) or (0.0, 0.0),
        camera_id=cid,
        name=data.get("name", data.get("label")),
        direction=data.get("direction", data.get("dir", "both")),
        id=str(data.get("id", data.get("line_id", uuid.uuid4()))),
    )
    with _lock:
        _TRIP_LINES.setdefault(cid, {})[line.id] = line
    return line.to_dict()


def set_trip_lines(camera_id: Any, lines: Any) -> List[dict]:
    """Replace all trip lines for a camera."""
    cid = _normalize_camera_id(camera_id)
    if isinstance(lines, dict):
        lines = list(lines.values())
    parsed: List[TripLine] = []
    for item in lines or []:
        try:
            parsed.append(TripLine.from_dict(item, camera_id=cid))
        except (ValueError, TypeError):
            continue
    with _lock:
        _TRIP_LINES[cid] = {ln.id: ln for ln in parsed}
    return [ln.to_dict() for ln in parsed]


def add_trip_line(camera_id: Any = None, *args: Any, **kwargs: Any) -> dict:
    if args and isinstance(args[0], dict) and camera_id is None:
        merged = dict(args[0])
        merged.update(kwargs)
        return define_trip_line(merged)
    if args and len(args) >= 2 and camera_id is not None:
        return define_trip_line(camera_id, args[0], args[1], **kwargs)
    return define_trip_line(camera_id, *args, **kwargs)


def get_trip_lines(camera_id: Any = None, **kwargs: Any) -> List[dict]:
    cid = _normalize_camera_id(camera_id if camera_id is not None else kwargs.get("camera_id", kwargs.get("camera", "default")))
    with _lock:
        lines = list(_TRIP_LINES.get(cid, {}).values())
    return [ln.to_dict() for ln in lines]


def list_trip_lines(camera_id: Any = None, **kwargs: Any) -> List[dict]:
    if camera_id is None and not kwargs:
        # List every camera's lines.
        with _lock:
            out = [ln.to_dict() for per_cam in _TRIP_LINES.values() for ln in per_cam.values()]
        return out
    return get_trip_lines(camera_id, **kwargs)


def get_trip_line(camera_id: Any, line_id: Any) -> Optional[dict]:
    cid = _normalize_camera_id(camera_id)
    with _lock:
        line = _TRIP_LINES.get(cid, {}).get(str(line_id))
    return line.to_dict() if line is not None else None


def remove_trip_line(camera_id: Any, line_id: Any = None, **kwargs: Any) -> dict:
    if line_id is None and isinstance(camera_id, dict):
        data = dict(camera_id)
        data.update(kwargs)
        camera_id = data.get("camera_id", data.get("camera", "default"))
        line_id = data.get("id", data.get("line_id"))
    cid = _normalize_camera_id(camera_id)
    with _lock:
        per_cam = _TRIP_LINES.get(cid, {})
        removed = per_cam.pop(str(line_id), None) is not None
    return {"removed": removed, "id": str(line_id), "camera_id": cid, "ok": True, "success": True}


def clear_trip_lines(camera_id: Any = None, **kwargs: Any) -> dict:
    if camera_id is None:
        with _lock:
            n = sum(len(v) for v in _TRIP_LINES.values())
            _TRIP_LINES.clear()
        return {"cleared": n, "count": n, "ok": True, "success": True}
    cid = _normalize_camera_id(camera_id)
    with _lock:
        n = len(_TRIP_LINES.get(cid, {}))
        _TRIP_LINES[cid] = {}
    return {"cleared": n, "count": n, "camera_id": cid, "ok": True, "success": True}


def evaluate_crossing(line: Any, prev: Any, curr: Any, camera_id: Any = None) -> dict:
    """Evaluate a single track step prev->curr against one trip line."""
    trip = line if isinstance(line, TripLine) else TripLine.from_dict(line, camera_id=camera_id)
    a = _coerce_point(prev)
    b = _coerce_point(curr)
    if a is None or b is None:
        return {"crossed": False, "direction": None, "line_id": trip.id, "camera_id": trip.camera_id}
    crossed, direction, point = segment_crossing(trip.p1, trip.p2, a, b)
    if crossed and trip.direction != "both" and direction != trip.direction:
        return {"crossed": False, "direction": direction, "filtered": True,
                "line_id": trip.id, "camera_id": trip.camera_id, "point": list(point or [])}
    return {"crossed": crossed, "direction": direction if crossed else None,
            "line_id": trip.id, "camera_id": trip.camera_id,
            "point": list(point) if point else None,
            "line_direction": trip.direction}


def evaluate_track(camera_id: Any = None, points: Any = None, **kwargs: Any) -> dict:
    """Evaluate an ordered point track against a camera's trip lines.

    Accepts ``evaluate_track(camera_id, [(x,y), ...])`` or kwargs with
    ``points``/``track``/``positions``. Returns ``{"events": [...],
    "crossed": bool, "count": n}``.
    """
    data: Dict[str, Any] = dict(kwargs)
    if isinstance(camera_id, dict) and points is None:
        data = {**camera_id, **kwargs}
        camera_id = data.pop("camera_id", data.pop("camera", "default"))
        points = data.pop("points", data.pop("track", data.pop("positions", None)))
    if points is None:
        for key in ("points", "track", "positions", "path", "trajectory", "centroids"):
            if data.get(key) is not None:
                points = data.pop(key)
                break
    cid = _normalize_camera_id(camera_id if camera_id is not None else data.get("camera_id", data.get("camera", "default")))
    track = _coerce_point_list(points)
    with _lock:
        lines = list(_TRIP_LINES.get(cid, {}).values())
    line_filter = data.get("line_id", data.get("id", data.get("line")))
    if line_filter is not None:
        lines = [ln for ln in lines if ln.id == str(line_filter)]
    events: List[dict] = []
    for idx in range(1, len(track)):
        prev, curr = track[idx - 1], track[idx]
        for ln in lines:
            crossed, direction, point = segment_crossing(ln.p1, ln.p2, prev, curr)
            if not crossed:
                continue
            if ln.direction != "both" and direction != ln.direction:
                continue
            events.append({
                "crossed": True,
                "line_id": ln.id,
                "camera_id": cid,
                "line_name": ln.name,
                "name": ln.name,
                "direction": direction,
                "segment_index": idx,
                "prev": [prev[0], prev[1]],
                "curr": [curr[0], curr[1]],
                "point": list(point) if point else None,
                "crossing_point": list(point) if point else None,
            })
    return {"events": events, "crossed": bool(events), "count": len(events),
            "camera_id": cid, "ok": True, "success": True}


def evaluate(camera_id: Any = None, points: Any = None, **kwargs: Any) -> dict:
    return evaluate_track(camera_id, points, **kwargs)


class TripwireService:
    """Object-oriented wrapper around the tripwire store."""

    def define(self, *a: Any, **k: Any) -> dict:
        return define_trip_line(*a, **k)

    def define_trip_line(self, *a: Any, **k: Any) -> dict:
        return define_trip_line(*a, **k)

    def add(self, *a: Any, **k: Any) -> dict:
        return add_trip_line(*a, **k)

    def set_lines(self, *a: Any, **k: Any) -> List[dict]:
        return set_trip_lines(*a, **k)

    def list(self, *a: Any, **k: Any) -> List[dict]:
        return list_trip_lines(*a, **k)

    def get_lines(self, *a: Any, **k: Any) -> List[dict]:
        return get_trip_lines(*a, **k)

    def get(self, *a: Any, **k: Any) -> Optional[dict]:
        return get_trip_line(*a, **k)

    def remove(self, *a: Any, **k: Any) -> dict:
        return remove_trip_line(*a, **k)

    def clear(self, *a: Any, **k: Any) -> dict:
        return clear_trip_lines(*a, **k)

    def evaluate(self, *a: Any, **k: Any) -> dict:
        return evaluate_track(*a, **k)

    def evaluate_track(self, *a: Any, **k: Any) -> dict:
        return evaluate_track(*a, **k)

    def evaluate_crossing(self, *a: Any, **k: Any) -> dict:
        return evaluate_crossing(*a, **k)


tripwire_service = TripwireService()
tripwire_manager = tripwire_service
service = tripwire_service
manager = tripwire_service
default_service = tripwire_service


__all__ = [
    "TripLine",
    "TripWire",
    "TripwireLine",
    "VirtualTripwire",
    "define_trip_line",
    "set_trip_lines",
    "add_trip_line",
    "get_trip_lines",
    "list_trip_lines",
    "get_trip_line",
    "remove_trip_line",
    "clear_trip_lines",
    "evaluate_crossing",
    "evaluate_track",
    "evaluate",
    "segment_crossing",
    "TripwireService",
    "tripwire_service",
]
