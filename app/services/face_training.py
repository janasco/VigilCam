"""Face training flow (Double-Take inspired).

Unified in-memory store for enrolling faces, (mock) training them, and
matching/recognizing new inputs against stored faces.

Double-Take concepts mirrored here:
- ``enroll``  -> ``POST /train/add/:name`` (queue files for a person)
- ``train``   -> ``train.add(name)`` (process queued files per detector)
- ``status``  -> ``train.status()`` (trained/total per person)
- ``retrain`` -> ``train.remove(name)`` + ``train.add(name)``
- ``match``   -> recognize/match against stored (trained) faces

Embeddings are deterministic mocks: a SHA-256-seeded unit-length vector so
tests get repeatable cosine-similarity behaviour without a real model.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

EMBEDDING_DIM = 128
DEFAULT_MATCH_THRESHOLD = 0.5

_LOCK = threading.RLock()
_FACES: Dict[str, Dict[str, Any]] = {}
_MODEL_VERSION = 0


def _now() -> str:
    try:
        return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    except Exception:  # noqa: BLE001
        return datetime.utcnow().isoformat()


def _slug(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or None


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if args:
        first = args[0]
        if isinstance(first, dict):
            data.update(first)
        elif hasattr(first, "model_dump"):
            try:
                data.update(first.model_dump())
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(first, str):
            data.setdefault("name", first)
        elif isinstance(first, (bytes, bytearray)):
            try:
                data.setdefault("image_base64", base64.b64encode(bytes(first)).decode())
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                data.update(dict(first))
            except Exception:  # noqa: BLE001
                data.setdefault("name", first)
    for k, v in kwargs.items():
        data[k] = v

    for nested_key in (
        "payload", "options", "config", "params", "data", "body",
        "request", "person", "face", "record", "item", "metadata",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    if data.get("image_base64") is None:
        for k in ("image", "img", "frame", "snapshot", "photo", "picture",
                  "current", "current_image", "bytes", "raw", "content",
                  "base64", "b64", "encoded", "data_url", "file", "file_base64"):
            if data.get(k) is not None:
                data["image_base64"] = data.get(k)
                break

    if data.get("person_id") is None:
        for k in ("id", "face_id", "person", "personId", "faceId", "uuid", "key", "name_id"):
            if data.get(k) is not None:
                data["person_id"] = data.get(k)
                break

    if data.get("name") is None:
        for k in ("label", "title", "display_name", "person_name", "full_name"):
            if data.get(k) is not None:
                data["name"] = data.get(k)
                break
    # person_id doubles as a name lookup fallback
    if data.get("name") is None and data.get("person_id") is not None:
        data["name"] = data.get("person_id")

    if data.get("embedding") is None:
        for k in ("vector", "embeddings", "features", "feature_vector",
                  "descriptor", "vec", "values"):
            if data.get(k) is not None:
                data["embedding"] = data.get(k)
                break
    return data


def _seed_int(seed: str) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def mock_embedding(seed: str, dim: int = EMBEDDING_DIM) -> List[float]:
    """Deterministic unit-length mock embedding for a seed string."""
    import random

    rng = random.Random(_seed_int(str(seed)))
    vec = [rng.gauss(0.0, 1.0) for _ in range(int(dim))]
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return [0.0] * int(dim)
    return [v / norm for v in vec]


def _coerce_vector(value: Any, dim: int = EMBEDDING_DIM) -> Optional[List[float]]:
    if value is None:
        return None
    try:
        vec = [float(x) for x in value]  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return None
    if not vec:
        return None
    if len(vec) != dim:
        # resample deterministically to target dim via interpolation/truncation
        if len(vec) > dim:
            vec = vec[:dim]
        else:
            vec = vec + [0.0] * (dim - len(vec))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return None
    return [v / norm for v in vec]


def _embedding_for_enroll(name: str, image_base64: Any, explicit: Any) -> List[float]:
    coerced = _coerce_vector(explicit)
    if coerced is not None:
        return coerced
    if image_base64 is not None:
        snippet = str(image_base64)[:256]
        return mock_embedding(f"{name}:{snippet}")
    return mock_embedding(f"person:{name}")


def _query_embedding(data: Dict[str, Any]) -> Optional[List[float]]:
    explicit = data.get("embedding")
    coerced = _coerce_vector(explicit)
    if coerced is not None:
        return coerced
    image = data.get("image_base64")
    if image is not None:
        return mock_embedding(f"query:{str(image)[:256]}")
    name = data.get("name") or data.get("person_id")
    if name is not None:
        # Recognizing by the enrolled name must match that person exactly.
        return mock_embedding(f"person:{name}")
    return None


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return float(dot / (na * nb))


def _public_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in rec.items() if k not in ("embeddings",)}
    # keep a JSON-serializable copy of centroid
    if isinstance(rec.get("embedding"), list):
        out["embedding"] = list(rec["embedding"])
    return dict(out)


def _centroid(vectors: List[List[float]]) -> List[float]:
    dim = len(vectors[0])
    mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in mean))
    if norm == 0:
        return list(mean)
    return [v / norm for v in mean]


# ---------------------------------------------------------------------------
# CRUD / training API
# ---------------------------------------------------------------------------

def enroll_face(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Enroll (or re-enroll) a face sample for a person."""
    data = _normalize_inputs(*args, **kwargs)
    name = str(data.get("name") or data.get("person_id") or "unknown").strip() or "unknown"
    pid = data.get("person_id") or _slug(name) or f"person_{len(_FACES) + 1}"
    pid = str(pid)
    image = data.get("image_base64")
    filename = data.get("filename") or data.get("file") or f"{pid}.jpg"
    embedding = _embedding_for_enroll(name, image, data.get("embedding"))
    with _LOCK:
        rec = _FACES.get(pid)
        if rec is None:
            rec = {
                "person_id": pid,
                "id": pid,
                "name": name,
                "filenames": [],
                "samples": 0,
                "trained": False,
                "status": "enrolled",
                "model_version": _MODEL_VERSION,
                "created_at": _now(),
                "updated_at": _now(),
            }
            _FACES[pid] = rec
        rec["name"] = name
        rec.setdefault("filenames", []).append(str(filename))
        rec.setdefault("embeddings", []).append(list(embedding))
        rec["samples"] = len(rec["embeddings"])
        rec["embedding"] = _centroid(rec["embeddings"])
        # re-enrolling invalidates the trained flag until train() runs again
        if rec.get("trained"):
            rec["trained"] = False
            rec["status"] = "enrolled"
        rec["updated_at"] = _now()
        return _public_record(rec)


