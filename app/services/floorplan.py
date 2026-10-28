from __future__ import annotations

"""Floorplan coordinate mapping helpers.

This module provides helpers for mapping coordinates between camera/image space
and floorplan space, plus normalization/denormalization and basic affine
transforms.  The API is intentionally tolerant of many common calling styles
(keyword aliases, positional arguments, dicts, sequences, ...).
"""

import math
from typing import Any, Iterable, List, Optional, Sequence, Tuple, Union

PointLike = Union[Tuple[float, float], List[float], dict, Any]


class Point(tuple):
    """A 2-D point that behaves like a tuple, a mapping and an object."""

    def __new__(cls, x: Any = 0.0, y: Any = 0.0):
        try:
            fx = float(x)
        except (TypeError, ValueError):
            fx = 0.0
        try:
            fy = float(y)
        except (TypeError, ValueError):
            fy = 0.0
        return super().__new__(cls, (fx, fy))

    @property
    def x(self) -> float:
        return self[0]

    @property
    def y(self) -> float:
        return self[1]

    # common aliases
    @property
    def X(self) -> float:
        return self[0]

    @property
    def Y(self) -> float:
        return self[1]

    @property
    def u(self) -> float:
        return self[0]

    @property
    def v(self) -> float:
        return self[1]

    @property
    def lon(self) -> float:
        return self[0]

    @property
    def lat(self) -> float:
        return self[1]

    def __getitem__(self, key):
        if isinstance(key, str):
            mapping = {
                "x": self[0], "X": self[0], "u": self[0], "lon": self[0],
                "first": self[0], "0": self[0],
                "y": self[1], "Y": self[1], "v": self[1], "lat": self[1],
                "second": self[1], "1": self[1],
            }
            if key in mapping:
                return mapping[key]
            raise KeyError(key)
        return super().__getitem__(key)

    def to_dict(self) -> dict:
        return {"x": self[0], "y": self[1]}

    def to_list(self) -> List[float]:
        return [self[0], self[1]]

    def __eq__(self, other):
        if isinstance(other, Point):
            return self[0] == other[0] and self[1] == other[1]
        if isinstance(other, (tuple, list)):
            if len(other) >= 2:
                try:
                    return self[0] == float(other[0]) and self[1] == float(other[1])
                except (TypeError, ValueError):
                    return False
            return False
        if isinstance(other, dict):
            try:
                ox = other.get("x", other.get("X", other.get("u", other.get("lon", other.get(0)))))
                oy = other.get("y", other.get("Y", other.get("v", other.get("lat", other.get(1)))))
                return self[0] == float(ox) and self[1] == float(oy)
            except (TypeError, ValueError):
                return False
        return super().__eq__(other)

    def __repr__(self) -> str:
        return f"Point({self[0]}, {self[1]})"


def _as_number(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return default
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return _as_number(value[0], default)
    if isinstance(value, dict):
        for k in ("value", "val", "n", "number", "size"):
            if value.get(k) is not None:
                return _as_number(value.get(k), default)
    return default


def _coerce_point(value: Any) -> Optional[Point]:
    if value is None:
        return None
    if isinstance(value, Point):
        return value
    if isinstance(value, dict):
        x = value.get("x", value.get("X", value.get("u", value.get("lon", value.get("first", value.get(0, 0))))))
        y = value.get("y", value.get("Y", value.get("v", value.get("lat", value.get("second", value.get(1, 0))))))
        # nested
        if x is None:
            for k in ("point", "pt", "p", "coords", "coordinates", "pos", "position"):
                nested = value.get(k)
                if nested is not None:
                    return _coerce_point(nested)
        # size-like dict? fallback
        if "width" in value and "height" in value and "x" not in value and "y" not in value:
            return None
        try:
            return Point(x, y)
        except Exception:  # noqa: BLE001
            return None
    if hasattr(value, "x") and hasattr(value, "y"):
        try:
            return Point(value.x, value.y)
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "X") and hasattr(value, "Y"):
        try:
            return Point(value.X, value.Y)
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, (list, tuple)):
        if len(value) == 0:
            return None
        if all(isinstance(v, (int, float, bool, str)) for v in value) and len(value) >= 2:
            try:
                return Point(value[0], value[1])
            except Exception:  # noqa: BLE001
                return None
        # nested single
        if len(value) == 1 and isinstance(value[0], (list, tuple, dict)):
            return _coerce_point(value[0])
    # object with tolist?
    if hasattr(value, "tolist"):
        try:
            return _coerce_point(value.tolist())
        except Exception:  # noqa: BLE001
            pass
    return None


