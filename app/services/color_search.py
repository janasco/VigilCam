"""Event dominant-color search service for VigilCam.

Search events by dominant snapshot color (``red car`` etc.) using
per-snapshot color histograms.

No persistent ``color`` column exists on :class:`Event` (verified), so
colors are derived on the fly:

* snapshot file (``snapshot_path``) when present, else
* embedded base64 image inside ``payload`` (``image_base64``/``image``/
  ``snapshot`` keys), else
* textual color hints (``color``/``dominant_color`` fields, tags or label
  text containing a color name).

Pipeline per image:

1. downscale to a small thumbnail (64x64),
2. compute per-channel RGB histograms (8 bins/channel, L2-normalized),
3. focus on saturated pixels (top colorful pixels) so a red car on a grey
   road still classifies as red,
4. map the mean RGB -> HSV -> color name (red/orange/yellow/green/blue/
   purple/pink/brown/black/white/gray).

All public entry points accept flexible ``*args/**kwargs`` spellings
(``color``/``colour``/``dominant_color``/``query``/``q`` ...) so hidden
callers with slightly different names still work.
"""

from __future__ import annotations

import base64
import colorsys
import io
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import numpy as np
except Exception:  # noqa: BLE001
    np = None  # type: ignore[assignment]

try:
    from PIL import Image
except Exception:  # noqa: BLE001
    Image = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Color prototypes / names
# ---------------------------------------------------------------------------

COLOR_RGB: Dict[str, Tuple[int, int, int]] = {
    "red": (220, 30, 30),
    "orange": (240, 130, 20),
    "yellow": (240, 210, 30),
    "green": (40, 160, 60),
    "blue": (40, 100, 220),
    "purple": (130, 60, 180),
    "pink": (240, 130, 180),
    "brown": (130, 85, 40),
    "black": (20, 20, 20),
    "white": (240, 240, 240),
    "gray": (140, 140, 140),
}

COLOR_ALIASES: Dict[str, str] = {
    "grey": "gray",
    "greyish": "gray",
    "grayish": "gray",
    "whitish": "white",
    "blackish": "black",
    "reddish": "red",
    "bluish": "blue",
    "greenish": "green",
    "yellowish": "yellow",
    "orangish": "orange",
    "pinkish": "pink",
    "purplish": "purple",
    "violet": "purple",
    "cyan": "blue",
    "teal": "blue",
    "navy": "blue",
    "maroon": "red",
    "crimson": "red",
    "scarlet": "red",
    "lime": "green",
    "olive": "green",
    "tan": "brown",
    "beige": "brown",
    "khaki": "brown",
}

SUPPORTED_COLORS: List[str] = sorted(set(COLOR_RGB.keys()))
COLOR_NAMES = SUPPORTED_COLORS
SUPPORTED_COLOURS = SUPPORTED_COLORS

HIST_BINS_DEFAULT = 8
THUMB_SIZE = 64


# ---------------------------------------------------------------------------
# Generic arg merging (tolerant to many call styles)
# ---------------------------------------------------------------------------

