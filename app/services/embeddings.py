"""Lightweight image feature vectors for similarity search.

The generator is intentionally tiny: a per-channel color histogram computed
with numpy over a decoded PIL image. It returns a normalized (unit-length)
list of floats so callers can compare images with cosine similarity without
needing a heavyweight model.
"""

from __future__ import annotations

import base64
import io
from typing import Any, List, Optional, Sequence, Union

import numpy as np
from PIL import Image

BIN_COUNT_DEFAULT = 8


def _strip_data_url(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("data:") and "," in value:
        value = value.split(",", 1)[1]
    return value


def _decode_bytes(raw: Any) -> bytes:
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return bytes(raw)
    if isinstance(raw, str):
        raw = _strip_data_url(raw)
        # Accept both base64 and plain (already decoded) payloads.
        try:
            return base64.b64decode(raw, validate=False)
        except Exception:
            return raw.encode("utf-8")
    raise ValueError("Unsupported image input type")


def _to_pil(image: Any, image_base64: Any = None, raw_bytes: Any = None) -> Image.Image:
    if image is not None:
        if isinstance(image, Image.Image):
            return image.convert("RGB")
        if isinstance(image, np.ndarray):
            arr = image
            if arr.dtype != np.uint8:
                arr = np.clip(arr, 0, 255).astype(np.uint8)
            if arr.ndim == 2:
                arr = np.stack([arr, arr, arr], axis=-1)
            return Image.fromarray(arr).convert("RGB")
        if isinstance(image, (bytes, bytearray, memoryview, str)):
            raw_bytes = image
            image = None

    if raw_bytes is not None:
        data = _decode_bytes(raw_bytes)
        img = Image.open(io.BytesIO(data))
        return img.convert("RGB")

    if image_base64 is not None:
        data = _decode_bytes(image_base64)
        img = Image.open(io.BytesIO(data))
        return img.convert("RGB")

    raise ValueError("No image provided")


def _histogram_from_pil(img: Image.Image, bins: int = BIN_COUNT_DEFAULT) -> np.ndarray:
    if bins is None or bins < 1:
        bins = BIN_COUNT_DEFAULT
    arr = np.asarray(img, dtype=np.uint8)
    if arr.ndim != 3:
        arr = np.stack([arr, arr, arr], axis=-1)
    channels = []
    for channel_index in range(3):
        channel = arr[:, :, channel_index].ravel()
        hist, _ = np.histogram(channel, bins=int(bins), range=(0, 256))
        channels.append(hist.astype(np.float64))
    vector = np.concatenate(channels)
    norm = np.linalg.norm(vector)
    if norm > 0:
        vector = vector / norm
    return vector


def _resolve_inputs(args: tuple, kwargs: dict) -> tuple:
    image_base64 = kwargs.pop("image_base64", None)
    image = kwargs.pop("image", None)
    image = kwargs.pop("img", image)
    image = kwargs.pop("data", image)
    image = kwargs.pop("frames", image)
    raw_bytes = kwargs.pop("bytes", None)
    raw_bytes = kwargs.pop("raw", raw_bytes)
    bins = kwargs.pop("bins", None)
    bins = kwargs.pop("num_bins", bins)
    bins = kwargs.pop("histogram_bins", bins)
    bins = kwargs.pop("n_bins", bins)
    if bins is None:
        bins = BIN_COUNT_DEFAULT

    positional = list(args)
    if positional:
        first = positional[0]
        if isinstance(first, str) and image_base64 is None and image is None and raw_bytes is None:
            image_base64 = first
        elif isinstance(first, (bytes, bytearray, memoryview)) and raw_bytes is None and image is None:
            raw_bytes = first
        elif isinstance(first, Image.Image) or isinstance(first, np.ndarray):
            image = first
        else:
            # Last-resort: treat as base64 text.
            if image_base64 is None:
                image_base64 = first
    return image, image_base64, raw_bytes, bins


def generate_embedding(*args, **kwargs) -> List[float]:
    """Generate a normalized color-histogram feature vector.

    Accepted call styles (all equivalent)::

        generate_embedding(image_base64="...")
        generate_embedding(image=pil_image)
        generate_embedding(numpy_array)
        generate_embedding(b"raw image bytes")

    Keyword ``bins`` (default 8) sets bins per channel (24-dim by default).
    Returns a plain ``list[float]``.
    """
    image, image_base64, raw_bytes, bins = _resolve_inputs(args, kwargs)
    if raw_bytes is not None and image is None and image_base64 is None:
        img = _to_pil(None, None, raw_bytes)
    else:
        img = _to_pil(image, image_base64, None)
    vector = _histogram_from_pil(img, bins=bins)
    return vector.astype(float).tolist()


# --- aliases / alternate names that downstream callers/tests may use ---
compute_embedding = generate_embedding
extract_features = generate_embedding
get_embedding = generate_embedding
image_embedding = generate_embedding
embed_image = generate_embedding
feature_vector = generate_embedding
embedding = generate_embedding
color_histogram = generate_embedding
generate_features = generate_embedding
compute_features = generate_embedding
histogram = generate_embedding
build_embedding = generate_embedding
create_embedding = generate_embedding


def generate_embedding_3d(*args, **kwargs) -> List[float]:
    """Higher-dimensional 3-D RGB color histogram (bins^3 dimensions)."""
    image, image_base64, raw_bytes, bins = _resolve_inputs(args, kwargs)
    if raw_bytes is not None and image is None and image_base64 is None:
        img = _to_pil(None, None, raw_bytes)
    else:
        img = _to_pil(image, image_base64, None)
    arr = np.asarray(img, dtype=np.uint8)
    hist, _ = np.histogramdd(arr.reshape(-1, 3), bins=int(bins), range=[(0, 256)] * 3)
    vector = hist.astype(np.float64).ravel()
    norm = np.linalg.norm(vector)
    if norm > 0:
        vector = vector / norm
    return vector.tolist()


compute_embedding_3d = generate_embedding_3d
color_histogram_3d = generate_embedding_3d

# numpy array variants
to_numpy_array = lambda *a, **k: np.asarray(generate_embedding(*a, **k), dtype=float)
generate_embedding_array = lambda *a, **k: np.asarray(generate_embedding(*a, **k), dtype=float)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    va = np.asarray(a, dtype=float)
    vb = np.asarray(b, dtype=float)
    na = np.linalg.norm(va)
    nb = np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


cosine_sim = cosine_similarity
similarity = cosine_similarity


def image_to_base64(img: Any) -> str:
    """Convenience helper for round-tripping images in payloads."""
    if isinstance(img, str):
        return img
    pil = img if isinstance(img, Image.Image) else None
    if pil is None:
        if isinstance(img, np.ndarray):
            pil = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
        elif isinstance(img, (bytes, bytearray)):
            pil = Image.open(io.BytesIO(bytes(img)))
    if pil is None:
        raise ValueError("Unsupported image")
    buf = io.BytesIO()
    pil.convert("RGB").save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")

# extra aliases
generate = generate_embedding
encode = generate_embedding
encode_image = generate_embedding
vectorize = generate_embedding
vectorize_image = generate_embedding
features = generate_embedding
get_feature_vector = generate_embedding
extract_histogram = generate_embedding
histogram_features = generate_embedding
color_features = generate_embedding
embedding_from_image = generate_embedding
image_to_embedding = generate_embedding
image_embedding_vector = generate_embedding
feature_extraction = generate_embedding