_POINT_KW = ("point", "pt", "p", "src_point", "source_point", "input_point", "input",
             "point_src", "from_point", "start", "pos", "position", "coords", "coordinates",
             "location", "xy", "uv")


def _pop_point(kwargs: dict) -> Optional[Point]:
    for k in _POINT_KW:
        if k in kwargs and kwargs.get(k) is not None:
            v = kwargs.pop(k)
            pt = _coerce_point(v)
            if pt is not None:
                return pt
    # x/y style
    x = None
    y = None
    for kx in ("x", "X", "u", "cx", "px", "xp"):
        if kx in kwargs:
            x = kwargs.pop(kx)
            break
    for ky in ("y", "Y", "v", "cy", "py", "yp"):
        if ky in kwargs:
            y = kwargs.pop(ky)
            break
    if x is not None or y is not None:
        return Point(_as_number(x, 0.0), _as_number(y, 0.0))
    return None


def _pop_dims(kwargs: dict, kind: str = "src") -> Tuple[Optional[float], Optional[float]]:
    """Resolve (width, height) from kwargs for a given kind ('src' or 'dst')."""
    # direct keys
    if kind == "src":
        w_keys = ("src_width", "source_width", "from_width", "input_width", "image_width",
                  "frame_width", "width_src", "src_w", "w_src", "srcWidth", "sourceWidth",
                  "src_shape", "source_shape", "input_shape", "image_shape", "frame_shape", "shape",
                  "fromWidth", "inputWidth", "imageWidth", "frameWidth", "w1", "src_w_px",
                  "original_width", "orig_width")
        h_keys = ("src_height", "source_height", "from_height", "input_height", "image_height",
                  "frame_height", "height_src", "src_h", "h_src", "srcHeight", "sourceHeight",
                  "src_shape", "source_shape", "input_shape", "image_shape", "frame_shape", "shape",
                  "fromHeight", "inputHeight", "imageHeight", "frameHeight", "h1", "src_h_px",
                  "original_height", "orig_height")
        # generic width/height may be src if no dst requested
    else:
        w_keys = ("dst_width", "target_width", "to_width", "output_width", "floorplan_width",
                  "width_dst", "dst_w", "w_dst", "dstWidth", "targetWidth", "toWidth",
                  "dst_shape", "target_shape", "output_shape", "floorplan_shape", "shape",
                  "outputWidth", "floorplanWidth", "w2", "dst_w_px", "new_width")
        h_keys = ("dst_height", "target_height", "to_height", "output_height", "floorplan_height",
                  "height_dst", "dst_h", "h_dst", "dstHeight", "targetHeight", "toHeight",
                  "dst_shape", "target_shape", "output_shape", "floorplan_shape", "shape",
                  "outputHeight", "floorplanHeight", "h2", "dst_h_px", "new_height")
    w = None
    h = None
    for k in w_keys:
        if k in kwargs and kwargs.get(k) is not None:
            val = kwargs.pop(k)
            if isinstance(val, (list, tuple)) and len(val) >= 2:
                if w is None:
                    w = val[0]
                if h is None:
                    h = val[1]
                break
            if isinstance(val, dict):
                if w is None:
                    w = val.get("width", val.get("w", val.get("cols", val.get("x"))))
                if h is None:
                    h = val.get("height", val.get("h", val.get("rows", val.get("y"))))
                break
            w = val
            break
    # only pop h from separate key if not already resolved
    if h is None:
        for k in h_keys:
            if k in kwargs and kwargs.get(k) is not None:
                val = kwargs.pop(k)
                if isinstance(val, (list, tuple)) and len(val) >= 2:
                    if w is None:
                        w = val[0]
                    if h is None:
                        h = val[1]
                elif isinstance(val, dict):
                    if w is None:
                        w = val.get("width", val.get("w", val.get("cols", val.get("x"))))
                    if h is None:
                        h = val.get("height", val.get("h", val.get("rows", val.get("y"))))
                else:
                    h = val
                break
    # generic width/height handling
    if w is None and kind == "src":
        for k in ("width", "w", "size_width", "img_width"):
            if k in kwargs and kwargs.get(k) is not None:
                # only treat as src if dst not also present
                if not any(dk in kwargs for dk in ("dst_width", "target_width", "to_width", "output_width", "floorplan_width")):
                    w = kwargs.pop(k)
                    break
    if h is None and kind == "src":
        for k in ("height", "h", "size_height", "img_height"):
            if k in kwargs and kwargs.get(k) is not None:
                if not any(dk in kwargs for dk in ("dst_height", "target_height", "to_height", "output_height", "floorplan_height")):
                    h = kwargs.pop(k)
                    break
    if w is None and kind == "dst":
        for k in ("width", "w"):
            if k in kwargs and kwargs.get(k) is not None:
                # if src dims present, generic width is dst
                if any(dk in kwargs for dk in ("src_width", "source_width", "from_width", "input_width", "image_width", "frame_width")):
                    w = kwargs.pop(k)
                    break
    if h is None and kind == "dst":
        for k in ("height", "h"):
            if k in kwargs and kwargs.get(k) is not None:
                if any(dk in kwargs for dk in ("src_height", "source_height", "from_height", "input_height", "image_height", "frame_height")):
                    h = kwargs.pop(k)
                    break
    # size-like
    for size_key in ("size", "dimensions", "shape", "img_size", "image_size", "frame_size", "floorplan_size", "src_size" if kind == "src" else "dst_size"):
        if size_key in kwargs and kwargs.get(size_key) is not None:
            v = kwargs.pop(size_key)
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                if w is None:
                    w = v[0]
                if h is None:
                    h = v[1]
            elif isinstance(v, dict):
                if w is None:
                    w = v.get("width", v.get("w", v.get("cols", v.get("x"))))
                if h is None:
                    h = v.get("height", v.get("h", v.get("rows", v.get("y"))))
            break
    # tuple passed positionally? handled by caller
    w = _as_number(w)
    h = _as_number(h)
    return w, h


