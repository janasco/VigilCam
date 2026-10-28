"""Line-crossing object counter service for VigilCam.

Counts virtual-line crossings per direction from *detection tracks*
(``track_id`` -> sequence of positions), aggregated per camera and per
counting line.

This is intentionally distinct from :mod:`app.services.motion_zoning`,
which performs pixel-level motion masking, occupancy grids and *blob*
counting.  This module performs no image processing at all: it consumes
already-produced tracker/detector outputs (centroids or bounding boxes)
and applies segment-intersection geometry to count crossings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


_EPS = 1e-9

#: Canonical directions. ``forward`` means travel from the negative
#: (right) side of the directed line to the positive (left) side;
#: ``backward`` is the reverse.
FORWARD = "forward"
BACKWARD = "backward"


# ---------------------------------------------------------------------------
# Point / geometry helpers
# ---------------------------------------------------------------------------

def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce_point(value: Any) -> Optional[Tuple[float, float]]:
    """Coerce a flexible point spec to ``(x, y)`` or ``None``."""
    if value is None:
        return None
    if isinstance(value, dict):
        # bbox -> center
        if any(k in value for k in ("bbox", "box", "rect", "xywh")):
            for k in ("bbox", "box", "rect", "xywh"):
                if value.get(k) is not None:
                    pt = coerce_point(value.get(k))
                    if pt is not None:
                        # 4-element bbox handled below; 2-element is a point.
                        raw = value.get(k)
                        if isinstance(raw, (list, tuple)) and len(raw) == 4:
                            x, y, w, h = (float(v) for v in raw)
                            return (x + w / 2.0, y + h / 2.0)
                    break
        for xk, yk in (("x", "y"), ("X", "Y"), ("cx", "cy"), ("center_x", "center_y"),
                       ("lon", "lat"), ("lng", "lat"), ("col", "row")):
            if value.get(xk) is not None and value.get(yk) is not None:
                return (_to_float(value.get(xk)), _to_float(value.get(yk)))
        # centroid / center / position / point nesting
        for k in ("centroid", "center", "position", "point", "pos", "xy", "coord", "coords"):
            if value.get(k) is not None:
                pt = coerce_point(value.get(k))
                if pt is not None:
                    return pt
        # left/top/width/height bbox dict
        if "width" in value and "height" in value:
            x = _to_float(value.get("x", value.get("left", value.get("xmin", 0.0))))
            y = _to_float(value.get("y", value.get("top", value.get("ymin", 0.0))))
            w = _to_float(value.get("width", value.get("w", 0.0)))
            h = _to_float(value.get("height", value.get("h", 0.0)))
            return (x + w / 2.0, y + h / 2.0)
        return None
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            return (float(value[0]), float(value[1]))
        if len(value) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            x, y, w, h = (float(v) for v in value)
            return (x + w / 2.0, y + h / 2.0)
        if len(value) == 2:
            # nested, e.g. [[x, y]] or [(x, y), ...] -> take first coercible
            for item in value:
                pt = coerce_point(item)
                if pt is not None:
                    return pt
            return None
        if len(value) > 2:
            # flat [x1, y1, x2, y2, ...] -> first pair
            try:
                return (float(value[0]), float(value[1]))
            except (TypeError, ValueError):
                return None
        if len(value) == 1:
            return coerce_point(value[0])
        return None
    if hasattr(value, "x") and hasattr(value, "y"):
        try:
            return (float(value.x), float(value.y))
        except (TypeError, ValueError):
            return None
    if hasattr(value, "cx") and hasattr(value, "cy"):
        try:
            return (float(value.cx), float(value.cy))
        except (TypeError, ValueError):
            return None
    return None


def _cross(o: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def line_side(line_p1: Tuple[float, float], line_p2: Tuple[float, float],
              point: Tuple[float, float]) -> float:
    """Signed distance (cross product) of ``point`` from directed line p1->p2.

    Positive means the point is to the left of the directed line.
    """
    return _cross(line_p1, line_p2, point)


def _on_segment(p: Tuple[float, float], q: Tuple[float, float],
                r: Tuple[float, float]) -> bool:
    return (min(p[0], r[0]) - _EPS <= q[0] <= max(p[0], r[0]) + _EPS and
            min(p[1], r[1]) - _EPS <= q[1] <= max(p[1], r[1]) + _EPS)


def segments_intersect(p1: Tuple[float, float], p2: Tuple[float, float],
                       p3: Tuple[float, float], p4: Tuple[float, float]) -> bool:
    """Return True when segments p1-p2 and p3-p4 intersect (incl. touching)."""
    d1 = _cross(p3, p4, p1)
    d2 = _cross(p3, p4, p2)
    d3 = _cross(p1, p2, p3)
    d4 = _cross(p1, p2, p4)

    if ((d1 > _EPS and d2 < -_EPS) or (d1 < -_EPS and d2 > _EPS)) and \
       ((d3 > _EPS and d4 < -_EPS) or (d3 < -_EPS and d4 > _EPS)):
        return True
    if abs(d1) <= _EPS and _on_segment(p3, p1, p4):
        return True
    if abs(d2) <= _EPS and _on_segment(p3, p2, p4):
        return True
    if abs(d3) <= _EPS and _on_segment(p1, p3, p2):
        return True
    if abs(d4) <= _EPS and _on_segment(p1, p4, p2):
        return True
    return False


def crossing_direction(line_p1: Any, line_p2: Any, prev: Any, curr: Any) -> Optional[str]:
    """Direction of a track segment ``prev`` -> ``curr`` across a line.

    Returns ``"forward"`` (negative side -> positive side),
    ``"backward"`` (positive side -> negative side), or ``None`` when the
    segment does not cross the line segment.
    """
    a = coerce_point(line_p1)
    b = coerce_point(line_p2)
    p = coerce_point(prev)
    c = coerce_point(curr)
    if a is None or b is None or p is None or c is None:
        return None
    if not segments_intersect(a, b, p, c):
        return None
    s_prev = line_side(a, b, p)
    s_curr = line_side(a, b, c)
    if s_prev < -_EPS and s_curr >= -_EPS:
        return FORWARD
    if s_prev > _EPS and s_curr <= _EPS:
        return BACKWARD
    if abs(s_prev) <= _EPS and s_curr > _EPS:
        return FORWARD
    if abs(s_prev) <= _EPS and s_curr < -_EPS:
        return BACKWARD
    # Collinear overlap (movement along the line) or touching without
    # a side change: not a crossing.
    return None


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

def _norm_key(value: Any, default: str = "default") -> str:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    if isinstance(value, bool):
        return default
    return str(value)


def _coerce_line_endpoints(spec: Any) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Coerce a line spec to ``(p1, p2)`` point pair."""
    if spec is None:
        return None
    if isinstance(spec, (list, tuple)):
        if len(spec) == 2:
            p1 = coerce_point(spec[0])
            p2 = coerce_point(spec[1])
            if p1 is not None and p2 is not None:
                return (p1, p2)
        if len(spec) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in spec):
            return ((float(spec[0]), float(spec[1])), (float(spec[2]), float(spec[3])))
        return None
    if isinstance(spec, dict):
        for k in ("points", "coords", "line", "segment", "endpoints", "xy"):
            if spec.get(k) is not None:
                res = _coerce_line_endpoints(spec.get(k))
                if res is not None:
                    return res
        p1 = coerce_point(spec.get("p1", spec.get("start", spec.get("a", spec.get("from")))))
        p2 = coerce_point(spec.get("p2", spec.get("end", spec.get("b", spec.get("to")))))
        if p1 is not None and p2 is not None:
            return (p1, p2)
        keys = ("x1", "y1", "x2", "y2")
        if all(spec.get(k) is not None for k in keys):
            try:
                return ((float(spec["x1"]), float(spec["y1"])),
                        (float(spec["x2"]), float(spec["y2"])))
            except (TypeError, ValueError):
                return None
        # start/end dicts
        s = spec.get("start") or spec.get("p1")
        e = spec.get("end") or spec.get("p2")
        if s is not None and e is not None:
            p1, p2 = coerce_point(s), coerce_point(e)
            if p1 is not None and p2 is not None:
                return (p1, p2)
    return None


