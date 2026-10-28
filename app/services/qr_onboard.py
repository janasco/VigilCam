"""Camera QR onboarding codes service.

Builds small self-contained onboarding payloads for cameras and renders
them as real, scannable QR Code PNGs using only the standard library plus
Pillow (already a VigilCam dependency).

No third-party QR library (``qrcode``/``segno``) is used: this module
contains a compact, original QR Model 2 encoder (byte mode, versions 1-40,
EC levels L/M/Q/H) with Reed-Solomon ECC, function patterns, masking and
format/version info. Tables of codeword counts are facts from ISO/IEC 18004.

Public surface (stable):
- build_onboarding_payload(...) -> dict
- encode_onboarding_text(payload) -> str
- decode_onboarding_text(code) -> dict
- encode_qr_matrix(text, ec="M", mask=0) -> dict
- render_qr_png(text, ...) -> bytes
- qr_data_url(text, ...) -> str
- create_onboarding_code(...) -> dict (stored, expiring)
- verify_onboarding_code(...) / get / list / revoke / clear / get_status
- generate_camera_qr(...) -> dict (stateless PNG + metadata)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

CONTENT_TYPE_PNG = "image/png"
CODE_PREFIX_V1 = "VIGILCAM1:"
CODE_PREFIX_LEGACY = "VIGILCAM:"
DEFAULT_EC_LEVEL = "M"
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
MAX_TTL_SECONDS = 90 * 24 * 3600

_MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

# ---------------------------------------------------------------------------
# QR Model 2 tables (facts from ISO/IEC 18004; index 0 is padding).
# Rows are EC levels L/M/Q/H, columns are versions 1..40.
# ---------------------------------------------------------------------------

_ECC_PER_BLOCK: Tuple[Tuple[int, ...], ...] = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30,
     28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26,
     26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28,
     26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28,
     26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)

_NUM_BLOCKS: Tuple[Tuple[int, ...], ...] = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10,
     12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17,
     18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23,
     23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25,
     34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)

_ECL_ORDER = {"L": 0, "M": 1, "Q": 2, "H": 3}
_FORMAT_BITS = (1, 0, 3, 2)  # indexed by ordinal L/M/Q/H


def _ecl_ordinal(level: Any) -> int:
    s = str(level if level is not None else DEFAULT_EC_LEVEL).strip().upper()
    return _ECL_ORDER.get(s, 1)


def _ecl_name(ordinal: int) -> str:
    for k, v in _ECL_ORDER.items():
        if v == ordinal:
            return k
    return "M"


# ---------------------------------------------------------------------------
# QR low-level helpers
# ---------------------------------------------------------------------------

def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree: int) -> bytes:
    if not 1 <= degree <= 255:
        raise ValueError("degree out of range")
    result = bytearray([0] * (degree - 1) + [1])
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return bytes(result)


def _rs_remainder(data: bytes, divisor: bytes) -> bytes:
    result = bytearray([0] * len(divisor))
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return bytes(result)


def _raw_data_modules(version: int) -> int:
    if not 1 <= version <= 40:
        raise ValueError("version out of range")
    result = (16 * version + 128) * version + 64
    if version >= 2:
        num_align = version // 7 + 2
        result -= (25 * num_align - 10) * num_align - 55
        if version >= 7:
            result -= 36
    return result


def _num_data_codewords(version: int, ecl_ord: int) -> int:
    return _raw_data_modules(version) // 8 - _ECC_PER_BLOCK[ecl_ord][version] * _NUM_BLOCKS[ecl_ord][version]


def _alignment_positions(version: int) -> List[int]:
    if version == 1:
        return []
    size = version * 4 + 17
    num_align = version // 7 + 2
    step = ((version * 8 + num_align * 3 + 5) // (num_align * 4 - 4)) * 2
    positions = [size - 7 - i * step for i in range(num_align - 1)] + [6]
    return sorted(positions)


def _char_count_bits(version: int) -> int:
    return 8 if version <= 9 else 16


def _encode_data_codewords(data: bytes, version: int, ecl_ord: int) -> bytes:
    capacity = _num_data_codewords(version, ecl_ord)
    max_bits = capacity * 8
    bits: List[int] = [0, 1, 0, 0]  # byte mode indicator 0100
    ccbits = _char_count_bits(version)
    n = len(data)
    for i in range(ccbits - 1, -1, -1):
        bits.append((n >> i) & 1)
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    if len(bits) > max_bits:
        raise ValueError("data too long for version")
    bits.extend([0] * min(4, max_bits - len(bits)))
    while len(bits) % 8 != 0:
        bits.append(0)
    out = bytearray()
    for i in range(0, len(bits), 8):
        val = 0
        for b in bits[i:i + 8]:
            val = (val << 1) | b
        out.append(val)
    pad = 0
    while len(out) < capacity:
        out.append(0xEC if pad % 2 == 0 else 0x11)
        pad += 1
    return bytes(out)


def _choose_version(data_len: int, ecl_ord: int) -> int:
    for version in range(1, 41):
        ccbits = _char_count_bits(version)
        needed = 4 + ccbits + data_len * 8
        if needed <= _num_data_codewords(version, ecl_ord) * 8:
            return version
    raise ValueError("data too long for any QR version")


def _mask_func(mask: int):
    if mask == 0:
        return lambda x, y: (x + y) % 2
    if mask == 1:
        return lambda x, y: y % 2
    if mask == 2:
        return lambda x, y: x % 3
    if mask == 3:
        return lambda x, y: (x + y) % 3
    if mask == 4:
        return lambda x, y: (x // 3 + y // 2) % 2
    if mask == 5:
        return lambda x, y: (x * y) % 2 + (x * y) % 3
    if mask == 6:
        return lambda x, y: ((x * y) % 2 + (x * y) % 3) % 2
    if mask == 7:
        return lambda x, y: ((x + y) % 2 + (x * y) % 3) % 2
    raise ValueError("mask out of range")


def _build_matrix(data_cw: bytes, version: int, ecl_ord: int, mask: int) -> List[List[bool]]:
    size = version * 4 + 17
    modules: List[List[bool]] = [[False] * size for _ in range(size)]
    is_func: List[List[bool]] = [[False] * size for _ in range(size)]

    def set_func(x: int, y: int, dark: bool) -> None:
        modules[y][x] = bool(dark)
        is_func[y][x] = True

    for i in range(size):
        dark = (i % 2 == 0)
        set_func(6, i, dark)
        set_func(i, 6, dark)

    def finder(cx: int, cy: int) -> None:
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                xx, yy = cx + dx, cy + dy
                if 0 <= xx < size and 0 <= yy < size:
                    set_func(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))

    finder(3, 3)
    finder(size - 4, 3)
    finder(3, size - 4)

    pos = _alignment_positions(version)
    npos = len(pos)
    skips = {(0, 0), (0, npos - 1), (npos - 1, 0)} if npos else set()
    for i in range(npos):
        for j in range(npos):
            if (i, j) in skips:
                continue
            cx, cy = pos[i], pos[j]
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    set_func(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(m: int) -> None:
        data = (_FORMAT_BITS[ecl_ord] << 3) | m
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412

        def bit(k: int) -> bool:
            return ((bits >> k) & 1) != 0

        for k in range(6):
            set_func(8, k, bit(k))
        set_func(8, 7, bit(6))
        set_func(8, 8, bit(7))
        set_func(7, 8, bit(8))
        for k in range(9, 15):
            set_func(14 - k, 8, bit(k))
        for k in range(8):
            set_func(size - 1 - k, 8, bit(k))
        for k in range(8, 15):
            set_func(8, size - 15 + k, bit(k))
        set_func(8, size - 8, True)

    draw_format(mask)

    if version >= 7:
        rem_v = version
        for _ in range(12):
            rem_v = (rem_v << 1) ^ ((rem_v >> 11) * 0x1F25)
        vbits = (version << 12) | rem_v
        for k in range(18):
            b = ((vbits >> k) & 1) != 0
            a = size - 11 + k % 3
            c = k // 3
            set_func(a, c, b)
            set_func(c, a, b)

    num_blocks = _NUM_BLOCKS[ecl_ord][version]
    ec_len = _ECC_PER_BLOCK[ecl_ord][version]
    raw_cw = _raw_data_modules(version) // 8
    num_short = num_blocks - raw_cw % num_blocks
    short_len = raw_cw // num_blocks
    divisor = _rs_divisor(ec_len)
    blocks: List[bytes] = []
    k = 0
    for i in range(num_blocks):
        take = short_len - ec_len + (0 if i < num_short else 1)
        chunk = bytes(data_cw[k:k + take])
        k += len(chunk)
        ecc = _rs_remainder(chunk, divisor)
        if i < num_short:
            chunk = chunk + b"\x00"
        blocks.append(chunk + ecc)

    all_cw = bytearray()
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ec_len or j >= num_short:
                all_cw.append(blk[i])

    total_bits = len(all_cw) * 8
    idx = 0
    for right in range(size - 1, 0, -2):
        r = right - 1 if right <= 6 else right
        for vert in range(size):
            for j in range(2):
                x = r - j
                upward = (r + 1) & 2 == 0
                y = size - 1 - vert if upward else vert
                if not is_func[y][x] and idx < total_bits:
                    all_cw_byte = all_cw[idx >> 3]
                    modules[y][x] = ((all_cw_byte >> (7 - (idx & 7))) & 1) != 0
                    idx += 1

    mf = _mask_func(mask)
    for y in range(size):
        for x in range(size):
            if not is_func[y][x] and mf(x, y) == 0:
                modules[y][x] = not modules[y][x]

    draw_format(mask)
    return modules


def _simple_penalty(modules: List[List[bool]]) -> int:
    size = len(modules)
    score = 0
    for y in range(size - 1):
        row = modules[y]
        nxt = modules[y + 1]
        for x in range(size - 1):
            if row[x] == row[x + 1] == nxt[x] == nxt[x + 1]:
                score += 3
    dark = sum(1 for row in modules for c in row if c)
    total = size * size
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    if k < 0:
        k = 0
    return score + k * 10


# ---------------------------------------------------------------------------
# Public QR API
# ---------------------------------------------------------------------------

def encode_qr_matrix(text: Any, ec: Any = DEFAULT_EC_LEVEL, mask: Any = 0) -> Dict[str, Any]:
    """Encode ``text`` (str or bytes) as a QR matrix.

    Returns dict with ``matrix`` (list of rows of bool), ``version``,
    ``size``, ``mask`` and ``ec``. Byte mode is used; ``mask`` may be 0-7
    or -1/None for automatic selection via a lightweight penalty.
    """
    if isinstance(text, bytes):
        data = bytes(text)
    else:
        data = str(text if text is not None else "").encode("utf-8")
    if not data:
        raise ValueError("text is required")
    ecl_ord = _ecl_ordinal(ec)
    version = _choose_version(len(data), ecl_ord)
    data_cw = _encode_data_codewords(data, version, ecl_ord)
    use_mask: Optional[int]
    try:
        use_mask = int(mask) if mask is not None else 0
    except (TypeError, ValueError):
        use_mask = 0
    if use_mask is not None and use_mask < 0:
        best: Optional[List[List[bool]]] = None
        best_mask = 0
        best_score: Optional[int] = None
        for candidate in range(8):
            mat = _build_matrix(data_cw, version, ecl_ord, candidate)
            score = _simple_penalty(mat)
            if best_score is None or score < best_score:
                best_score = score
                best = mat
                best_mask = candidate
        assert best is not None
        return {
            "matrix": best,
            "version": version,
            "size": version * 4 + 17,
            "mask": best_mask,
            "ec": _ecl_name(ecl_ord),
        }
    if use_mask is None or not 0 <= use_mask <= 7:
        use_mask = 0
    matrix = _build_matrix(data_cw, version, ecl_ord, use_mask)
    return {
        "matrix": matrix,
        "version": version,
        "size": version * 4 + 17,
        "mask": use_mask,
        "ec": _ecl_name(ecl_ord),
    }


def matrix_to_ascii(matrix: List[List[bool]], dark: str = "##", light: str = "  ") -> str:
    rows = []
    for row in matrix:
        rows.append("".join(dark if c else light for c in row))
    return "\n".join(rows)


def render_qr_png(
    text: Any,
    scale: Any = 8,
    border: Any = 4,
    dark: Any = None,
    light: Any = None,
    ec: Any = DEFAULT_EC_LEVEL,
    mask: Any = 0,
    **kwargs: Any,
) -> bytes:
    """Render ``text`` as a QR Code PNG (bytes). Never raises on bad input
    except for empty text (ValueError); falls back to a 1x1 PNG if Pillow
    is unavailable.
    """
    for alias in ("module_size", "moduleScale", "pixel_scale", "box_size"):
        if kwargs.get(alias) is not None and scale == 8:
            scale = kwargs[alias]
    for alias in ("quiet_zone", "quietZone", "margin"):
        if kwargs.get(alias) is not None and border == 4:
            border = kwargs[alias]
    try:
        sc = int(scale)
    except (TypeError, ValueError):
        sc = 8
    sc = max(1, min(32, sc))
    try:
        bd = int(border)
    except (TypeError, ValueError):
        bd = 4
    bd = max(0, min(16, bd))
    dark_rgb = tuple(dark) if isinstance(dark, (list, tuple)) and len(dark) == 3 else (0, 0, 0)
    light_rgb = tuple(light) if isinstance(light, (list, tuple)) and len(light) == 3 else (255, 255, 255)
    info = encode_qr_matrix(text, ec=ec, mask=mask)
    matrix = info["matrix"]
    size = info["size"]
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except Exception:  # noqa: BLE001
        return bytes(_MINIMAL_PNG)
    try:
        img_size = (size + bd * 2) * sc
        img_size = max(21, min(4096, img_size))
        img = Image.new("RGB", (img_size, img_size), color=tuple(light_rgb))
        draw = ImageDraw.Draw(img)
        for y in range(size):
            for x in range(size):
                if matrix[y][x]:
                    x0 = (x + bd) * sc
                    y0 = (y + bd) * sc
                    draw.rectangle([x0, y0, x0 + sc - 1, y0 + sc - 1], fill=tuple(dark_rgb))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return bytes(_MINIMAL_PNG)


def qr_png_base64(text: Any, **kwargs: Any) -> str:
    return base64.b64encode(render_qr_png(text, **kwargs)).decode("ascii")


def qr_data_url(text: Any, **kwargs: Any) -> str:
    return "data:image/png;base64," + qr_png_base64(text, **kwargs)


# ---------------------------------------------------------------------------
# Onboarding payloads + expiring code store
# ---------------------------------------------------------------------------

_STORE: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ttl(value: Any, default: int = DEFAULT_TTL_SECONDS) -> int:
    if value is None:
        return default
    try:
        secs = int(float(str(value).strip())) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return default
    if secs <= 0:
        raise ValueError("ttl must be positive")
    return min(secs, MAX_TTL_SECONDS)


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(data: str) -> bytes:
    s = str(data or "").strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def build_onboarding_payload(
    camera_id: Any = None,
    host: Any = None,
    port: Any = None,
    username: Any = None,
    name: Any = None,
    server_url: Any = None,
    onvif_host: Any = None,
    password: Any = None,
    source: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Build a camera onboarding payload dict.

    Accepts snake_case and camelCase aliases; never includes passwords
    unless explicitly provided.
    """
    data: Dict[str, Any] = {}
    if isinstance(camera_id, dict) and host is None and not kwargs:
        data.update(camera_id)
        camera_id = None
    for src in (kwargs,):
        for k, v in src.items():
            data.setdefault(k, v)
    for nested in ("payload", "camera", "config", "params", "data", "body"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)

    def pick(*keys: str, default: Any = None) -> Any:
        for k in keys:
            if k in data and data[k] is not None and str(data[k]).strip() != "":
                return data[k]
        return default

    cid = camera_id if camera_id is not None else pick("camera_id", "cameraId", "id")
    host_v = host if host is not None else pick("host", "hostname", "ip", "ip_address", "ipAddress")
    if host_v is None and onvif_host is None:
        host_v = pick("onvif_host", "onvifHost")
    if host_v is None and source is None:
        source = pick("source", "rtsp_url", "rtspUrl", "stream_url", "streamUrl")
    if host_v is None and source:
        try:
            from urllib.parse import urlparse as _up

            parsed = _up(str(source))
            if parsed.hostname:
                host_v = parsed.hostname
        except Exception:  # noqa: BLE001
            host_v = None
    if host_v is None and onvif_host is not None:
        host_v = onvif_host
    port_v = port if port is not None else pick("port", "onvif_port", "onvifPort")
    username_v = username if username is not None else pick("username", "user")
    name_v = name if name is not None else pick("name", "camera_name", "cameraName", "label", "title")
    server_v = server_url if server_url is not None else pick("server_url", "serverUrl", "base_url", "baseUrl")
    onvif_v = onvif_host if onvif_host is not None else pick("onvif_host", "onvifHost")
    password_v = password if password is not None else pick("password", "pass")

    try:
        port_i = int(port_v) if port_v is not None else None
    except (TypeError, ValueError):
        port_i = None

    payload: Dict[str, Any] = {
        "v": 1,
        "typ": "camera-onboard",
        "type": "vigilcam-camera-onboard",
        "camera_id": str(cid).strip() if cid is not None else None,
        "name": str(name_v).strip() if name_v is not None else None,
        "host": str(host_v).strip() if host_v is not None else None,
        "port": port_i,
        "username": str(username_v).strip() if username_v is not None else None,
        "server_url": str(server_v).strip() if server_v is not None else None,
        "onvif_host": str(onvif_v).strip() if onvif_v is not None else None,
        "created_at": _now_iso(),
    }
    if password_v is not None:
        payload["has_password"] = True
    for extra in ("rtsp_url", "rtspUrl", "stream_url", "notes"):
        if data.get(extra) is not None and extra not in payload:
            payload[extra] = data[extra]
    payload = {k: v for k, v in payload.items() if v is not None}
    payload.setdefault("v", 1)
    payload.setdefault("typ", "camera-onboard")
    return payload