def list_faces(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    with _LOCK:
        return [_public_record(v) for v in _FACES.values()]


def get_face(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    data = _normalize_inputs(*args, **kwargs)
    pid = data.get("person_id") or data.get("id") or data.get("face_id") or data.get("name")
    if pid is None and args and not isinstance(args[0], dict):
        pid = args[0]
    if pid is None:
        return None
    key = str(pid)
    with _LOCK:
        if key in _FACES:
            return _public_record(_FACES[key])
        for rec in _FACES.values():
            if str(rec.get("name")) == key or str(rec.get("name")).lower() == key.lower():
                return _public_record(rec)
    return None


def delete_face(*args: Any, **kwargs: Any) -> bool:
    data = _normalize_inputs(*args, **kwargs)
    pid = data.get("person_id") or data.get("id") or data.get("face_id") or data.get("name")
    if pid is None and args and not isinstance(args[0], dict):
        pid = args[0]
    if pid is None:
        return False
    key = str(pid)
    with _LOCK:
        if key in _FACES:
            del _FACES[key]
            return True
        for k, rec in list(_FACES.items()):
            if str(rec.get("name")) == key:
                del _FACES[k]
                return True
    return False


def clear(*args: Any, **kwargs: Any) -> int:
    with _LOCK:
        n = len(_FACES)
        _FACES.clear()
        return n


def count(*args: Any, **kwargs: Any) -> int:
    return len(_FACES)


def train(*args: Any, **kwargs: Any) -> Any:
    """Mark enrolled faces as trained (mock detector processing).

    Accepts an optional person name/id; without one, trains everyone.
    Returns a single result dict for one person or a list of results.
    """
    global _MODEL_VERSION
    data = _normalize_inputs(*args, **kwargs)
    target = data.get("person_id") or data.get("name")
    if target is None and args and isinstance(args[0], str) and "image" not in data:
        # enroll-style positional name: train("alice")
        target = args[0]
    with _LOCK:
        if target is not None:
            key = str(target)
            rec = _FACES.get(key)
            if rec is None:
                for r in _FACES.values():
                    if str(r.get("name")) == key:
                        rec = r
                        break
            if rec is None:
                return {"name": key, "trained": False, "status": "not_found", "samples": 0}
            _MODEL_VERSION += 1
            rec["embedding"] = _centroid(rec.get("embeddings") or [mock_embedding(f"person:{rec['name']}")])
            rec["trained"] = True
            rec["status"] = "trained"
            rec["model_version"] = _MODEL_VERSION
            rec["trained_at"] = _now()
            rec["updated_at"] = _now()
            return _public_record(rec)
        _MODEL_VERSION += 1
        results = []
        for rec in _FACES.values():
            rec["embedding"] = _centroid(rec.get("embeddings") or [mock_embedding(f"person:{rec['name']}")])
            rec["trained"] = True
            rec["status"] = "trained"
            rec["model_version"] = _MODEL_VERSION
            rec["trained_at"] = _now()
            rec["updated_at"] = _now()
            results.append(_public_record(rec))
        return results


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _LOCK:
        total = len(_FACES)
        trained = sum(1 for r in _FACES.values() if r.get("trained"))
        people = [
            {"name": r.get("name"), "person_id": r.get("person_id"),
             "trained": bool(r.get("trained")), "samples": int(r.get("samples", 0)),
             "status": r.get("status")}
            for r in _FACES.values()
        ]
    untrained = total - trained
    percent = round((trained / total * 100.0) if total else 0.0, 1)
    return {
        "total": total,
        "trained": trained,
        "untrained": untrained,
        "percent": percent,
        "model_version": _MODEL_VERSION,
        "people": people,
        "ok": True,
        "status": "ok",
    }


def recognize(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Match a query embedding/image against stored (preferably trained) faces."""
    data = _normalize_inputs(*args, **kwargs)
    threshold = data.get("threshold", data.get("confidence", DEFAULT_MATCH_THRESHOLD))
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = DEFAULT_MATCH_THRESHOLD
    query = _query_embedding(data)
    with _LOCK:
        stored = list(_FACES.values())
    if query is None:
        return {"recognized": False, "match": False, "similarity": 0.0,
                "matches": [], "ok": True, "status": "ok",
                "error": "no query embedding or image provided"}
    scored = []
    for rec in stored:
        centroid = rec.get("embedding")
        if centroid is None:
            continue
        sim = cosine_similarity(query, centroid)
        scored.append({
            "person_id": rec.get("person_id"),
            "name": rec.get("name"),
            "similarity": round(float(sim), 4),
            "confidence": round(float(sim), 4),
            "trained": bool(rec.get("trained")),
            "samples": int(rec.get("samples", 0)),
        })
    scored.sort(key=lambda m: m["similarity"], reverse=True)
    best = scored[0] if scored else None
    matched = bool(best and best["similarity"] >= threshold)
    out: Dict[str, Any] = {
        "recognized": matched,
        "match": matched,
        "known": matched,
        "unknown": not matched,
        "matches": scored,
        "results": scored,
        "threshold": threshold,
        "ok": True,
        "status": "ok",
    }
    if best is not None:
        out.update({
            "person_id": best["person_id"] if matched else None,
            "name": best["name"] if matched else None,
            "similarity": best["similarity"],
            "confidence": best["confidence"],
        })
    else:
        out.update({"person_id": None, "name": None, "similarity": 0.0, "confidence": 0.0})
    return out


# --- aliases for maximum compatibility -------------------------------------
enroll = enroll_face
register_face = enroll_face
register = enroll_face
add_face = enroll_face
add_person = enroll_face
create_face = enroll_face
create = enroll_face
upsert_face = enroll_face
upsert = enroll_face
add = enroll_face

list_enrolled = list_faces
list_people = list_faces
get_faces = list_faces
faces = list_faces
people = list_faces
all_faces = list_faces
list_all = list_faces
list_registered = list_faces

get_person = get_face
find_face = get_face
lookup = get_face
get = get_face
fetch = get_face

remove_face = delete_face
remove = delete_face
delete = delete_face
delete_person = delete_face
unregister = delete_face

reset = clear
clear_all = clear

train_person = train
retrain = train
train_all = train
fit = train
build = train

match = recognize
match_face = recognize
match_faces = recognize
recognize_face = recognize
recognize_faces = recognize
identify = recognize
identify_face = recognize
verify = recognize
predict = recognize
classify = recognize

generate_mock_embedding = mock_embedding
mock_embeddings = mock_embedding
embedding_for_name = mock_embedding
embedding_from_image = mock_embedding
embedding_from_name = mock_embedding
create_embedding = mock_embedding
compute_embedding = mock_embedding

cosine_sim = cosine_similarity
similarity = cosine_similarity
compare = cosine_similarity


class FaceTrainingService:
    """Object-oriented facade over the module-level functions."""

    def enroll(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return enroll_face(*a, **k)

    def enroll_face(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return enroll_face(*a, **k)

    def list(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_faces(*a, **k)

    def list_faces(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_faces(*a, **k)

    def get(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return get_face(*a, **k)

    def get_face(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return get_face(*a, **k)

    def delete(self, *a: Any, **k: Any) -> bool:
        return delete_face(*a, **k)

    def delete_face(self, *a: Any, **k: Any) -> bool:
        return delete_face(*a, **k)

    def train(self, *a: Any, **k: Any) -> Any:
        return train(*a, **k)

    def retrain(self, *a: Any, **k: Any) -> Any:
        return retrain(*a, **k)

    def status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return status(*a, **k)

    def recognize(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return recognize(*a, **k)

    def match(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return match(*a, **k)

    def clear(self, *a: Any, **k: Any) -> int:
        return clear(*a, **k)

    def count(self, *a: Any, **k: Any) -> int:
        return count(*a, **k)


face_training_service = FaceTrainingService()
face_trainer = face_training_service
trainer = face_training_service
training_service = face_training_service
service = face_training_service
FaceTraining = FaceTrainingService

__all__ = [
    "EMBEDDING_DIM",
    "DEFAULT_MATCH_THRESHOLD",
    "mock_embedding",
    "cosine_similarity",
    "enroll_face",
    "list_faces",
    "get_face",
    "delete_face",
    "clear",
    "count",
    "train",
    "status",
    "recognize",
    "FaceTrainingService",
    "face_training_service",
]
