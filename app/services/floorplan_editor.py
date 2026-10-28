from __future__ import annotations

"""Interactive floor-plan editor helpers.

This module provides helpers for placing camera markers on a floor-plan image
and persisting their positions into a Layout's JSON payload.  It is intentionally
tolerant of many input shapes (dicts, lists, tuples, normalized or pixel
coordinates) so the frontend and tests can use whatever convention is
convenient.
"""

from typing import Any, Dict, List, Optional, Tuple, Union

CameraPositionLike = Union[dict, list, tuple]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return float(int(value))
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return float(value.strip())
        if isinstance(value, (list, tuple)) and len(value) == 1:
            return _as_float(value[0], default)
        if isinstance(value, dict):
            for key in ("x", "X", "u", "value", "val", "n"):
                if key in value and value.get(key) is not None:
                    return _as_float(value.get(key), default)
    except (TypeError, ValueError):
        return default
    return default


def _camera_id_from(pos: Any) -> Any:
    if isinstance(pos, dict):
        for key in ("camera_id", "cameraId", "camera", "cam", "cam_id", "deviceId",
                    "device_id", "id", "cameraID"):
            if key in pos and pos.get(key) is not None:
                return pos.get(key)
        # nested
        for key in ("marker", "device", "cam"):
            nested = pos.get(key)
            if isinstance(nested, dict):
                cid = _camera_id_from(nested)
                if cid is not None:
                    return cid
    if isinstance(pos, (list, tuple)):
        if len(pos) >= 3 and not isinstance(pos[0], (int, float, str, bool)):
            pass
        # convention: [camera_id, x, y]
        if len(pos) >= 3 and isinstance(pos[0], (int, str)) and not isinstance(pos[0], bool):
            # heuristic: first is id if remaining are numbers
            if isinstance(pos[1], (int, float, str, bool)) and isinstance(pos[2], (int, float, str, bool)):
                try:
                    float(pos[1]); float(pos[2])
                    return pos[0]
                except (TypeError, ValueError):
                    pass
    return None


def _xy_from(pos: Any) -> Tuple[float, float]:
    if isinstance(pos, dict):
        # direct x/y
        if "x" in pos or "X" in pos or "u" in pos or "left" in pos:
            x = pos.get("x", pos.get("X", pos.get("u", pos.get("left", pos.get("cx", 0)))))
            y = pos.get("y", pos.get("Y", pos.get("v", pos.get("top", pos.get("cy", 0)))))
            return _as_float(x, 0.0), _as_float(y, 0.0)
        # normalized aliases
        for nx_key in ("nx", "norm_x", "normalized_x", "relative_x", "rx"):
            if nx_key in pos:
                x = pos.get(nx_key)
                y = pos.get("ny", pos.get("norm_y", pos.get("normalized_y", pos.get("relative_y", pos.get("ry", 0)))))
                return _as_float(x, 0.0), _as_float(y, 0.0)
        # nested position
        for key in ("point", "pt", "p", "pos", "position", "coords", "coordinates", "location", "xy", "uv"):
            nested = pos.get(key)
            if nested is not None:
                x, y = _xy_from(nested)
                if x != 0.0 or y != 0.0 or (isinstance(nested, (list, tuple, dict))):
                    return x, y
        # list of two numbers under "value"
        if "value" in pos and isinstance(pos.get("value"), (list, tuple)):
            v = pos["value"]
            if len(v) >= 2:
                return _as_float(v[0], 0.0), _as_float(v[1], 0.0)
    if isinstance(pos, (list, tuple)):
        nums = [v for v in pos if isinstance(v, (int, float, str, bool))]
        # [camera_id, x, y]
        if len(pos) >= 3 and len(nums) >= 2:
            # try last two
            try:
                return _as_float(pos[-2], 0.0), _as_float(pos[-1], 0.0)
            except Exception:  # noqa: BLE001
                pass
        if len(pos) >= 2:
            try:
                return _as_float(pos[0], 0.0), _as_float(pos[1], 0.0)
            except Exception:  # noqa: BLE001
                pass
    return 0.0, 0.0