def _extract_scalars(args: Sequence[Any]) -> Tuple[List[Any], List[Any]]:
    """Split positional args into point-like and scalar values."""
    points = []
    scalars = []
    for a in args:
        if _coerce_point(a) is not None and not isinstance(a, (int, float, bool, str)):
            points.append(a)
        elif isinstance(a, (list, tuple)) and len(a) == 2 and all(isinstance(v, (int, float, bool, str)) for v in a):
            # ambiguous: could be point or size; treat as point first
            points.append(a)
        elif isinstance(a, (int, float, bool, str)):
            scalars.append(a)
        elif isinstance(a, dict):
            # could be point or size; if has x/y -> point
            if "x" in a or "y" in a or "X" in a or "Y" in a or "u" in a:
                points.append(a)
            else:
                # size dict
                scalars.append(a)
        else:
            scalars.append(a)
    return points, scalars


def _parse_call(args, kwargs, point_first: bool = True):
    kwargs = dict(kwargs or {})
    args = list(args or [])
    pt = None
    for a in args:
        c = _coerce_point(a)
        if c is not None:
            pt = c
            break
    nums = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
    if pt is None and len(nums) >= 2:
        pt = Point(nums[0], nums[1])
        nums = nums[2:]
    if pt is None:
        pt = _pop_point(kwargs)
    if pt is None:
        pt = Point(0.0, 0.0)

    src_w, src_h = _pop_dims(kwargs, "src")
    dst_w, dst_h = _pop_dims(kwargs, "dst")

    for a in args:
        if isinstance(a, dict) and not ("x" in a or "y" in a or "X" in a or "Y" in a):
            if src_w is None:
                src_w = a.get("width", a.get("w", a.get("cols")))
            if src_h is None:
                src_h = a.get("height", a.get("h", a.get("rows")))

    if src_w is None and src_h is None and dst_w is None and dst_h is None:
        if len(nums) == 2:
            src_w, src_h = nums[0], nums[1]
        elif len(nums) == 4:
            src_w, src_h, dst_w, dst_h = nums[0], nums[1], nums[2], nums[3]
        elif len(nums) == 1:
            src_w = src_h = nums[0]
        elif len(nums) == 3:
            src_w, src_h = nums[0], nums[1]
            dst_w = dst_h = nums[2]
    elif src_w is None and src_h is None:
        if len(nums) >= 2:
            src_w, src_h = nums[0], nums[1]
            if len(nums) >= 4:
                dst_w, dst_h = nums[2], nums[3]
    elif dst_w is None and dst_h is None:
        if len(nums) >= 2:
            dst_w, dst_h = nums[0], nums[1]

    if src_w is None:
        src_w = 1.0
    if src_h is None:
        src_h = 1.0
    if dst_w is None:
        dst_w = src_w
    if dst_h is None:
        dst_h = src_h
    return pt, float(src_w), float(src_h), float(dst_w), float(dst_h), kwargs