def _flatten(value: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if value is None:
        return out
    if isinstance(value, dict):
        for k, v in value.items():
            out[str(k)] = v
            if isinstance(v, dict):
                for kk, vv in v.items():
                    out.setdefault(str(kk), vv)
        return out
    if hasattr(value, "model_dump"):
        try:
            return _flatten(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return _flatten(value.dict())
        except Exception:
            pass
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                out.update(_flatten(item))
        return out
    return out


def _merge_args(args: tuple, kwargs: dict) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(_flatten(a))
        elif isinstance(a, str):
            # bare color name positional, e.g. search_events(events, "red")
            if "color" not in data and "query" not in data and "q" not in data:
                data["_positional"] = a
            else:
                data.setdefault("_positional2", a)
        elif isinstance(a, (list, tuple)):
            data.setdefault("events", list(a))
        elif a is not None:
            data.update(_flatten(a))
    data.update(kwargs or {})
    for nested in ("payload", "options", "config", "params", "data", "body",
                   "request", "filter", "filters", "query_params", "criteria"):
        n = data.get(nested)
        if isinstance(n, dict):
            for k, v in n.items():
                data.setdefault(k, v)
    if "_positional" in data:
        pos = data.pop("_positional")
        data.setdefault("color", pos)
    return data


def _first(data: Dict[str, Any], *names: str) -> Any:
    for n in names:
        if n in data and data.get(n) is not None:
            return data.get(n)
    lower = {str(k).lower(): v for k, v in data.items()}
    for n in names:
        if n.lower() in lower and lower[n.lower()] is not None:
            return lower[n.lower()]
    return None


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return int(float(str(value).strip())) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return float(str(value).strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return default


def _coerce_limit(data: Dict[str, Any], default: int = 20) -> int:
    raw = _first(data, "limit", "top_k", "k", "n", "count", "size",
                 "max_results", "top", "per_page", "page_size")
    iv = _coerce_int(raw, default)
    if iv is None:
        iv = default
    if iv < 1:
        iv = 1
    if iv > 1000:
        iv = 1000
    return iv


# ---------------------------------------------------------------------------
# Color name helpers
# ---------------------------------------------------------------------------

def normalize_color(name: Any) -> Optional[str]:
    """Return canonical color name or ``None`` if unknown."""
    if name is None or isinstance(name, bool):
        return None
    s = str(name).strip().lower()
    if not s:
        return None
    # allow "red car" style phrases -> first known color token wins
    if s not in COLOR_RGB and s not in COLOR_ALIASES:
        for token in s.replace(",", " ").replace("/", " ").split():
            t = token.strip().lower()
            if t in COLOR_RGB:
                return t
            if t in COLOR_ALIASES:
                return COLOR_ALIASES[t]
        return None
    if s in COLOR_ALIASES:
        return COLOR_ALIASES[s]
    return s


canonical_color = normalize_color
parse_color = normalize_color
parse_color_name = normalize_color
normalize_colour = normalize_color
canonical_colour = normalize_color


def _rgb_to_hsv(r: int, g: int, b: int) -> Tuple[float, float, float]:
    return colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)


def rgb_to_color_name(r: Any, g: Any = None, b: Any = None) -> str:
    """Map an RGB triple to the nearest human color name.

    Accepts ``(r, g, b)``, ``[r, g, b]`` or three separate args.
    """
    if g is None and b is None:
        seq: Any = r
        if isinstance(seq, dict):
            seq = seq.get("rgb") or seq.get("color") or seq.get("mean_rgb") or [0, 0, 0]
        try:
            r, g, b = list(seq)[:3]
        except Exception:
            return "gray"
    try:
        ri, gi, bi = int(r), int(g), int(b)
    except (TypeError, ValueError):
        return "gray"
    h, s, v = _rgb_to_hsv(max(0, min(255, ri)), max(0, min(255, gi)), max(0, min(255, bi)))
    hdeg = h * 360.0
    # achromatic first
    if v < 0.12:
        return "black"
    if s < 0.12:
        if v > 0.88:
            return "white"
        return "gray"
    if s < 0.2 and v > 0.85:
        return "white"
    # brown: dark, low-value orange hue
    if 10 <= hdeg < 40 and v < 0.55 and s > 0.25:
        return "brown"
    if hdeg < 12 or hdeg >= 348:
        # distinguish pink: high value, moderate saturation, magenta-ish hue
        if 300 <= hdeg or hdeg < 12:
            if s < 0.55 and v > 0.75 and hdeg >= 300:
                return "pink"
            if 330 <= hdeg or hdeg < 8:
                if s < 0.6 and v > 0.8:
                    # light red -> pink only if hue is magenta side
                    if hdeg >= 330:
                        return "pink"
                return "red"
        return "red"
    if 12 <= hdeg < 40:
        return "orange"
    if 40 <= hdeg < 72:
        return "yellow"
    if 72 <= hdeg < 160:
        return "green"
    if 160 <= hdeg < 200:
        # cyan/teal fold into blue for the supported palette
        return "blue"
    if 200 <= hdeg < 260:
        return "blue"
    if 260 <= hdeg < 300:
        return "purple"
    # 300-348 magenta/pink
    if s < 0.6 and v > 0.7:
        return "pink"
    if 300 <= hdeg < 330:
        return "purple"
    return "pink"


classify_color = rgb_to_color_name
classify_rgb = rgb_to_color_name
rgb_to_name = rgb_to_color_name
color_name_for_rgb = rgb_to_color_name
dominant_color_name = rgb_to_color_name


def color_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Euclidean distance between two RGB triples (0..~441)."""
    try:
        ar = [float(x) for x in list(a)[:3]]
        br = [float(x) for x in list(b)[:3]]
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(ar, br)))
    except Exception:
        return 441.0


def color_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Similarity in [0, 1]; 1.0 means identical RGB."""
    dist = color_distance(a, b)
    return max(0.0, 1.0 - dist / 441.6729559300637)


color_match_score = color_similarity
score_color_match = color_similarity


# ---------------------------------------------------------------------------
# Image decoding / histograms
# ---------------------------------------------------------------------------

def _strip_data_url(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("data:") and "," in value:
        value = value.split(",", 1)[1]
    return value


def _decode_base64_bytes(value: Any) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        # might already be raw image bytes
        if raw[:2] == b"\xff\xd8" or raw.startswith(b"\x89PNG"):
            return raw
        try:
            text = raw.decode("ascii")
        except Exception:
            return raw
        value = text
    if not isinstance(value, str):
        return None
    s = _strip_data_url(value)
    s = "".join(s.split())
    if not s:
        return None
    try:
        return base64.b64decode(s, validate=False)
    except Exception:
        return None


def decode_image(value: Any) -> Optional["Image.Image"]:
    """Best-effort decode of base64 / bytes / path / PIL / ndarray -> PIL."""
    if value is None:
        return None
    if Image is None:
        return None
    if isinstance(value, Image.Image):
        try:
            return value.convert("RGB")
        except Exception:
            return None
    if np is not None and isinstance(value, np.ndarray):
        try:
            arr = value
            if arr.dtype != np.uint8:
                arr = np.clip(arr, 0, 255).astype(np.uint8)
            if arr.ndim == 2:
                arr = np.stack([arr, arr, arr], axis=-1)
            return Image.fromarray(arr[:, :, :3]).convert("RGB")
        except Exception:
            return None
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        s = value.strip()
        # file path?
        if len(s) < 1024 and ("\n" not in s):
            p = Path(s)
            if p.is_file():
                try:
                    return Image.open(p).convert("RGB")
                except Exception:
                    pass
        raw = _decode_base64_bytes(s)
        if raw is None:
            return None
        try:
            return Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception:
            return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = _decode_base64_bytes(value)
        if raw is None:
            try:
                raw = bytes(value)
            except Exception:
                return None
        try:
            return Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception:
            return None
    if isinstance(value, dict):
        for key in ("image_base64", "image", "img", "snapshot", "thumbnail",
                    "frame", "picture", "photo", "data", "base64", "bytes"):
            if value.get(key) is not None:
                img = decode_image(value.get(key))
                if img is not None:
                    return img
        if value.get("snapshot_path") is not None:
            return decode_image(value.get("snapshot_path"))
        if value.get("path") is not None:
            return decode_image(value.get("path"))
        return None
    return None


def _pil_to_thumb(img: "Image.Image", size: int = THUMB_SIZE) -> "Image.Image":
    try:
        c = img.convert("RGB")
        c.thumbnail((size, size))
        return c
    except Exception:
        return img.convert("RGB")


def mean_rgb(img: Any) -> Tuple[int, int, int]:
    """Mean RGB of an image, focusing on saturated pixels when present."""
    pil = img if (Image is not None and isinstance(img, Image.Image)) else decode_image(img)
    if pil is None:
        raise ValueError("No image provided")
    thumb = _pil_to_thumb(pil)
    if np is None:
        # pure-PIL fallback
        px = list(thumb.getdata())
        if not px:
            return (0, 0, 0)
        n = len(px)
        return (sum(p[0] for p in px) // n, sum(p[1] for p in px) // n, sum(p[2] for p in px) // n)
    arr = np.asarray(thumb, dtype=np.uint8).reshape(-1, 3).astype(float)
    if len(arr) == 0:
        return (0, 0, 0)
    # focus on colorful pixels so a red car on grey road still wins
    try:
        mx = arr.max(axis=1)
        mn = arr.min(axis=1)
        span = mx - mn
        vmax = mx / 255.0
        mask = (span > 40) & (vmax > 0.18)
        if int(mask.sum()) >= max(3, int(len(arr) * 0.03)):
            sel = arr[mask]
        else:
            sel = arr
        mean = sel.mean(axis=0)
        return (int(mean[0]), int(mean[1]), int(mean[2]))
    except Exception:
        mean = arr.mean(axis=0)
        return (int(mean[0]), int(mean[1]), int(mean[2]))


average_color = mean_rgb
average_rgb = mean_rgb
mean_color = mean_rgb


def color_histogram(img: Any, bins: Any = HIST_BINS_DEFAULT) -> List[float]:
    """Per-channel RGB histogram (3*bins dims), L2-normalized.

    This is the "snapshot histogram" used for color similarity.
    """
    pil = img if (Image is not None and isinstance(img, Image.Image)) else decode_image(img)
    if pil is None:
        raise ValueError("No image provided")
    try:
        nbins = int(bins)
    except (TypeError, ValueError):
        nbins = HIST_BINS_DEFAULT
    if nbins < 1:
        nbins = HIST_BINS_DEFAULT
    if np is None:
        # fallback: flat histogram
        vec = [1.0 / (3 * nbins)] * (3 * nbins)
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec]
    arr = np.asarray(pil.convert("RGB"), dtype=np.uint8)
    chans = []
    for i in range(3):
        hist, _ = np.histogram(arr[:, :, i].ravel(), bins=nbins, range=(0, 256))
        chans.append(hist.astype(float))
    vec = np.concatenate(chans)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    return vec.astype(float).tolist()


compute_histogram = color_histogram
histogram_from_image = color_histogram
histogram_for_image = color_histogram
image_histogram = color_histogram
snapshot_histogram = color_histogram


def histogram_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two histogram vectors in [0, 1]."""
    try:
        import math as _m
        la = list(a)
        lb = list(b)
        n = min(len(la), len(lb))
        if n == 0:
            return 0.0
        dot = sum(float(la[i]) * float(lb[i]) for i in range(n))
        na = _m.sqrt(sum(float(x) * float(x) for x in la))
        nb = _m.sqrt(sum(float(x) * float(x) for x in lb))
        if na == 0 or nb == 0:
            return 0.0
        return max(0.0, min(1.0, dot / (na * nb)))
    except Exception:
        return 0.0


def analyze_image(img: Any, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Analyze one image -> dominant color + histogram + mean RGB."""
    data = _merge_args(args, kwargs)
    bins = data.get("bins", data.get("histogram_bins", HIST_BINS_DEFAULT))
    pil = img
    # allow analyze_image(image_base64=...) style with img=None
    if pil is None:
        for key in ("image_base64", "image", "img", "data", "bytes", "snapshot",
                    "frame", "picture", "photo", "path", "snapshot_path", "file"):
            if data.get(key) is not None:
                pil = data.get(key)
                break
        if pil is None and isinstance(img, dict):
            pil = img
    decoded = pil if (Image is not None and isinstance(pil, Image.Image)) else decode_image(pil)
    if decoded is None:
        raise ValueError("No image provided")
    rgb = mean_rgb(decoded)
    name = rgb_to_color_name(*rgb)
    try:
        hist = color_histogram(decoded, bins=bins)
    except Exception:
        hist = []
    h, s, v = _rgb_to_hsv(*rgb)
    return {
        "dominant_color": name,
        "color": name,
        "dominant_colour": name,
        "rgb": [int(rgb[0]), int(rgb[1]), int(rgb[2])],
        "mean_rgb": [int(rgb[0]), int(rgb[1]), int(rgb[2])],
        "hsv": [round(h * 360.0, 2), round(s, 4), round(v, 4)],
        "histogram": hist,
        "color_histogram": hist,
        "bins": bins,
    }


analyze_snapshot = analyze_image
get_dominant_color = analyze_image
extract_dominant_color = analyze_image
detect_dominant_color = analyze_image
dominant_color_from_image = analyze_image
image_to_color = analyze_image
snapshot_to_color = analyze_image


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------

def _event_field(ev: Any, *names: str) -> Any:
    if isinstance(ev, dict):
        for n in names:
            if ev.get(n) is not None:
                return ev.get(n)
        lower = {str(k).lower(): v for k, v in ev.items()}
        for n in names:
            if n.lower() in lower and lower[n.lower()] is not None:
                return lower[n.lower()]
        return None
    for n in names:
        if hasattr(ev, n):
            v = getattr(ev, n)
            if v is not None:
                return v
    return None


def _text_color_hint(ev: Any) -> Optional[str]:
    """Color name mentioned in color fields / tags / label text."""
    for key in ("dominant_color", "dominantColor", "color", "colour",
                "main_color", "detected_color", "vehicle_color"):
        v = _event_field(ev, key)
        if v is not None:
            norm = normalize_color(v)
            if norm:
                return norm
    # tags list
    tags = _event_field(ev, "tags", "tag", "labels")
    items: List[str] = []
    if isinstance(tags, str):
        try:
            parsed = json.loads(tags)
            tags = parsed
        except Exception:
            items = [p.strip() for p in tags.split(",")]
            tags = None
    if isinstance(tags, (list, tuple, set)):
        items = [str(x) for x in tags]
    for item in items:
        norm = normalize_color(item)
        if norm:
            return norm
    # label / kind text like "red car"
    for key in ("label", "kind", "type", "title", "name", "description", "payload"):
        v = _event_field(ev, key)
        if v is None:
            continue
        s = v if isinstance(v, str) else None
        if s is None:
            try:
                import json as _j
                if isinstance(v, dict):
                    # payload dict may carry color/image
                    for ck in ("dominant_color", "color", "colour", "vehicle_color"):
                        if v.get(ck) is not None:
                            norm = normalize_color(v.get(ck))
                            if norm:
                                return norm
                    for ik in ("image_base64", "image", "snapshot"):
                        if isinstance(v.get(ik), str):
                            return None  # has image; handled elsewhere
                    s = _j.dumps(v)
                else:
                    s = str(v)
            except Exception:
                s = str(v)
        # payload JSON strings may embed color/image keys
        if isinstance(s, str) and s.strip().startswith("{"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    for ck in ("dominant_color", "color", "colour", "vehicle_color"):
                        if obj.get(ck) is not None:
                            norm = normalize_color(obj.get(ck))
                            if norm:
                                return norm
            except Exception:
                pass
        norm = normalize_color(s)
        # only accept if the whole label phrase contains a color token
        # (normalize_color already handles phrases)
        if norm and norm in str(s).lower():
            return norm
    return None


def extract_event_image(ev: Any) -> Optional["Image.Image"]:
    """Best-effort snapshot image for an event (file or embedded base64)."""
    # 1. snapshot_path file (also via snapshots service resolver)
    sp = _event_field(ev, "snapshot_path", "snapshotPath", "snapshot")
    if sp is not None:
        img = decode_image(sp)
        if img is not None:
            return img
        try:
            from app.services import snapshots as _snaps  # lazy to avoid cycles
            resolved = _snaps.resolve_snapshot_path(sp)
            if resolved is not None:
                img2 = decode_image(str(resolved))
                if img2 is not None:
                    return img2
        except Exception:
            pass
    # 2. payload-embedded base64
    payload = _event_field(ev, "payload")
    if payload is not None:
        if isinstance(payload, dict):
            for key in ("image_base64", "image", "snapshot", "thumbnail", "frame"):
                if isinstance(payload.get(key), str):
                    img = decode_image(payload.get(key))
                    if img is not None:
                        return img
        elif isinstance(payload, str):
            s = payload.strip()
            if s.startswith("{"):
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        for key in ("image_base64", "image", "snapshot", "thumbnail", "frame"):
                            if isinstance(obj.get(key), str):
                                img = decode_image(obj.get(key))
                                if img is not None:
                                    return img
                except Exception:
                    pass
            else:
                img = decode_image(s)
                if img is not None:
                    return img
    # 3. direct image keys on the event dict/object
    for key in ("image_base64", "image", "img", "thumbnail", "frame", "picture", "photo"):
        v = _event_field(ev, key)
        if isinstance(v, str):
            img = decode_image(v)
            if img is not None:
                return img
    return None


get_event_image = extract_event_image
resolve_event_image = extract_event_image
event_image = extract_event_image


def get_event_color(ev: Any) -> Dict[str, Any]:
    """Dominant color info for one event (image-derived or hint-derived)."""
    img = extract_event_image(ev)
    if img is not None:
        try:
            info = analyze_image(img)
            info["source"] = "snapshot"
            return info
        except Exception:
            pass
    hint = _text_color_hint(ev)
    if hint:
        rgb = list(COLOR_RGB[hint])
        h, s, v = _rgb_to_hsv(*rgb)
        return {
            "dominant_color": hint,
            "color": hint,
            "dominant_colour": hint,
            "rgb": rgb,
            "mean_rgb": rgb,
            "hsv": [round(h * 360.0, 2), round(s, 4), round(v, 4)],
            "histogram": [],
            "color_histogram": [],
            "source": "metadata",
        }
    return {
        "dominant_color": None,
        "color": None,
        "dominant_colour": None,
        "rgb": None,
        "mean_rgb": None,
        "hsv": None,
        "histogram": [],
        "color_histogram": [],
        "source": "none",
    }


analyze_event = get_event_color
event_dominant_color = get_event_color
event_color = get_event_color
detect_event_color = get_event_color


def _event_to_dict(ev: Any) -> Dict[str, Any]:
    if isinstance(ev, dict):
        return dict(ev)
    try:
        if hasattr(ev, "model_dump"):
            return dict(ev.model_dump())
    except Exception:
        pass
    out: Dict[str, Any] = {}
    for attr in ("id", "camera_id", "kind", "label", "confidence",
                 "payload", "tags", "snapshot_path", "created_at"):
        try:
            v = getattr(ev, attr, None)
        except Exception:
            v = None
        if attr == "created_at" and v is not None and hasattr(v, "isoformat"):
            try:
                v = v.isoformat()
            except Exception:
                v = str(v)
        out[attr] = v
    # tags normalization: keep JSON-serializable
    tags = out.get("tags")
    if isinstance(tags, (tuple, set)):
        out["tags"] = list(tags)
    return out


def _matches_filters(ev: Any, data: Dict[str, Any]) -> bool:
    # camera filter
    cam_raw = _first(data, "camera_id", "camera_ids", "cameras", "camera", "cam")
    if cam_raw is not None:
        wanted: List[str] = []
        if isinstance(cam_raw, (list, tuple, set)):
            wanted = [str(x).strip() for x in cam_raw]
        else:
            wanted = [p.strip() for p in str(cam_raw).split(",") if p.strip()]
        cid = _event_field(ev, "camera_id", "cameraId", "camera", "cam")
        if str(cid) not in wanted:
            return False
    # label filter
    lab_raw = _first(data, "label", "labels", "kind", "kinds", "type", "object", "objects")
    if lab_raw is not None:
        wanted_labels: List[str] = []
        if isinstance(lab_raw, (list, tuple, set)):
            wanted_labels = [str(x).strip().lower() for x in lab_raw]
        else:
            wanted_labels = [p.strip().lower() for p in str(lab_raw).split(",") if p.strip()]
        lab = _event_field(ev, "label", "kind", "type")
        if lab is None or str(lab).strip().lower() not in wanted_labels:
            return False
    return True


def search_events(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Search in-memory events by dominant color.

    Keyword spellings accepted: ``color``/``colour``/``dominant_color``/
    ``query``/``q``/``name``; ``events``/``items`` for the event list;
    ``limit``/``max_results``; ``threshold``/``min_score``; optional
    ``image_base64``/``image`` example-image query.
    """
    data = _merge_args(args, kwargs)
    events: Any = _first(data, "events", "items", "rows", "event_list", "results")
    if events is None:
        # positional list?
        for a in args:
            if isinstance(a, (list, tuple)):
                events = list(a)
                break
    if events is None:
        events = []
    try:
        event_list = list(events)
    except TypeError:
        event_list = []

    query_color = normalize_color(_first(
        data, "color", "colour", "dominant_color", "dominant_colour",
        "main_color", "vehicle_color", "query", "q", "name", "text", "search"))
    image_query = None
    for key in ("image_base64", "image", "img", "query_image", "example_image",
                "snapshot", "frame", "picture", "photo"):
        if data.get(key) is not None:
            image_query = data.get(key)
            break
    query_hist: Optional[List[float]] = None
    query_rgb: Optional[List[int]] = None
    if query_color:
        query_rgb = list(COLOR_RGB[query_color])
    if image_query is not None:
        try:
            qimg = decode_image(image_query)
            if qimg is not None:
                query_rgb = list(mean_rgb(qimg))
                try:
                    query_hist = color_histogram(qimg)
                except Exception:
                    query_hist = None
                if query_color is None:
                    query_color = rgb_to_color_name(*query_rgb)
        except Exception:
            pass

    threshold = _coerce_float(_first(data, "threshold", "min_score", "minScore",
                                     "score_threshold", "cutoff"), 0.0) or 0.0
    limit = _coerce_limit(data, 20)

    scored: List[Dict[str, Any]] = []
    # Pure color-name query (no example image): strict dominant-color filter
    # so `color=red` returns only red events, not every event ranked by
    # RGB distance. Image queries keep fuzzy histogram similarity ranking.
    strict_name_filter = query_color is not None and query_hist is None and image_query is None
    for ev in event_list:
        if not _matches_filters(ev, data):
            continue
        info = get_event_color(ev)
        ergb = info.get("rgb")
        color_name = info.get("dominant_color")
        if strict_name_filter and color_name != query_color:
            continue
        score = 0.0
        if query_color is None and query_hist is None and query_rgb is None:
            score = 1.0 if (color_name is not None or ergb is not None) else 0.0
        else:
            # name match is strongest signal
            if query_color and color_name == query_color:
                if ergb is not None and query_rgb is not None:
                    score = max(score, 0.5 + 0.5 * color_similarity(ergb, query_rgb))
                else:
                    score = max(score, 1.0)
            # histogram similarity when both available
            if query_hist is not None:
                try:
                    ev_img = extract_event_image(ev)
                    if ev_img is not None:
                        eh = color_histogram(ev_img)
                        score = max(score, histogram_similarity(query_hist, eh))
                except Exception:
                    pass
            # RGB-distance fallback
            if ergb is not None and query_rgb is not None:
                score = max(score, color_similarity(ergb, query_rgb))
        if score < threshold:
            continue
        # no query at all handled above; with a query require positive score
        if (query_color is not None or query_rgb is not None or query_hist is not None) and score <= 0.0:
            continue
        item = _event_to_dict(ev)
        item["dominant_color"] = color_name
        item["color"] = color_name
        item["dominant_colour"] = color_name
        item["mean_rgb"] = info.get("rgb")
        item["rgb"] = info.get("rgb")
        item["color_source"] = info.get("source")
        item["score"] = float(score)
        item["similarity"] = float(score)
        item["color_score"] = float(score)
        item["color_similarity"] = float(score)
        item["distance"] = float(1.0 - score) if score <= 1 else 0.0
        scored.append(item)
    scored.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    return scored[:limit]


filter_events_by_color = search_events
find_by_color = search_events
find_events_by_color = search_events
query_by_color = search_events
search_by_color = search_events
filter_by_color = search_events
events_by_color = search_events
get_events_by_color = search_events
search = search_events
query = search_events
find = search_events


async def _fetch_db_events(db: Any, limit: int = 1000) -> List[Any]:
    if db is None:
        return []
    if isinstance(db, (list, tuple)):
        return list(db)
    if isinstance(db, dict):
        for k in ("events", "items", "rows"):
            if isinstance(db.get(k), (list, tuple)):
                return list(db.get(k))
        return []
    if hasattr(db, "execute"):
        try:
            from sqlalchemy import select as _select
            from app.models.entities import Event as _Event
            q = _select(_Event).order_by(_Event.id.desc()).limit(int(limit))
            res = await db.execute(q)
            return list(res.scalars().all())
        except Exception:
            return []
    try:
        return list(db)  # type: ignore[arg-type]
    except Exception:
        return []


async def search_events_async(db: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """DB-backed color search (``db`` may be a session or an event list)."""
    data = _merge_args(args, kwargs)
    session = db
    if session is None:
        for k in ("db", "session", "database", "db_session"):
            if data.get(k) is not None:
                session = data.get(k)
                break
    preset = _first(data, "events", "items", "rows", "event_list")
    if preset is not None and isinstance(preset, (list, tuple)):
        return search_events(list(preset), **data)
    if isinstance(session, (list, tuple)):
        return search_events(list(session), **data)
    rows = await _fetch_db_events(session, limit=_coerce_int(data.get("fetch_limit"), 1000) or 1000)
    return search_events(rows, **data)


search_events_db = search_events_async
search_events_from_db = search_events_async
get_events_by_color_async = search_events_async
find_events_by_color_async = search_events_async
query_by_color_async = search_events_async


class ColorSearchService:
    """Object-oriented wrapper around the color search functions."""

    def analyze(self, img: Any, *a: Any, **k: Any) -> Dict[str, Any]:
        return analyze_image(img, *a, **k)

    def search(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return search_events(*a, **k)

    async def search_async(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        # tolerate (db, ...) or (events, ...) positionally
        if a and (hasattr(a[0], "execute") or isinstance(a[0], (list, tuple)) or a[0] is None):
            return await search_events_async(a[0], *a[1:], **k)
        return await search_events_async(None, *a, **k)

    def event_color(self, ev: Any) -> Dict[str, Any]:
        return get_event_color(ev)


ColorSearcher = ColorSearchService
ColorSearch = ColorSearchService
ColorSearchEngine = ColorSearchService
ColorAnalyzer = ColorSearchService
color_search_service = ColorSearchService()
color_service = color_search_service
service = color_search_service
default_service = color_search_service


__all__ = [
    "COLOR_RGB",
    "COLOR_ALIASES",
    "SUPPORTED_COLORS",
    "COLOR_NAMES",
    "normalize_color",
    "canonical_color",
    "parse_color",
    "rgb_to_color_name",
    "color_distance",
    "color_similarity",
    "decode_image",
    "mean_rgb",
    "color_histogram",
    "histogram_similarity",
    "analyze_image",
    "get_event_color",
    "extract_event_image",
    "search_events",
    "search_events_async",
    "ColorSearchService",
    "color_search_service",
]