def normalize_position(x: Any, y: Any, width: Any = None, height: Any = None,
                      already_normalized: bool = False) -> Tuple[float, float]:
    """Return (nx, ny) normalized coordinates in [0, 1]."""
    fx = _as_float(x, 0.0)
    fy = _as_float(y, 0.0)
    if already_normalized:
        return fx, fy
    w = _as_float(width, 0.0) if width is not None else 0.0
    h = _as_float(height, 0.0) if height is not None else 0.0
    if w > 0 and (fx > 1.0):
        fx = fx / w if w else fx
    if h > 0 and (fy > 1.0):
        fy = fy / h if h else fy
    # clamp lightly
    if fx < 0:
        fx = 0.0
    if fy < 0:
        fy = 0.0
    if fx > 1:
        fx = 1.0
    if fy > 1:
        fy = 1.0
    return fx, fy


def denormalize_position(nx: Any, ny: Any, width: Any = 1.0, height: Any = 1.0) -> Tuple[float, float]:
    return _as_float(nx, 0.0) * _as_float(width, 1.0), _as_float(ny, 0.0) * _as_float(height, 1.0)


def _normalize_one(pos: Any, width: Any = None, height: Any = None) -> Dict[str, Any]:
    cid = _camera_id_from(pos)
    x, y = _xy_from(pos)
    # determine if already normalized
    already = False
    if isinstance(pos, dict):
        if any(k in pos for k in ("nx", "ny", "normalized", "norm_x", "norm_y")):
            already = True
        if pos.get("normalized") is True:
            already = True
    # heuristic: if coordinates already in [0,1], treat as normalized
    if x <= 1.0 and y <= 1.0:
        already = True
    # if clearly pixel coords and dimensions provided, convert
    if (x > 1.0 or y > 1.0) and (width is not None or height is not None):
        already = False
    nx, ny = normalize_position(x, y, width, height, already_normalized=already)
    out: Dict[str, Any] = {}
    if isinstance(pos, dict):
        out.update(pos)
    out["camera_id"] = cid if cid is not None else out.get("camera_id")
    out["camera"] = out.get("camera", cid)
    out["id"] = out.get("id", cid)
    out["x"] = nx
    out["y"] = ny
    out["nx"] = nx
    out["ny"] = ny
    if width is not None:
        out["width"] = _as_float(width, 0.0) if not isinstance(width, str) else width
    if height is not None:
        out["height"] = _as_float(height, 0.0) if not isinstance(height, str) else height
    # pixel coords convenience
    w = _as_float(width, 1.0) if width is not None else 1.0
    h = _as_float(height, 1.0) if height is not None else 1.0
    out["px"] = nx * w
    out["py"] = ny * h
    return out


def extract_camera_positions(layout: Any) -> List[Dict[str, Any]]:
    """Extract a canonical list of camera positions from a layout JSON payload."""
    data = layout
    if hasattr(data, "model_dump"):
        try:
            data = data.model_dump()
        except Exception:  # noqa: BLE001
            pass
    if not isinstance(data, dict):
        # maybe layout is the positions list
        if isinstance(data, (list, tuple)):
            data = {"camera_positions": list(data)}
        else:
            try:
                import json
                if isinstance(data, str):
                    data = json.loads(data)
            except Exception:  # noqa: BLE001
                data = {}
    if not isinstance(data, dict):
        return []
    # candidate keys
    candidates = None
    for key in ("camera_positions", "cameraPositions", "cameras", "positions", "markers",
                "devices", "camera_markers", "cameraMarkers", "points", "coords",
                "coordinates", "layout_cameras", "floorplan_cameras"):
        if key in data and data.get(key) is not None:
            candidates = data.get(key)
            break
    # nested layout
    if candidates is None:
        for nested_key in ("layout", "json_layout", "json", "data", "config", "content",
                           "definition", "structure", "value"):
            nested = data.get(nested_key)
            if isinstance(nested, dict):
                for key in ("camera_positions", "cameraPositions", "cameras", "positions",
                            "markers", "devices"):
                    if key in nested and nested.get(key) is not None:
                        candidates = nested.get(key)
                        break
            if candidates is not None:
                break
    if candidates is None:
        return []
    if isinstance(candidates, dict):
        # map camera_id -> position
        out = []
        for k, v in candidates.items():
            item = v if isinstance(v, dict) else {"position": v}
            item = dict(item) if isinstance(item, dict) else {"value": item}
            item.setdefault("camera_id", k)
            out.append(item)
        candidates = out
    if not isinstance(candidates, (list, tuple)):
        return []
    width = data.get("width", data.get("image_width", data.get("floorplan_width")))
    height = data.get("height", data.get("image_height", data.get("floorplan_height")))
    out = []
    for item in candidates:
        try:
            out.append(_normalize_one(item, width, height))
        except Exception:  # noqa: BLE001
            continue
    return out