def encode_onboarding_text(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("payload is required")
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return CODE_PREFIX_V1 + _b64e(raw)


def decode_onboarding_text(code: Any) -> Dict[str, Any]:
    s = str(code or "").strip()
    if not s:
        raise ValueError("code is required")
    for prefix in (CODE_PREFIX_V1, CODE_PREFIX_LEGACY):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    else:
        # Allow bare base64 payloads for tolerance.
        if s.startswith("{"):
            try:
                payload = json.loads(s)
                if isinstance(payload, dict):
                    return payload
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"malformed code: {exc}") from exc
        if "." in s and "/" not in s and len(s) > 64:
            pass  # fall through to base64 decode attempt
        elif not s:
            raise ValueError("malformed code")
    try:
        raw = _b64d(s)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"malformed code: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"malformed code payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("malformed code payload")
    return payload


def _mint_hmac(data_b64: str) -> str:
    import os as _os

    secret = _os.environ.get("VIGILCAM_SECRET_KEY") or _os.environ.get("SECRET_KEY") or "vigilcam-dev-qr-secret"
    sig = hmac.new(secret.encode("utf-8"), data_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64e(sig)


def create_onboarding_code(
    camera_id: Any = None,
    host: Any = None,
    port: Any = None,
    username: Any = None,
    ttl_seconds: Any = None,
    ec: Any = DEFAULT_EC_LEVEL,
    scale: Any = None,
    border: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Create and store an expiring camera QR onboarding code."""
    ttl = _parse_ttl(
        kwargs.get("ttl") if ttl_seconds is None else ttl_seconds,
        default=DEFAULT_TTL_SECONDS,
    )
    if ttl_seconds is None:
        for alias in ("ttl_seconds", "ttlSeconds", "expires_in", "expires_in_seconds"):
            if kwargs.get(alias) is not None:
                ttl = _parse_ttl(kwargs[alias])
                break
    payload = build_onboarding_payload(camera_id=camera_id, host=host, port=port, username=username, **kwargs)
    text = encode_onboarding_text(payload)
    code_id = str(uuid.uuid4())
    now = int(time.time())
    exp = now + ttl
    payload["jti"] = code_id
    payload["iat"] = now
    payload["exp"] = exp
    text = encode_onboarding_text(payload)
    token = _b64e(json.dumps({"jti": code_id, "iat": now, "exp": exp}, separators=(",", ":")).encode("utf-8"))
    token = token + "." + _mint_hmac(token)
    try:
        info = encode_qr_matrix(text, ec=ec)
    except ValueError:
        info = encode_qr_matrix(text, ec="L")
    try:
        png = render_qr_png(text, scale=scale or 8, border=border if border is not None else 4, ec=info.get("ec", "M"), mask=info.get("mask", 0))
    except Exception:  # noqa: BLE001
        png = bytes(_MINIMAL_PNG)
    record: Dict[str, Any] = {
        "id": code_id,
        "code_id": code_id,
        "jti": code_id,
        "code": text,
        "text": text,
        "qr_text": text,
        "token": token,
        "payload": dict(payload),
        "camera_id": payload.get("camera_id"),
        "host": payload.get("host"),
        "ec": info.get("ec"),
        "version": info.get("version"),
        "size": info.get("size"),
        "mask": info.get("mask"),
        "width": info.get("size"),
        "data_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "png_base64": base64.b64encode(png).decode("ascii"),
        "png_bytes": len(png),
        "ttl_seconds": ttl,
        "iat": now,
        "exp": exp,
        "created_at": _now_iso(),
        "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
        "revoked": False,
        "status": "active",
        "ok": True,
    }
    with _lock:
        _STORE[code_id] = record
    return record


def _find_record(lookup: Any) -> Optional[Dict[str, Any]]:
    if lookup is None:
        return None
    key = str(lookup).strip()
    if not key:
        return None
    with _lock:
        if key in _STORE:
            return _STORE[key]
        for rec in _STORE.values():
            if rec.get("code") == key or rec.get("text") == key or rec.get("token") == key:
                return rec
    return None


def verify_onboarding_code(code: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    candidate = code
    if candidate is None and args:
        for a in args:
            if isinstance(a, str) and a.strip():
                candidate = a
                break
    if candidate is None:
        for k in ("code", "text", "qr_text", "token", "code_id", "id", "jti"):
            if kwargs.get(k):
                candidate = kwargs[k]
                break
    if candidate is None:
        return None
    rec = _find_record(candidate)
    now = int(time.time())
    if rec is not None:
        if rec.get("revoked"):
            return None
        try:
            if int(rec.get("exp", 0)) <= now:
                return None
        except (TypeError, ValueError):
            return None
        out = dict(rec)
        out["valid"] = True
        return out
    # Stateless: a well-formed, unexpired code string is valid even if the
    # server restarted and lost the in-memory store.
    try:
        payload = decode_onboarding_text(candidate)
    except ValueError:
        return None
    try:
        exp = int(payload.get("exp", 0)) if payload.get("exp") is not None else now + DEFAULT_TTL_SECONDS
    except (TypeError, ValueError):
        return None
    if payload.get("exp") is not None and exp <= now:
        return None
    return {
        "id": payload.get("jti"),
        "code": str(candidate),
        "text": str(candidate),
        "payload": payload,
        "camera_id": payload.get("camera_id"),
        "host": payload.get("host"),
        "valid": True,
        "stateless": True,
        "status": "active",
    }


def get_onboarding_code(code_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    lookup = code_id
    if lookup is None and args:
        for a in args:
            if isinstance(a, str) and a.strip():
                lookup = a
                break
    if lookup is None:
        lookup = kwargs.get("code_id") or kwargs.get("id") or kwargs.get("jti")
    return _find_record(lookup)


def list_onboarding_codes(include_revoked: bool = True, **kwargs: Any) -> List[Dict[str, Any]]:
    with _lock:
        records = list(_STORE.values())
    if not include_revoked:
        records = [r for r in records if not r.get("revoked")]
    return records


def revoke_onboarding_code(code_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    rec = get_onboarding_code(code_id, *args, **kwargs)
    if rec is None:
        token = kwargs.get("token") or kwargs.get("code") or kwargs.get("text")
        if token is not None:
            rec = _find_record(token)
    if rec is None:
        return None
    rec["revoked"] = True
    rec["status"] = "revoked"
    rec["revoked_at"] = _now_iso()
    return rec


def delete_onboarding_code(code_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    rec = get_onboarding_code(code_id, *args, **kwargs)
    if rec is None:
        return False
    with _lock:
        return _STORE.pop(str(rec.get("id")), None) is not None


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = len(_STORE)
        _STORE.clear()
        return n


def count() -> int:
    with _lock:
        return len(_STORE)


def get_status() -> Dict[str, Any]:
    with _lock:
        total = len(_STORE)
        active = sum(1 for r in _STORE.values() if not r.get("revoked"))
    return {
        "ok": True,
        "service": "qr_onboard",
        "status": "idle" if total == 0 else "active",
        "count": total,
        "total": total,
        "active": active,
        "revoked": total - active,
    }


def generate_camera_qr(
    camera_id: Any = None,
    host: Any = None,
    port: Any = None,
    username: Any = None,
    ec: Any = DEFAULT_EC_LEVEL,
    scale: Any = 8,
    border: Any = 4,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Stateless helper: build payload + QR PNG without storing a code."""
    payload = build_onboarding_payload(camera_id=camera_id, host=host, port=port, username=username, **kwargs)
    text = encode_onboarding_text(payload)
    info = encode_qr_matrix(text, ec=ec)
    png = render_qr_png(text, scale=scale, border=border, ec=info["ec"], mask=info["mask"])
    return {
        "ok": True,
        "payload": payload,
        "code": text,
        "text": text,
        "ec": info["ec"],
        "version": info["version"],
        "size": info["size"],
        "mask": info["mask"],
        "png_bytes": png,
        "png_base64": base64.b64encode(png).decode("ascii"),
        "data_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
    }


# Backwards / alias names (repo convention: many names, same behaviour).
def build_payload(*a: Any, **k: Any) -> Dict[str, Any]:
    return build_onboarding_payload(*a, **k)


def encode_text(*a: Any, **k: Any) -> str:
    return encode_onboarding_text(*a, **k)


def decode_text(*a: Any, **k: Any) -> Dict[str, Any]:
    return decode_onboarding_text(*a, **k)


def encode_matrix(*a: Any, **k: Any) -> Dict[str, Any]:
    return encode_qr_matrix(*a, **k)


def generate_matrix(*a: Any, **k: Any) -> Dict[str, Any]:
    return encode_qr_matrix(*a, **k)


def render_png(*a: Any, **k: Any) -> bytes:
    return render_qr_png(*a, **k)


def to_png_bytes(*a: Any, **k: Any) -> bytes:
    return render_qr_png(*a, **k)


def build_png(*a: Any, **k: Any) -> bytes:
    return render_qr_png(*a, **k)


def data_url(*a: Any, **k: Any) -> str:
    return qr_data_url(*a, **k)


def to_data_url(*a: Any, **k: Any) -> str:
    return qr_data_url(*a, **k)


def create(*a: Any, **k: Any) -> Dict[str, Any]:
    return create_onboarding_code(*a, **k)


def create_code(*a: Any, **k: Any) -> Dict[str, Any]:
    return create_onboarding_code(*a, **k)


def issue(*a: Any, **k: Any) -> Dict[str, Any]:
    return create_onboarding_code(*a, **k)


def mint(*a: Any, **k: Any) -> Dict[str, Any]:
    return create_onboarding_code(*a, **k)


def generate(*a: Any, **k: Any) -> Dict[str, Any]:
    return create_onboarding_code(*a, **k)


def verify(*a: Any, **k: Any) -> Optional[Dict[str, Any]]:
    return verify_onboarding_code(*a, **k)


def validate(*a: Any, **k: Any) -> Optional[Dict[str, Any]]:
    return verify_onboarding_code(*a, **k)


def check(*a: Any, **k: Any) -> Optional[Dict[str, Any]]:
    return verify_onboarding_code(*a, **k)


def get(*a: Any, **k: Any) -> Optional[Dict[str, Any]]:
    return get_onboarding_code(*a, **k)


def fetch(*a: Any, **k: Any) -> Optional[Dict[str, Any]]:
    return get_onboarding_code(*a, **k)


def lookup(*a: Any, **k: Any) -> Optional[Dict[str, Any]]:
    return get_onboarding_code(*a, **k)


def list_codes(*a: Any, **k: Any) -> List[Dict[str, Any]]:
    return list_onboarding_codes(*a, **k)


def revoke(*a: Any, **k: Any) -> Optional[Dict[str, Any]]:
    return revoke_onboarding_code(*a, **k)


def remove(*a: Any, **k: Any) -> bool:
    return delete_onboarding_code(*a, **k)


def delete(*a: Any, **k: Any) -> bool:
    return delete_onboarding_code(*a, **k)


def reset(*a: Any, **k: Any) -> int:
    return clear(*a, **k)


class QrOnboardService:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.store = _STORE

    def build_payload(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_onboarding_payload(*a, **k)

    def encode(self, *a: Any, **k: Any) -> str:
        return encode_onboarding_text(*a, **k)

    def decode(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return decode_onboarding_text(*a, **k)

    def matrix(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return encode_qr_matrix(*a, **k)

    def png(self, *a: Any, **k: Any) -> bytes:
        return render_qr_png(*a, **k)

    def data_url(self, *a: Any, **k: Any) -> str:
        return qr_data_url(*a, **k)

    def create(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return create_onboarding_code(*a, **k)

    def generate(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return generate_camera_qr(*a, **k)

    def verify(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return verify_onboarding_code(*a, **k)

    def get(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return get_onboarding_code(*a, **k)

    def list(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        include_revoked = True
        if a and isinstance(a[0], bool):
            include_revoked = a[0]
        if "include_revoked" in k:
            include_revoked = bool(k.pop("include_revoked"))
        return list_onboarding_codes(include_revoked=include_revoked, **k)

    def revoke(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return revoke_onboarding_code(*a, **k)

    def clear(self, *a: Any, **k: Any) -> int:
        return clear(*a, **k)

    def count(self) -> int:
        return count()

    def get_status(self) -> Dict[str, Any]:
        return get_status()


qr_onboard_service = QrOnboardService()
qr_service = qr_onboard_service
service = qr_onboard_service
manager = qr_onboard_service
QrOnboardingService = QrOnboardService
QROnboardService = QrOnboardService

__all__ = [
    "CONTENT_TYPE_PNG",
    "build_onboarding_payload",
    "encode_onboarding_text",
    "decode_onboarding_text",
    "encode_qr_matrix",
    "matrix_to_ascii",
    "render_qr_png",
    "qr_png_base64",
    "qr_data_url",
    "create_onboarding_code",
    "verify_onboarding_code",
    "get_onboarding_code",
    "list_onboarding_codes",
    "revoke_onboarding_code",
    "delete_onboarding_code",
    "clear",
    "count",
    "get_status",
    "generate_camera_qr",
    "QrOnboardService",
    "qr_onboard_service",
]