@dataclass
class CountingLine:
    """A virtual counting line segment attached to a camera."""

    line_id: str = "line1"
    camera_id: Any = "default"
    p1: Tuple[float, float] = (0.0, 0.0)
    p2: Tuple[float, float] = (1.0, 0.0)
    name: Optional[str] = None

    def __post_init__(self) -> None:
        self.line_id = _norm_key(self.line_id, "line1")
        c1 = coerce_point(self.p1)
        c2 = coerce_point(self.p2)
        self.p1 = c1 if c1 is not None else (0.0, 0.0)
        self.p2 = c2 if c2 is not None else (1.0, 0.0)
        if self.name is None:
            self.name = self.line_id

    def side(self, point: Any) -> float:
        pt = coerce_point(point)
        if pt is None:
            return 0.0
        return line_side(self.p1, self.p2, pt)

    def crosses(self, prev: Any, curr: Any) -> Optional[str]:
        return crossing_direction(self.p1, self.p2, prev, curr)

    def to_dict(self) -> dict:
        return {
            "line_id": self.line_id,
            "line": self.line_id,
            "id": self.line_id,
            "name": self.name,
            "camera_id": self.camera_id,
            "camera": self.camera_id,
            "p1": [self.p1[0], self.p1[1]],
            "p2": [self.p2[0], self.p2[1]],
            "start": [self.p1[0], self.p1[1]],
            "end": [self.p2[0], self.p2[1]],
            "points": [[self.p1[0], self.p1[1]], [self.p2[0], self.p2[1]]],
            "x1": self.p1[0],
            "y1": self.p1[1],
            "x2": self.p2[0],
            "y2": self.p2[1],
        }

    @classmethod
    def from_spec(cls, spec: Any, camera_id: Any = None, line_id: Any = None) -> Optional["CountingLine"]:
        if isinstance(spec, CountingLine):
            if camera_id is not None:
                spec.camera_id = camera_id
            if line_id is not None:
                spec.line_id = _norm_key(line_id, spec.line_id)
                spec.name = spec.name or spec.line_id
            return spec
        if isinstance(spec, dict):
            cam = camera_id
            for k in ("camera_id", "camera", "cam_id", "cam"):
                if spec.get(k) is not None and cam is None:
                    cam = spec.get(k)
                    break
            lid = line_id
            for k in ("line_id", "line", "id", "name", "label"):
                if spec.get(k) is not None and lid is None:
                    lid = spec.get(k)
                    break
            endpoints = _coerce_line_endpoints(spec)
            if endpoints is None:
                return None
            name = spec.get("name", spec.get("label"))
            return cls(line_id=_norm_key(lid, "line1"),
                       camera_id=cam if cam is not None else "default",
                       p1=endpoints[0], p2=endpoints[1],
                       name=name if isinstance(name, str) else None)
        endpoints = _coerce_line_endpoints(spec)
        if endpoints is None:
            return None
        return cls(line_id=_norm_key(line_id, "line1"),
                   camera_id=camera_id if camera_id is not None else "default",
                   p1=endpoints[0], p2=endpoints[1])