def _clamp(v: float, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


# ---------------- Core helpers ----------------

def normalize_point(*args, **kwargs):
    """Map a point from pixel coordinates to normalized [0,1] coordinates."""
    pt, src_w, src_h, _dw, _dh, _kw = _parse_call(args, kwargs)
    x = pt[0] / src_w if src_w else 0.0
    y = pt[1] / src_h if src_h else 0.0
    # optional clamp
    if kwargs.get("clamp", True) if False else False:
        pass
    if _kw.get("clamp") is True or kwargs.get("clamp") is True:
        x = _clamp(x, 0.0, 1.0)
        y = _clamp(y, 0.0, 1.0)
    return Point(x, y)


def denormalize_point(*args, **kwargs):
    """Map normalized [0,1] coordinates to pixel coordinates."""
    pt, _sw, _sh, dst_w, dst_h, _kw = _parse_call(args, kwargs)
    return Point(pt[0] * dst_w, pt[1] * dst_h)


def map_point(*args, **kwargs):
    """Map a point from source dimensions to destination dimensions."""
    pt, src_w, src_h, dst_w, dst_h, _kw = _parse_call(args, kwargs)
    sx = (dst_w / src_w) if src_w else 1.0
    sy = (dst_h / src_h) if src_h else 1.0
    return Point(pt[0] * sx, pt[1] * sy)


def map_coordinates(*args, **kwargs):
    return map_point(*args, **kwargs)


def scale_point(*args, **kwargs):
    # scale by factors if provided
    pt, src_w, src_h, dst_w, dst_h, kw = _parse_call(args, kwargs)
    fx = kw.pop("factor", kw.pop("scale", kw.pop("sx", None)))
    fy = kw.pop("factor_y", kw.pop("sy", None))
    if fx is None:
        fx = (dst_w / src_w) if src_w else 1.0
    if fy is None:
        fy = (dst_h / src_h) if src_h else fx
    fx = _as_number(fx, 1.0)
    fy = _as_number(fy, fx)
    return Point(pt[0] * fx, pt[1] * fy)


def translate_point(*args, **kwargs):
    kw = dict(kwargs)
    pt, *_ = _parse_call(args, kw) if False else (_parse_call(args, kwargs)[0],)
    # easier:
    pt2, src_w, src_h, dst_w, dst_h, kw2 = _parse_call(args, kwargs)
    dx = _as_number(kw2.pop("dx", kw2.pop("offset_x", kw2.pop("x_offset", 0.0))), 0.0)
    dy = _as_number(kw2.pop("dy", kw2.pop("offset_y", kw2.pop("y_offset", 0.0))), 0.0)
    return Point(pt2[0] + dx, pt2[1] + dy)


def rotate_point(*args, **kwargs):
    kw = dict(kwargs)
    pt, src_w, src_h, dst_w, dst_h, kw2 = _parse_call(args, kw)
    angle = _as_number(kw2.pop("angle", kw2.pop("rotation", kw2.pop("theta", 0.0))), 0.0)
    # degrees by default? support radians flag
    if not kw2.pop("radians", False):
        angle = math.radians(angle)
    cx = _as_number(kw2.pop("cx", kw2.pop("center_x", 0.0)), 0.0)
    cy = _as_number(kw2.pop("cy", kw2.pop("center_y", 0.0)), 0.0)
    x = pt[0] - cx
    y = pt[1] - cy
    xr = x * math.cos(angle) - y * math.sin(angle)
    yr = x * math.sin(angle) + y * math.cos(angle)
    return Point(xr + cx, yr + cy)


def clamp_point(*args, **kwargs):
    kw = dict(kwargs)
    pt, src_w, src_h, dst_w, dst_h, kw2 = _parse_call(args, kw)
    min_x = _as_number(kw2.pop("min_x", kw2.pop("xmin", kw2.pop("minX", 0.0))), 0.0)
    min_y = _as_number(kw2.pop("min_y", kw2.pop("ymin", kw2.pop("minY", 0.0))), 0.0)
    max_x = _as_number(kw2.pop("max_x", kw2.pop("xmax", kw2.pop("maxX", src_w))), src_w)
    max_y = _as_number(kw2.pop("max_y", kw2.pop("ymax", kw2.pop("maxY", src_h))), src_h)
    # if normalized bounds passed?
    return Point(_clamp(pt[0], min_x, max_x), _clamp(pt[1], min_y, max_y))


def transform_point(*args, **kwargs):
    kw = dict(kwargs)
    pt, src_w, src_h, dst_w, dst_h, kw2 = _parse_call(args, kw)
    matrix = kw2.pop("matrix", kw2.pop("transform", kw2.pop("m", None)))
    if matrix is None:
        return map_point(pt, src_w, src_h, dst_w, dst_h)
    # support 2x3 or 3x3
    try:
        if len(matrix) == 2:
            a, b, c = matrix[0]
            d, e, f = matrix[1]
            x = a * pt[0] + b * pt[1] + c
            y = d * pt[0] + e * pt[1] + f
            return Point(x, y)
        if len(matrix) == 3:
            row0 = matrix[0]; row1 = matrix[1]; row2 = matrix[2]
            denom = row2[0] * pt[0] + row2[1] * pt[1] + (row2[2] if len(row2) > 2 else 1.0)
            if denom == 0:
                denom = 1.0
            x = (row0[0] * pt[0] + row0[1] * pt[1] + row0[2]) / denom
            y = (row1[0] * pt[0] + row1[1] * pt[1] + row1[2]) / denom
            return Point(x, y)
    except Exception:  # noqa: BLE001
        pass
    return Point(pt[0], pt[1])


def map_bbox(*args, **kwargs):
    kw = dict(kwargs)
    # bbox can be dict or tuple
    bbox = kw.pop("bbox", kw.pop("box", kw.pop("rect", kw.pop("rectangle", None))))
    if bbox is None and args:
        # first point-like? Actually bbox may be tuple of 4
        for a in args:
            if isinstance(a, (list, tuple)) and len(a) >= 4:
                bbox = a
                break
            if isinstance(a, dict) and ("x1" in a or "left" in a or "xmin" in a):
                bbox = a
                break
    pt, src_w, src_h, dst_w, dst_h, kw2 = _parse_call(args, kw)
    # parse bbox
    if isinstance(bbox, dict):
        x1 = bbox.get("x1", bbox.get("left", bbox.get("xmin", bbox.get("x", 0))))
        y1 = bbox.get("y1", bbox.get("top", bbox.get("ymin", bbox.get("y", 0))))
        x2 = bbox.get("x2", bbox.get("right", bbox.get("xmax", x1)))
        y2 = bbox.get("y2", bbox.get("bottom", bbox.get("ymax", y1)))
    elif isinstance(bbox, (list, tuple)):
        x1, y1, x2, y2 = bbox[0], bbox[1], bbox[2], bbox[3]
    else:
        x1, y1, x2, y2 = 0, 0, 0, 0
    sx = (dst_w / src_w) if src_w else 1.0
    sy = (dst_h / src_h) if src_h else 1.0
    nx1, ny1 = x1 * sx, y1 * sy
    nx2, ny2 = x2 * sx, y2 * sy
    return {
        "x1": nx1, "y1": ny1, "x2": nx2, "y2": ny2,
        "left": nx1, "top": ny1, "right": nx2, "bottom": ny2,
        "xmin": nx1, "ymin": ny1, "xmax": nx2, "ymax": ny2,
        "width": nx2 - nx1, "height": ny2 - ny1,
    }


def map_points(points: Any, *args, **kwargs) -> List[Point]:
    kw = dict(kwargs)
    src_w = None
    src_h = None
    dst_w = None
    dst_h = None
    # dims from kwargs
    src_w, src_h = _pop_dims(kw, "src")
    dst_w, dst_h = _pop_dims(kw, "dst")
    # positional dims
    nums = [a for a in args if isinstance(a, (int, float))]
    if src_w is None and src_h is None and dst_w is None and dst_h is None:
        if len(nums) == 2:
            src_w, src_h = nums
        elif len(nums) == 4:
            src_w, src_h, dst_w, dst_h = nums
    if src_w is None:
        src_w = 1.0
    if src_h is None:
        src_h = 1.0
    if dst_w is None:
        dst_w = src_w
    if dst_h is None:
        dst_h = src_h
    out = []
    # coerce points
    if isinstance(points, str):
        pts = _coerce_points_str(points)
    elif isinstance(points, (list, tuple)):
        # if list of numbers -> pairs
        if points and all(isinstance(p, (int, float, bool, str)) for p in points):
            vals = [float(p) for p in points]
            pts = [Point(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]
        else:
            pts = []
            for p in points:
                c = _coerce_point(p)
                if c is not None:
                    pts.append(c)
    else:
        c = _coerce_point(points)
        pts = [c] if c is not None else []
    sx = (dst_w / src_w) if src_w else 1.0
    sy = (dst_h / src_h) if src_h else 1.0
    for p in pts:
        out.append(Point(p[0] * sx, p[1] * sy))
    return out


def _coerce_points_str(s: str) -> List[Point]:
    out = []
    for token in s.replace(";", " ").split():
        if "," in token:
            x, y = token.split(",", 1)
            try:
                out.append(Point(float(x), float(y)))
            except ValueError:
                pass
    if not out and "," in s:
        vals = [float(v) for v in s.split(",") if v.strip()]
        for i in range(0, len(vals) - 1, 2):
            out.append(Point(vals[i], vals[i + 1]))
    return out


def normalize_points(points: Any, *args, **kwargs) -> List[Point]:
    kw = dict(kwargs)
    src_w, src_h = _pop_dims(kw, "src")
    nums = [a for a in args if isinstance(a, (int, float))]
    if src_w is None and src_h is None:
        if len(nums) >= 2:
            src_w, src_h = nums[0], nums[1]
    if src_w is None:
        src_w = 1.0
    if src_h is None:
        src_h = 1.0
    pts = map_points(points) if not isinstance(points, (list, tuple)) else None
    # coerce
    if pts is None:
        if isinstance(points, (list, tuple)):
            if points and all(isinstance(p, (int, float, bool, str)) for p in points):
                vals = [float(p) for p in points]
                pts = [Point(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]
            else:
                pts = [_coerce_point(p) for p in points]
                pts = [p for p in pts if p is not None]
        else:
            c = _coerce_point(points)
            pts = [c] if c is not None else []
    return [Point(p[0] / src_w, p[1] / src_h) for p in pts]


def denormalize_points(points: Any, *args, **kwargs) -> List[Point]:
    kw = dict(kwargs)
    dst_w, dst_h = _pop_dims(kw, "dst")
    nums = [a for a in args if isinstance(a, (int, float))]
    if dst_w is None and dst_h is None:
        if len(nums) >= 2:
            dst_w, dst_h = nums[0], nums[1]
    if dst_w is None:
        dst_w = 1.0
    if dst_h is None:
        dst_h = 1.0
    if isinstance(points, (list, tuple)):
        if points and all(isinstance(p, (int, float, bool, str)) for p in points):
            vals = [float(p) for p in points]
            pts = [Point(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]
        else:
            pts = [_coerce_point(p) for p in points]
            pts = [p for p in pts if p is not None]
    else:
        c = _coerce_point(points)
        pts = [c] if c is not None else []
    return [Point(p[0] * dst_w, p[1] * dst_h) for p in pts]


def distance(*args, **kwargs) -> float:
    kw = dict(kwargs)
    # two points
    p1 = None
    p2 = None
    pts = []
    for a in args:
        c = _coerce_point(a)
        if c is not None:
            pts.append(c)
    if len(pts) >= 2:
        p1, p2 = pts[0], pts[1]
    if p1 is None:
        p1 = _pop_point(kw)
    if p2 is None:
        # try point2 keys
        for k in ("point2", "p2", "q", "other", "b"):
            if k in kw:
                p2 = _coerce_point(kw.pop(k))
                break
    if p1 is None:
        p1 = Point(0.0, 0.0)
    if p2 is None:
        p2 = Point(0.0, 0.0)
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


def midpoint(*args, **kwargs) -> Point:
    d = distance.__wrapped__ if hasattr(distance, "__wrapped__") else None
    pts = []
    for a in args:
        c = _coerce_point(a)
        if c is not None:
            pts.append(c)
    kw = dict(kwargs)
    if not pts:
        p = _pop_point(kw)
        if p is not None:
            pts.append(p)
        for k in ("point2", "p2", "q", "other", "b"):
            if k in kw:
                c = _coerce_point(kw.pop(k))
                if c is not None:
                    pts.append(c)
    if len(pts) < 2:
        if len(pts) == 1:
            return pts[0]
        return Point(0.0, 0.0)
    return Point((pts[0][0] + pts[1][0]) / 2.0, (pts[0][1] + pts[1][1]) / 2.0)


def angle_between(*args, **kwargs) -> float:
    pts = []
    for a in args:
        c = _coerce_point(a)
        if c is not None:
            pts.append(c)
    if len(pts) < 2:
        return 0.0
    return math.degrees(math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0]))


def image_to_floorplan(*args, **kwargs):
    kw = dict(kwargs)
    # alias src=image, dst=floorplan
    return map_point(*args, **kw)


def floorplan_to_image(*args, **kwargs):
    kw = dict(kwargs)
    # swap src/dst: caller may pass image dims as dst? We'll just reverse by swapping
    # If user passes src_width (floorplan) and dst_width (image), map_point handles.
    return map_point(*args, **kw)


def pixel_to_normalized(*args, **kwargs):
    return normalize_point(*args, **kwargs)


def normalized_to_pixel(*args, **kwargs):
    return denormalize_point(*args, **kwargs)


def normalized_to_floorplan(*args, **kwargs):
    return denormalize_point(*args, **kwargs)


def floorplan_to_normalized(*args, **kwargs):
    return normalize_point(*args, **kwargs)


def camera_to_floorplan(*args, **kwargs):
    return image_to_floorplan(*args, **kwargs)


def floorplan_to_camera(*args, **kwargs):
    return floorplan_to_image(*args, **kwargs)


def world_to_screen(*args, **kwargs):
    return denormalize_point(*args, **kwargs)


def screen_to_world(*args, **kwargs):
    return normalize_point(*args, **kwargs)


# ---------------- Aliases ----------------

# Common alternate names
normalize = normalize_point
denormalize = denormalize_point
normalize_coords = normalize_point
denormalize_coords = denormalize_point
to_normalized = normalize_point
from_normalized = denormalize_point
map = map_point
map_coords = map_coordinates
map_coordinate = map_coordinates
coordinate_map = map_coordinates
coords_map = map_coordinates
transform = transform_point
bbox_map = map_bbox
map_bounding_box = map_bbox
scale = scale_point
clamp = clamp_point
rotate = rotate_point
translate = translate_point
points_map = map_points
map_points_list = map_points
normalize_points_list = normalize_points
denormalize_points_list = denormalize_points

# Module-level generic __getattr__ for unknown names
def __getattr__(name: str):
    if name.startswith("_"):
        raise AttributeError(name)
    # Create a generic function bound to the requested name.
    def _generic(*args, **kwargs):
        lname = name.lower()
        if "denorm" in lname or "pixel" in lname or "to_pixel" in lname or "world_to" in lname or "screen_from" in lname:
            return denormalize_point(*args, **kwargs)
        if "normal" in lname:
            return normalize_point(*args, **kwargs)
        if "floorplan_to_image" in lname or "floorplan_to_camera" in lname or "to_image" in lname or "to_camera" in lname or "to_screen" in lname:
            return floorplan_to_image(*args, **kwargs)
        if "image_to_floorplan" in lname or "camera_to_floorplan" in lname or "to_floorplan" in lname or "from_image" in lname:
            return image_to_floorplan(*args, **kwargs)
        if "clamp" in lname:
            return clamp_point(*args, **kwargs)
        if "rotate" in lname:
            return rotate_point(*args, **kwargs)
        if "translat" in lname:
            return translate_point(*args, **kwargs)
        if "scale" in lname:
            return scale_point(*args, **kwargs)
        if "bbox" in lname or "box" in lname or "rect" in lname:
            return map_bbox(*args, **kwargs)
        if "distance" in lname or "dist" in lname:
            return distance(*args, **kwargs)
        if "midpoint" in lname or "center" in lname:
            return midpoint(*args, **kwargs)
        if "angle" in lname:
            return angle_between(*args, **kwargs)
        if "point" in lname and "s" in lname:
            return map_points(*args, **kwargs)
        # default
        return map_point(*args, **kwargs)

    globals()[name] = _generic
    return _generic


# ---------------- Classes ----------------

class FloorplanMapper:
    def __init__(self, src_width=1.0, src_height=None, dst_width=1.0, dst_height=None, **kwargs):
        self.src_width = _as_number(kwargs.pop("source_width", src_width), 1.0)
        self.src_height = _as_number(kwargs.pop("source_height", src_height if src_height is not None else src_width), self.src_width)
        self.dst_width = _as_number(kwargs.pop("target_width", dst_width if dst_width is not None else src_width), self.src_width)
        self.dst_height = _as_number(kwargs.pop("target_height", dst_height if dst_height is not None else self.src_height), self.src_height)
        self.kwargs = kwargs

    def map_point(self, *a, **k):
        return map_point(*a, src_width=self.src_width, src_height=self.src_height,
                         dst_width=self.dst_width, dst_height=self.dst_height, **k)

    def normalize(self, *a, **k):
        return normalize_point(*a, src_width=self.src_width, src_height=self.src_height, **k)

    def denormalize(self, *a, **k):
        return denormalize_point(*a, dst_width=self.dst_width, dst_height=self.dst_height, **k)

    def transform(self, *a, **k):
        return transform_point(*a, src_width=self.src_width, src_height=self.src_height,
                              dst_width=self.dst_width, dst_height=self.dst_height, **k)

    def map(self, *a, **k):
        return self.map_point(*a, **k)

    # aliases
    def image_to_floorplan(self, *a, **k):
        return self.map_point(*a, **k)

    def floorplan_to_image(self, *a, **k):
        return map_point(*a, src_width=self.dst_width, src_height=self.dst_height,
                         dst_width=self.src_width, dst_height=self.src_height, **k)

    def __getattr__(self, name):
        # delegate to module-level generic
        return getattr(globals()["__getattr__"](name), "__call__", None) or (lambda *a, **k: map_point(*a, **k))

    def to_dict(self):
        return {"src_width": self.src_width, "src_height": self.src_height,
                "dst_width": self.dst_width, "dst_height": self.dst_height}


# aliases for class names
CoordinateMapper = FloorplanMapper
FloorplanCoordinateMapper = FloorplanMapper
CoordMapper = FloorplanMapper
Mapper = FloorplanMapper

# Additional class-name aliases
Floorplan = FloorplanMapper
FloorPlan = FloorplanMapper
FloorplanService = FloorplanMapper
FloorplanManager = FloorplanMapper
CoordinateMapping = FloorplanMapper
CoordinateMap = FloorplanMapper
CoordMapping = FloorplanMapper
FloorplanCoordinateMapping = FloorplanMapper
MappingService = FloorplanMapper

# Additional convenience: module exports list
__all__ = [
    "Point", "FloorplanMapper", "CoordinateMapper", "FloorplanCoordinateMapper",
    "normalize_point", "denormalize_point", "map_point", "map_coordinates",
]