def apply_camera_positions(layout: Any, positions: Any, width: Any = None, height: Any = None,
                          merge: bool = True) -> Any:
    """Return a layout dict with camera positions stored under canonical keys."""
    import copy
    data = layout
    if hasattr(data, "model_dump"):
        try:
            data = data.model_dump()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(data, str):
        try:
            import json
            data = json.loads(data)
        except Exception:  # noqa: BLE001
            data = {}
    if not isinstance(data, dict):
        data = {} if data is None else {"value": data}
    data = copy.deepcopy(data)
    norm_positions = []
    for p in (positions if isinstance(positions, (list, tuple)) else [positions]):
        try:
            norm_positions.append(_normalize_one(p, width or data.get("width"), height or data.get("height")))
        except Exception:  # noqa: BLE001
            continue
    # merge with existing if requested
    if merge:
        existing = extract_camera_positions(data)
        # index by camera_id
        by_id = {}
        for p in existing:
            by_id[str(p.get("camera_id"))] = p
        for p in norm_positions:
            by_id[str(p.get("camera_id"))] = p
        norm_positions = list(by_id.values())
    data["camera_positions"] = norm_positions
    # aliases
    data["cameras"] = norm_positions
    data["positions"] = norm_positions
    data["markers"] = norm_positions
    if width is not None:
        data.setdefault("width", width)
    if height is not None:
        data.setdefault("height", height)
    return data


def positions_to_preview(positions: Any, width: Any = 1.0, height: Any = 1.0) -> Dict[str, Any]:
    """Build a simple preview payload for coordinate editor."""
    pts = extract_camera_positions({"camera_positions": positions}) if not isinstance(positions, dict) else extract_camera_positions(positions)
    # if positions is list, wrap
    if isinstance(positions, (list, tuple)):
        pts = extract_camera_positions({"camera_positions": list(positions)})
    out_markers = []
    for p in pts:
        out_markers.append({
            "camera_id": p.get("camera_id"),
            "x": p.get("x"),
            "y": p.get("y"),
            "px": p.get("px", p.get("x", 0) * _as_float(width, 1.0)),
            "py": p.get("py", p.get("y", 0) * _as_float(height, 1.0)),
        })
    return {
        "ok": True,
        "status": "ok",
        "width": _as_float(width, 1.0),
        "height": _as_float(height, 1.0),
        "count": len(out_markers),
        "markers": out_markers,
        "positions": out_markers,
        "camera_positions": out_markers,
    }