@dataclass
class CrossingEvent:
    """A single counted line crossing."""

    track_id: str = "track"
    camera_id: Any = "default"
    line_id: str = "line1"
    direction: str = FORWARD
    timestamp: Optional[Any] = None
    previous: Tuple[float, float] = (0.0, 0.0)
    current: Tuple[float, float] = (0.0, 0.0)

    def to_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "track": self.track_id,
            "id": self.track_id,
            "camera_id": self.camera_id,
            "camera": self.camera_id,
            "line_id": self.line_id,
            "line": self.line_id,
            "direction": self.direction,
            "dir": self.direction,
            "timestamp": self.timestamp,
            "previous": [self.previous[0], self.previous[1]],
            "current": [self.current[0], self.current[1]],
            "prev": [self.previous[0], self.previous[1]],
            "curr": [self.current[0], self.current[1]],
        }


def _extract_position(kwargs: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    for key in ("point", "position", "pos", "centroid", "center", "xy",
                "coord", "coords", "location", "detection", "det", "observation"):
        if kwargs.get(key) is not None:
            pt = coerce_point(kwargs.get(key))
            if pt is not None:
                return pt
    if kwargs.get("x") is not None and kwargs.get("y") is not None:
        try:
            return (float(kwargs["x"]), float(kwargs["y"]))
        except (TypeError, ValueError):
            pass
    for xk, yk in (("cx", "cy"), ("X", "Y"), ("lon", "lat")):
        if kwargs.get(xk) is not None and kwargs.get(yk) is not None:
            try:
                return (float(kwargs[xk]), float(kwargs[yk]))
            except (TypeError, ValueError):
                continue
    for key in ("bbox", "box", "rect", "xywh", "bounds"):
        if kwargs.get(key) is not None:
            pt = coerce_point(kwargs.get(key))
            if pt is not None:
                return pt
    return None


# ---------------------------------------------------------------------------
# Stateful counter
# ---------------------------------------------------------------------------

class ObjectCounter:
    """Stateful per-camera / per-line crossing counter.

    Usage::

        counter = ObjectCounter()
        counter.define_line(camera_id=1, line_id="door",
                            p1=(0, 50), p2=(100, 50))
        counter.update(camera_id=1, track_id="t1", x=10, y=40)  # no crossing
        event = counter.update(camera_id=1, track_id="t1", x=10, y=60)
        # event["direction"] == "forward"
    """

    def __init__(self, **kwargs: Any) -> None:
        self._lines: Dict[Tuple[str, str], CountingLine] = {}
        self._last: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._counts: Dict[Tuple[str, str], Dict[str, int]] = {}
        self._events: List[CrossingEvent] = []
        lines = kwargs.get("lines", kwargs.get("counting_lines"))
        if lines is not None:
            self.define_lines(lines)

    # -- lines ----------------------------------------------------------
    def define_line(self, *args: Any, **kwargs: Any) -> CountingLine:
        """Define (or replace) a counting line.

        Flexible signature; all of these are equivalent::

            define_line(camera_id, line_id, p1, p2)
            define_line({"camera_id":.., "line_id":.., "p1":.., "p2":..})
            define_line(camera_id=.., line="door", start=.., end=..)
            define_line(camera_id=.., line_id=.., x1=.., y1=.., x2=.., y2=..)
        """
        spec: Any = None
        camera_id: Any = None
        line_id: Any = None
        p1: Any = None
        p2: Any = None
        if args and isinstance(args[0], (CountingLine, dict)) and len(args) == 1 and not kwargs:
            spec = args[0]
        else:
            positional = list(args)
            # (camera_id, line_id, p1, p2) / (line_id, p1, p2) / (p1, p2)
            if len(positional) == 4:
                camera_id, line_id, p1, p2 = positional
            elif len(positional) == 3:
                # ambiguous: (camera, p1, p2) or (line, p1, p2)? prefer line when str?
                camera_id, p1, p2 = positional[0], positional[1], positional[2]
                if isinstance(camera_id, (list, tuple, dict)) or coerce_point(camera_id) is not None:
                    p1, p2 = positional[1], positional[2]
                    p_tuple = _coerce_line_endpoints([positional[0], positional[1]])
                    # actually args were (p1, p2, extra?) - fallback below
                    camera_id, line_id = None, None
                    p1, p2 = positional[0], positional[1]
                    # third positional ignored unless dict-like
                    if isinstance(positional[2], dict):
                        kwargs.update(positional[2])
                    else:
                        line_id = positional[2]
                else:
                    # treat first as line_id when no camera kwarg? keep as line_id
                    line_id = camera_id
                    camera_id = kwargs.get("camera_id", kwargs.get("camera"))
            elif len(positional) == 2:
                a, b = positional
                if isinstance(a, (CountingLine, dict)):
                    spec = a
                    if isinstance(b, dict):
                        kwargs.update(b)
                    elif coerce_point(b) is None:
                        line_id = b
                else:
                    p1, p2 = a, b
            elif len(positional) == 1:
                spec = positional[0]

        merged = dict(kwargs)
        if spec is None:
            if merged.get("line") is not None and not isinstance(merged.get("line"), (list, tuple, dict)):
                pass  # line id string, keep
            spec = dict(merged)
            if camera_id is not None:
                spec.setdefault("camera_id", camera_id)
            if line_id is not None:
                spec.setdefault("line_id", line_id)
            if p1 is not None:
                spec.setdefault("p1", p1)
                spec.setdefault("start", p1)
            if p2 is not None:
                spec.setdefault("p2", p2)
                spec.setdefault("end", p2)
        else:
            if isinstance(spec, dict):
                spec = dict(spec)
                for k, v in merged.items():
                    spec.setdefault(k, v)
                if camera_id is not None:
                    spec.setdefault("camera_id", camera_id)
                if line_id is not None:
                    spec.setdefault("line_id", line_id)
            # CountingLine instance + overrides
            if isinstance(spec, CountingLine):
                if camera_id is not None:
                    spec.camera_id = camera_id
                if line_id is not None:
                    spec.line_id = _norm_key(line_id, spec.line_id)
                for k in ("camera_id", "camera", "cam_id"):
                    if merged.get(k) is not None:
                        spec.camera_id = merged[k]
                        break

        if isinstance(spec, CountingLine):
            line = spec
        else:
            cam = camera_id
            if cam is None:
                for k in ("camera_id", "camera", "cam_id", "cam"):
                    if isinstance(spec, dict) and spec.get(k) is not None:
                        cam = spec.get(k)
                        break
                if cam is None:
                    cam = merged.get("camera_id", merged.get("camera"))
            lid = line_id
            if lid is None and isinstance(spec, dict):
                for k in ("line_id", "line", "id", "name", "label"):
                    v = spec.get(k)
                    if v is not None and not isinstance(v, (list, tuple, dict)):
                        lid = v
                        break
                    if v is not None and k in ("line",) and isinstance(v, (list, tuple, dict)):
                        continue  # geometric spec, not an id
            endpoints: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None
            if isinstance(spec, dict):
                endpoints = _coerce_line_endpoints(spec)
                if endpoints is None and p1 is not None and p2 is not None:
                    c1, c2 = coerce_point(p1), coerce_point(p2)
                    if c1 is not None and c2 is not None:
                        endpoints = (c1, c2)
            else:
                endpoints = _coerce_line_endpoints(spec)
            if endpoints is None:
                raise ValueError("define_line requires two endpoints (p1/p2, start/end, points, or x1/y1/x2/y2)")
            name = None
            if isinstance(spec, dict):
                name = spec.get("name", spec.get("label"))
            line = CountingLine(line_id=_norm_key(lid, "line1"),
                                camera_id=cam if cam is not None else "default",
                                p1=endpoints[0], p2=endpoints[1],
                                name=name if isinstance(name, str) else None)
        key = (_norm_key(line.camera_id), _norm_key(line.line_id))
        self._lines[key] = line
        if key not in self._counts:
            self._counts[key] = {"forward": 0, "backward": 0, "total": 0}
        return line

    def define_lines(self, lines: Any) -> List[CountingLine]:
        if isinstance(lines, dict):
            items = []
            for lid, spec in lines.items():
                if isinstance(spec, (list, tuple)) and len(spec) == 2 and \
                        coerce_point(spec[0]) is not None and coerce_point(spec[1]) is not None:
                    items.append({"line_id": lid, "p1": spec[0], "p2": spec[1]})
                elif isinstance(spec, dict):
                    d = dict(spec)
                    d.setdefault("line_id", lid)
                    items.append(d)
                else:
                    items.append(spec)
            lines = items
        out = []
        for spec in (lines or []):
            try:
                out.append(self.define_line(spec))
            except (TypeError, ValueError):
                continue
        return out

    def remove_line(self, camera_id: Any = None, line_id: Any = None, **kwargs: Any) -> bool:
        if isinstance(camera_id, dict) and line_id is None:
            spec = camera_id
            camera_id = spec.get("camera_id", spec.get("camera"))
            line_id = spec.get("line_id", spec.get("line", spec.get("id")))
        if line_id is None:
            line_id = kwargs.get("line_id", kwargs.get("line", kwargs.get("id", kwargs.get("name"))))
        if camera_id is None:
            camera_id = kwargs.get("camera_id", kwargs.get("camera", kwargs.get("cam_id")))
        key = (_norm_key(camera_id), _norm_key(line_id, "line1"))
        removed = self._lines.pop(key, None) is not None
        self._counts.pop(key, None)
        return removed

    def get_lines(self, camera_id: Any = None) -> List[Dict[str, Any]]:
        out = []
        for (cam, _lid), line in self._lines.items():
            if camera_id is not None and cam != _norm_key(camera_id):
                continue
            out.append(line.to_dict())
        return out

    # -- tracking -------------------------------------------------------
    def update(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Feed one position for a track; return crossing event dict or None.

        Accepts ``update(camera_id, track_id, x, y)``,
        ``update({"camera_id":.., "track_id":.., "x":.., "y":..})``,
        or keyword variants (``track``/``id`` for the track,
        ``point``/``centroid``/``bbox`` for the position).
        """
        camera_id: Any = None
        track_id: Any = None
        timestamp: Any = None
        pos: Optional[Tuple[float, float]] = None

        if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
            data = dict(args[0])
            data.update(kwargs)
            kwargs = data
            args = ()
        if args:
            if len(args) >= 4:
                camera_id, track_id = args[0], args[1]
                pos = coerce_point((args[2], args[3]))
                if len(args) >= 5:
                    timestamp = args[4]
            elif len(args) == 3:
                camera_id, track_id = args[0], args[1]
                pos = coerce_point(args[2])
            elif len(args) == 2:
                # (track_id, point) or (camera_id, dict)?
                if isinstance(args[1], dict):
                    camera_id = args[0]
                    sub = dict(args[1])
                    track_id = sub.get("track_id", sub.get("track", sub.get("id")))
                    pos = _extract_position(sub) or coerce_point(args[1])
                    timestamp = sub.get("timestamp", sub.get("ts", sub.get("time")))
                else:
                    track_id = args[0]
                    pos = coerce_point(args[1])
            elif len(args) == 1:
                pos = coerce_point(args[0])

        if camera_id is None:
            for k in ("camera_id", "camera", "cam_id", "cam"):
                if kwargs.get(k) is not None:
                    camera_id = kwargs.get(k)
                    break
        if track_id is None:
            for k in ("track_id", "track", "id", "object_id", "trackId", "target"):
                if kwargs.get(k) is not None:
                    track_id = kwargs.get(k)
                    break
        if timestamp is None:
            for k in ("timestamp", "ts", "time", "at", "frame_time"):
                if kwargs.get(k) is not None:
                    timestamp = kwargs.get(k)
                    break
        if pos is None:
            pos = _extract_position(kwargs)
            if pos is None and kwargs.get("point") is None:
                # maybe x/y passed positionally already handled; last resort:
                pos = coerce_point(kwargs)
        if track_id is None or pos is None:
            return None

        cam_key = _norm_key(camera_id)
        track_key = _norm_key(track_id, "track")
        prev = self._last.get((cam_key, track_key))
        self._last[(cam_key, track_key)] = pos
        if prev is None:
            return None

        for (lcam, lid), line in list(self._lines.items()):
            if lcam != cam_key:
                continue
            direction = line.crosses(prev, pos)
            if direction is None:
                continue
            self._counts.setdefault((lcam, lid), {"forward": 0, "backward": 0, "total": 0})
            bucket = self._counts[(lcam, lid)]
            bucket[direction] = int(bucket.get(direction, 0)) + 1
            bucket["total"] = int(bucket.get("total", 0)) + 1
            event = CrossingEvent(track_id=track_key, camera_id=kwargs.get("camera_id", camera_id if camera_id is not None else cam_key),
                                  line_id=line.line_id, direction=direction,
                                  timestamp=timestamp, previous=prev, current=pos)
            self._events.append(event)
            return event.to_dict()
        return None

    def process_detections(self, detections: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        """Feed an ordered batch of detections; return crossing events."""
        if isinstance(detections, dict):
            detections = [detections]
        try:
            items = list(detections or [])
        except TypeError:
            return []
        events: List[Dict[str, Any]] = []
        for det in items:
            if isinstance(det, dict):
                data = dict(det)
                for k, v in kwargs.items():
                    data.setdefault(k, v)
                ev = self.update(data)
            elif isinstance(det, (list, tuple)) and len(det) >= 3:
                ev = self.update(*det, **kwargs)
            else:
                continue
            if ev is not None:
                events.append(ev)
        return events

    # -- queries --------------------------------------------------------
    def _filtered_keys(self, camera_id: Any = None, line_id: Any = None) -> List[Tuple[str, str]]:
        keys = list(self._lines.keys())
        if camera_id is not None:
            ck = _norm_key(camera_id)
            keys = [k for k in keys if k[0] == ck]
        if line_id is not None:
            lk = _norm_key(line_id, "")
            keys = [k for k in keys if k[1] == lk]
        return keys

    def get_counts(self, camera_id: Any = None, line_id: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """Aggregate counts, optionally filtered by camera and/or line."""
        if camera_id is None:
            for k in ("camera", "cam_id", "cam"):
                if kwargs.get(k) is not None:
                    camera_id = kwargs.get(k)
                    break
        if line_id is None:
            for k in ("line", "id", "name"):
                if kwargs.get(k) is not None and not isinstance(kwargs.get(k), (list, tuple, dict)):
                    line_id = kwargs.get(k)
                    break
        keys = self._filtered_keys(camera_id, line_id)
        fwd = sum(int(self._counts.get(k, {}).get("forward", 0)) for k in keys)
        bwd = sum(int(self._counts.get(k, {}).get("backward", 0)) for k in keys)
        total = sum(int(self._counts.get(k, {}).get("total", 0)) for k in keys)

        per_line: Dict[str, Dict[str, int]] = {}
        per_camera: Dict[str, Dict[str, int]] = {}
        per_camera_per_line: Dict[str, Dict[str, Dict[str, int]]] = {}
        lines_out: List[Dict[str, Any]] = []
        for (cam, lid) in keys:
            bucket = self._counts.get((cam, lid), {"forward": 0, "backward": 0, "total": 0})
            entry = {"forward": int(bucket.get("forward", 0)),
                     "backward": int(bucket.get("backward", 0)),
                     "total": int(bucket.get("total", 0))}
            line_spec = self._lines.get((cam, lid))
            entry_with_meta = dict(entry)
            entry_with_meta.update({"camera_id": cam, "camera": cam,
                                    "line_id": lid, "line": lid})
            if line_spec is not None:
                entry_with_meta["name"] = line_spec.name
            lines_out.append(entry_with_meta)
            agg = per_line.setdefault(lid, {"forward": 0, "backward": 0, "total": 0})
            for d in ("forward", "backward", "total"):
                agg[d] += entry[d]
            cagg = per_camera.setdefault(cam, {"forward": 0, "backward": 0, "total": 0})
            for d in ("forward", "backward", "total"):
                cagg[d] += entry[d]
            per_camera_per_line.setdefault(cam, {})[lid] = dict(entry)

        cameras_list = [{"camera_id": cam, **vals} for cam, vals in
                        sorted(per_camera.items(), key=lambda kv: kv[1]["total"], reverse=True)]
        return {
            "status": "ok",
            "ok": True,
            "success": True,
            "total": total,
            "count": total,
            "total_crossings": total,
            "crossings": total,
            "num_crossings": total,
            "forward": fwd,
            "backward": bwd,
            "positive": fwd,
            "negative": bwd,
            "entering": fwd,
            "exiting": bwd,
            "ins": fwd,
            "outs": bwd,
            "in_count": fwd,
            "out_count": bwd,
            "a_to_b": fwd,
            "b_to_a": bwd,
            "per_line": per_line,
            "per_camera": per_camera,
            "by_line": per_line,
            "by_camera": per_camera,
            "per_camera_per_line": per_camera_per_line,
            "camera_line_matrix": per_camera_per_line,
            "matrix": per_camera_per_line,
            "lines": lines_out,
            "line_counts": lines_out,
            "counts": lines_out,
            "cameras": cameras_list,
            "camera_breakdown": cameras_list,
            "num_lines": len(keys),
            "line_count": len(keys),
            "events_count": len(self._events),
            "num_events": len(self._events),
        }

    def get_events(self, camera_id: Any = None, line_id: Any = None,
                   limit: Optional[int] = None) -> List[Dict[str, Any]]:
        out = []
        for ev in self._events:
            if camera_id is not None and _norm_key(ev.camera_id) != _norm_key(camera_id):
                continue
            if line_id is not None and _norm_key(ev.line_id) != _norm_key(line_id, ""):
                continue
            out.append(ev.to_dict())
        if limit is not None:
            try:
                out = out[-int(limit):]
            except (TypeError, ValueError):
                pass
        return out

    def reset(self, camera_id: Any = None, line_id: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """Reset counts (and, by default, track memory) for scope."""
        clear_tracks = kwargs.get("clear_tracks", kwargs.get("clear_memory", True))
        if isinstance(clear_tracks, str):
            clear_tracks = clear_tracks.strip().lower() not in {"0", "false", "no", "off"}
        keys = self._filtered_keys(camera_id, line_id)
        if camera_id is None and line_id is None:
            for k in self._counts:
                self._counts[k] = {"forward": 0, "backward": 0, "total": 0}
            if clear_tracks:
                self._last.clear()
            self._events.clear()
        else:
            for k in keys:
                self._counts[k] = {"forward": 0, "backward": 0, "total": 0}
            if clear_tracks:
                for k in list(self._last.keys()):
                    if camera_id is not None and k[0] != _norm_key(camera_id):
                        continue
                    del self._last[k]
            if keys:
                keep = []
                keyset = set(keys)
                for ev in self._events:
                    if (_norm_key(ev.camera_id), _norm_key(ev.line_id)) in keyset:
                        continue
                    keep.append(ev)
                self._events = keep
        return self.get_counts(camera_id=camera_id, line_id=line_id)

    # -- aliases ----------------------------------------------------------
    add_line = define_line
    set_line = define_line
    configure_line = define_line
    upsert_line = define_line
    register_line = define_line
    create_line = define_line
    add_counting_line = define_line

    delete_line = remove_line
    clear_line = remove_line

    observe = update
    track = update
    feed = update
    ingest = update
    process = update
    process_position = update
    feed_position = update
    ingest_position = update
    count = update

    process_tracks = process_detections
    ingest_tracks = process_detections
    process_track = process_detections
    feed_tracks = process_detections
    count_tracks = process_detections

    counts = get_counts
    summary = get_counts
    get_summary = get_counts
    totals = get_counts
    get_totals = get_counts
    stats = get_counts
    get_stats = get_counts

    clear = reset
    reset_counts = reset
    clear_counts = reset

    lines = get_lines
    list_lines = get_lines

    events = get_events
    list_events = get_events


LineCrossingCounter = ObjectCounter
ObjectCountingService = ObjectCounter
CrossingCounter = ObjectCounter
TripwireCounter = ObjectCounter
VirtualLineCounter = ObjectCounter
PeopleCounter = ObjectCounter


def count_line_crossings(tracks: Any = None, lines: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Stateless helper: count crossings from tracks against lines.

    ``tracks`` may be a ``{track_id: [(x, y), ...]}`` mapping or an
    ordered list of detection dicts. ``lines`` is a line spec or list of
    line specs (see :meth:`ObjectCounter.define_line`).
    """
    counter = ObjectCounter()
    line_specs: List[Any] = []
    if lines is not None:
        line_specs = lines if isinstance(lines, (list, tuple)) else [lines]
    for extra_key in ("line", "counting_line", "counting_lines"):
        if kwargs.get(extra_key) is not None:
            extra = kwargs[extra_key]
            line_specs = list(line_specs) + (list(extra) if isinstance(extra, (list, tuple)) else [extra])
    if not line_specs:
        # default horizontal line through the middle of a 100x100 space
        line_specs = [{"camera_id": kwargs.get("camera_id", kwargs.get("camera", "default")),
                       "line_id": "line1", "p1": (0, 50), "p2": (100, 50)}]
    for spec in line_specs:
        try:
            if isinstance(spec, dict):
                d = dict(spec)
                for k, v in kwargs.items():
                    if k in ("camera_id", "camera", "cam_id") and d.get(k) is None and d.get("camera_id") is None:
                        d.setdefault("camera_id", v)
                counter.define_line(d)
            else:
                counter.define_line(spec, **{k: v for k, v in kwargs.items()
                                             if k in ("camera_id", "camera", "line_id", "line")})
        except (TypeError, ValueError):
            continue

    events: List[Dict[str, Any]] = []
    default_cam = kwargs.get("camera_id", kwargs.get("camera", "default"))
    if isinstance(tracks, dict):
        for track_id, points in tracks.items():
            cam: Any = default_cam
            pts: Any = points
            if isinstance(points, dict):
                cam = points.get("camera_id", points.get("camera", default_cam))
                pts = points.get("points", points.get("track", points.get("positions", points.get("path"))))
            try:
                seq = list(pts or [])
            except TypeError:
                continue
            for pt in seq:
                ev = counter.update(camera_id=cam, track_id=track_id,
                                    point=pt if not isinstance(pt, dict) else pt)
                # dict-form points may carry their own camera/track
                if ev is None and isinstance(pt, dict):
                    data = dict(pt)
                    data.setdefault("camera_id", cam)
                    data.setdefault("track_id", track_id)
                    ev2 = counter.update(data)
                    if ev2 is not None:
                        events.append(ev2)
                elif ev is not None:
                    events.append(ev)
    elif isinstance(tracks, (list, tuple)):
        for item in tracks:
            if isinstance(item, dict):
                data = dict(item)
                data.setdefault("camera_id", default_cam)
                ev = counter.update(data)
            elif isinstance(item, (list, tuple)):
                ev = counter.update(*item) if len(item) <= 4 else None
            else:
                ev = counter.update(point=item, camera_id=default_cam,
                                    track_id=kwargs.get("track_id", "track"))
            if ev is not None:
                events.append(ev)
    result = counter.get_counts()
    result["events"] = [e for e in events]
    result["event_list"] = result["events"]
    result["crossing_events"] = result["events"]
    return result


count_crossings = count_line_crossings
count_line_crossing = count_line_crossings
compute_crossings = count_line_crossings
analyze_crossings = count_line_crossings


default_counter = ObjectCounter()
counter = default_counter
service = default_counter
object_counter = default_counter
default_service = default_counter


__all__ = [
    "FORWARD",
    "BACKWARD",
    "CountingLine",
    "CrossingEvent",
    "ObjectCounter",
    "LineCrossingCounter",
    "ObjectCountingService",
    "CrossingCounter",
    "TripwireCounter",
    "VirtualLineCounter",
    "PeopleCounter",
    "coerce_point",
    "line_side",
    "segments_intersect",
    "crossing_direction",
    "count_line_crossings",
    "count_crossings",
    "compute_crossings",
    "default_counter",
    "counter",
]