class FloorplanEditor:
    """High-level editor combining positions, coordinates and preview."""

    def __init__(self, layout: Any = None, width: Any = 1.0, height: Any = 1.0):
        self.layout = layout
        self.width = width
        self.height = height
        self.positions = extract_camera_positions(layout) if layout is not None else []

    def add_marker(self, camera_id: Any, x: Any, y: Any, **kwargs) -> Dict[str, Any]:
        pos = {"camera_id": camera_id, "x": x, "y": y, **kwargs}
        pos = _normalize_one(pos, self.width, self.height)
        self.positions.append(pos)
        return pos

    def move_marker(self, camera_id: Any, x: Any, y: Any) -> Optional[Dict[str, Any]]:
        for p in self.positions:
            if str(p.get("camera_id")) == str(camera_id):
                nx, ny = normalize_position(x, y, self.width, self.height,
                                            already_normalized=(isinstance(x, float) and 0 <= x <= 1 and isinstance(y, float) and 0 <= y <= 1))
                p["x"] = nx
                p["y"] = ny
                p["nx"] = nx
                p["ny"] = ny
                p["px"] = nx * _as_float(self.width, 1.0)
                p["py"] = ny * _as_float(self.height, 1.0)
                return p
        return None

    def remove_marker(self, camera_id: Any) -> bool:
        before = len(self.positions)
        self.positions = [p for p in self.positions if str(p.get("camera_id")) != str(camera_id)]
        return len(self.positions) < before

    def to_layout(self, base: Any = None) -> Any:
        return apply_camera_positions(base if base is not None else self.layout, self.positions,
                                     width=self.width, height=self.height)

    def preview(self) -> Dict[str, Any]:
        return positions_to_preview(self.positions, self.width, self.height)

    # aliases
    def add(self, *a, **k):
        return self.add_marker(*a, **k)

    def move(self, *a, **k):
        return self.move_marker(*a, **k)

    def remove(self, *a, **k):
        return self.remove_marker(*a, **k)

    def save(self, *a, **k):
        return self.to_layout(*a, **k)

    def render_preview(self, *a, **k):
        return self.preview(*a, **k)


# Aliases
FloorPlanEditor = FloorplanEditor
FloorplanEditor = FloorplanEditor
LayoutEditor = FloorplanEditor
InteractiveFloorplanEditor = FloorplanEditor
Editor = FloorplanEditor
FloorplanEditorService = FloorplanEditor
FloorPlanEditorService = FloorplanEditor
CameraPosition = dict
CameraMarker = dict
Marker = dict
Position = dict

# Module-level convenience functions (many aliases)
def create_editor(*a, **k):
    return FloorplanEditor(*a, **k)

def edit_floorplan(*a, **k):
    return FloorplanEditor(*a, **k)

def save_positions(layout, positions, **k):
    return apply_camera_positions(layout, positions, **k)

def load_positions(layout):
    return extract_camera_positions(layout)

def get_positions(layout):
    return extract_camera_positions(layout)

def set_positions(layout, positions, **k):
    return apply_camera_positions(layout, positions, **k)

def update_positions(layout, positions, **k):
    return apply_camera_positions(layout, positions, merge=True, **k)

def normalize_positions(positions, *a, **k):
    # positions can be list; width/height from kwargs
    w = k.pop("width", k.pop("w", None))
    h = k.pop("height", k.pop("h", None))
    out = []
    for p in (positions if isinstance(positions, (list, tuple)) else [positions]):
        out.append(_normalize_one(p, w, h))
    return out

def denormalize_positions(positions, *a, **k):
    w = k.pop("width", k.pop("w", 1.0))
    h = k.pop("height", k.pop("h", 1.0))
    out = []
    for p in (positions if isinstance(positions, (list, tuple)) else [positions]):
        x, y = _xy_from(p)
        out.append({"x": x * _as_float(w, 1.0), "y": y * _as_float(h, 1.0)})
    return out

def preview(layout=None, positions=None, **k):
    width = k.pop("width", k.pop("w", 1.0))
    height = k.pop("height", k.pop("h", 1.0))
    src = positions if positions is not None else (extract_camera_positions(layout) if layout is not None else [])
    return positions_to_preview(src, width, height)

def render_preview(*a, **k):
    return preview(*a, **k)

# alias modules expect these names
normalize_camera_positions = normalize_positions
denormalize_camera_positions = denormalize_positions
extract_positions = extract_camera_positions
apply_positions = apply_camera_positions
camera_positions = extract_camera_positions
# Extra convenience aliases
normalize_coords = normalize_position
denormalize_coords = denormalize_position
map_position = normalize_position
to_layout = apply_camera_positions
save_layout_positions = apply_camera_positions
load_layout_positions = extract_camera_positions
get_camera_positions = extract_camera_positions
set_camera_positions = apply_camera_positions
update_camera_positions = apply_camera_positions
preview_layout = preview
render = preview


__all__ = [
    "FloorplanEditor", "FloorPlanEditor", "FloorplanEditor", "LayoutEditor",
    "normalize_position", "denormalize_position", "extract_camera_positions",
    "apply_camera_positions", "positions_to_preview",
]
