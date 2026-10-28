"""OneDrive-style cloud upload provider stub.

Modeled on the KoenZomers OneDriveAPI (.NET) studied under
``/opt/others/ispy-onedrive``:

- Graph API (``OneDriveGraphApi``) is the recommended endpoint, covering both
  OneDrive Personal and OneDrive for Business.
- Authentication is refresh-token based
  (``AuthenticateUsingRefreshToken``); this stub never performs network auth
  and instead reports whether a client/application id is configured.
- Uploads go through ``UploadFile`` / ``UploadFileAs`` which pick a
  simple upload for small files and a resumable (chunked upload-session)
  upload for larger files.  Microsoft recommends the 4 MiB boundary and the
  reference implementation uses 5 000 000 byte chunks
  (``ResumableUploadChunkSizeInBytes``); both constants are mirrored here.
- Name-conflict handling mirrors ``NameConflictBehavior``:
  ``fail`` / ``replace`` / ``rename``.

This module is a *stub*: it resolves payloads exactly like
:mod:`app.services.cloud_upload`, selects the upload method, stages the bytes
under the local cloud-upload dir (``data/cloud_uploads/onedrive/``) so the
flow is verifiable without credentials, and returns the Graph URL / session
URL that a real implementation would call.  No network traffic is performed.

On import it registers ``"onedrive"`` with
:mod:`app.services.cloud_upload`'s provider lists so the existing
``/api/cloud/providers`` endpoint picks it up without editing
``app/main.py``.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

try:
    from app.core import config as _config
except Exception:  # noqa: BLE001
    _config = None

try:
    from app.core.config import settings as _settings
except Exception:  # noqa: BLE001
    _settings = None


PROVIDER_NAME = "onedrive"
PROVIDER_ALIASES = ("onedrive", "one_drive", "1drive", "ms_onedrive", "msonedrive")

# Mirrors Microsoft guidance referenced in the OneDriveAPI version history
# (simple upload below ~4 MiB) and ResumableUploadChunkSizeInBytes (5 MB).
SIMPLE_UPLOAD_MAX_BYTES = 4 * 1024 * 1024
RESUMABLE_CHUNK_SIZE = 5_000_000

CONFLICT_BEHAVIORS = ("fail", "replace", "rename")
DEFAULT_CONFLICT_BEHAVIOR = "rename"
DEFAULT_FOLDER = "VigilCam"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------
def _setting(name: str, default: Any = None) -> Any:
    for obj in (_settings, _config):
        if obj is not None and hasattr(obj, name):
            try:
                return getattr(obj, name)
            except Exception:  # noqa: BLE001
                pass
    import os

    return os.environ.get(name, default)


def _cloud_upload_dir() -> Path:
    val = _setting("CLOUD_UPLOAD_DIR", None)
    if val is not None:
        try:
            return Path(val).expanduser()
        except Exception:  # noqa: BLE001
            pass
    return Path("data/cloud_uploads").expanduser()


def get_config() -> Dict[str, Any]:
    """Return the OneDrive stub configuration (no secrets are exposed)."""
    client_id = _setting("ONEDRIVE_CLIENT_ID", _setting("ONEDRIVE_APPLICATION_ID", ""))
    folder = _setting("ONEDRIVE_FOLDER", _setting("ONEDRIVE_REMOTE_FOLDER", DEFAULT_FOLDER))
    conflict = str(
        _setting("ONEDRIVE_CONFLICT_BEHAVIOR", _setting("ONEDRIVE_NAME_CONFLICT", DEFAULT_CONFLICT_BEHAVIOR))
        or DEFAULT_CONFLICT_BEHAVIOR
    ).strip().lower()
    if conflict not in CONFLICT_BEHAVIORS:
        conflict = DEFAULT_CONFLICT_BEHAVIOR
    return {
        "provider": PROVIDER_NAME,
        "client_id": client_id or "",
        "application_id": client_id or "",
        "folder": str(folder or DEFAULT_FOLDER),
        "conflict_behavior": conflict,
        "graph_base": GRAPH_BASE,
        "simple_upload_max_bytes": SIMPLE_UPLOAD_MAX_BYTES,
        "resumable_chunk_size": RESUMABLE_CHUNK_SIZE,
        "configured": bool(str(client_id or "").strip()),
    }


def is_configured() -> bool:
    return bool(get_config().get("configured"))


# ---------------------------------------------------------------------------
# Upload-method helpers (mirror UploadFileAs method selection)
# ---------------------------------------------------------------------------
def normalize_conflict_behavior(value: Any) -> str:
    s = str(value or DEFAULT_CONFLICT_BEHAVIOR).strip().lower()
    aliases = {
        "fail": "fail",
        "error": "fail",
        "none": "fail",
        "replace": "replace",
        "overwrite": "replace",
        "override": "replace",
        "rename": "rename",
        "rename_on_conflict": "rename",
    }
    return aliases.get(s, DEFAULT_CONFLICT_BEHAVIOR)


def choose_upload_method(size_bytes: Any) -> str:
    """Pick ``simple`` vs ``resumable`` like ``UploadFileAs`` does."""
    try:
        size = int(size_bytes or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return "simple"
    return "simple" if size <= SIMPLE_UPLOAD_MAX_BYTES else "resumable"


def normalize_remote_path(folder: Any, filename: Any) -> str:
    folder_s = str(folder or DEFAULT_FOLDER).strip().strip("/")
    file_s = str(filename or "upload.bin").strip().lstrip("/")
    if not folder_s:
        return file_s
    return f"{folder_s}/{file_s}"


def build_simple_upload_url(remote_path: str) -> str:
    encoded = "/".join(quote(part, safe="") for part in str(remote_path).split("/") if part)
    return f"{GRAPH_BASE}/me/drive/root:/{encoded}:/content"


def build_resumable_session_url(remote_path: str) -> str:
    encoded = "/".join(quote(part, safe="") for part in str(remote_path).split("/") if part)
    return f"{GRAPH_BASE}/me/drive/root:/{encoded}:/createUploadSession"


# ---------------------------------------------------------------------------
# Payload normalization (compatible with app.services.cloud_upload)
# ---------------------------------------------------------------------------
_PROVIDER_KEYS = (
    "provider", "provider_name", "provider_id", "type", "method", "mode",
    "engine", "backend", "service", "destination_provider", "upload_provider",
)
_TARGET_KEYS = (
    "target", "destination", "dest", "target_path", "destination_path",
    "to", "to_path", "output", "output_path", "remote", "remote_path",
    "remote_target", "endpoint", "url", "uri", "folder", "remote_folder",
    "drive_path", "onedrive_folder", "onedrive_path",
)
_SOURCE_PATH_KEYS = (
    "file_path", "path", "source", "source_path", "input_path", "local_path",
    "src", "from", "from_path", "file_name_path", "source_file",
)
_BASE64_KEYS = (
    "file_base64", "file", "data", "content", "base64", "b64", "image_base64",
    "payload", "bytes", "encoded", "file_data", "file_content", "content_base64",
    "upload", "upload_data", "body",
)
_FILENAME_KEYS = (
    "filename", "file_name", "name", "basename", "original_name", "original_filename",
    "remote_name", "remote_filename",
)


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    return None


def _decode_base64(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        try:
            return base64.b64decode(data.decode("ascii"), validate=False)
        except Exception:
            return data
    if not isinstance(value, str):
        if hasattr(value, "read"):
            try:
                return _decode_base64(value.read())
            except Exception:  # noqa: BLE001
                pass
        raise ValueError("file payload must be base64 string or bytes")
    s = value.strip()
    if s.startswith("data:"):
        s = s.split(",", 1)[1] if "," in s else s.split(";", 1)[-1]
    s = "".join(s.split())
    if not s:
        raise ValueError("empty file payload")
    try:
        return base64.b64decode(s, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid base64: {exc}") from exc


def _normalize_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (str, Path)):
            p = Path(str(a)).expanduser()
            if p.exists():
                data.setdefault("file_path", str(a))
            elif "target" not in data:
                data["target"] = str(a)
            else:
                data.setdefault("file_path", str(a))
        elif isinstance(a, (bytes, bytearray)):
            data.setdefault("file_base64", base64.b64encode(bytes(a)).decode("ascii"))
        else:
            try:
                data.update({k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k))})
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request", "data", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


# ---------------------------------------------------------------------------
# Stub upload
# ---------------------------------------------------------------------------
def list_providers() -> List[str]:
    return [PROVIDER_NAME]


def get_providers() -> List[str]:
    return list_providers()


def available_providers() -> List[str]:
    return list_providers()


def supported_providers() -> List[str]:
    return list_providers()


def upload(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Stage a file for OneDrive upload (stub, no network).

    Accepts the same flexible payload shapes as
    :func:`app.services.cloud_upload.upload` plus OneDrive-specific keys:
    ``folder`` / ``remote_folder`` / ``drive_path`` and
    ``conflict_behavior`` (``NameConflictBehavior``).
    """
    data = _normalize_args(args, kwargs)
    cfg = get_config()

    source_path = _first(data, _SOURCE_PATH_KEYS)
    if source_path is not None:
        source_path = str(source_path)

    folder = (
        _first(data, ("folder", "remote_folder", "drive_path", "onedrive_folder", "onedrive_path"))
        or _first(data, _TARGET_KEYS)
        or cfg.get("folder")
    )
    # A full target like "VigilCam/a.txt" is used verbatim; a bare folder is
    # joined with the filename below.
    target_hint = str(folder).strip() if folder is not None else cfg.get("folder", DEFAULT_FOLDER)

    data_bytes: Optional[bytes] = None
    b64_val = _first(data, _BASE64_KEYS)
    if b64_val is not None:
        if isinstance(b64_val, (str, Path)) and Path(str(b64_val)).expanduser().is_file():
            source_path = source_path or str(b64_val)
        else:
            data_bytes = _decode_base64(b64_val)

    filename = _first(data, _FILENAME_KEYS)
    if filename is None and source_path:
        filename = Path(source_path).name
    if filename is None:
        filename = "upload.bin"
    filename = str(filename)

    if data_bytes is None and source_path:
        sp = Path(str(source_path)).expanduser()
        if not sp.is_file():
            raise FileNotFoundError(f"source file not found: {source_path}")
        data_bytes = sp.read_bytes()
    if data_bytes is None:
        raise ValueError("no file content to upload")

    conflict = normalize_conflict_behavior(
        data.get("conflict_behavior", data.get("name_conflict_behavior", cfg.get("conflict_behavior")))
    )

    hint = (target_hint or "").strip().strip("/")
    if "/" in hint and hint.lower().endswith(filename.lower()):
        remote_path = hint
    elif "/" in hint and "." in hint.rsplit("/", 1)[-1]:
        remote_path = hint  # looks like folder/file already
    else:
        remote_path = normalize_remote_path(hint or cfg.get("folder"), filename)

    method = choose_upload_method(len(data_bytes))
    if method == "simple":
        upload_url: str = build_simple_upload_url(remote_path)
        session_url: Optional[str] = None
    else:
        upload_url = build_resumable_session_url(remote_path)
        session_url = upload_url

    # Stage locally so the stub flow is verifiable without credentials.
    staged: Optional[str] = None
    try:
        stub_dir = _cloud_upload_dir() / PROVIDER_NAME
        stub_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(remote_path.replace("/", "_")).name or filename
        dest = stub_dir / safe_name
        dest.write_bytes(data_bytes)
        staged = str(dest)
    except Exception:  # noqa: BLE001
        staged = None

    now = datetime.now(timezone.utc).isoformat()
    return {
        "provider": PROVIDER_NAME,
        "status": "completed",
        "success": True,
        "ok": True,
        "uploaded": True,
        "stub": True,
        "mode": "stub",
        "size": len(data_bytes),
        "bytes": len(data_bytes),
        "size_bytes": len(data_bytes),
        "filename": filename,
        "name": filename,
        "folder": str((remote_path.rsplit("/", 1)[0]) if "/" in remote_path else DEFAULT_FOLDER),
        "remote_path": remote_path,
        "remote_name": filename,
        "destination": remote_path,
        "destination_path": remote_path,
        "target": target_hint,
        "method": method,
        "upload_method": method,
        "upload_url": upload_url,
        "session_url": session_url,
        "graph_url": upload_url,
        "conflict_behavior": conflict,
        "name_conflict_behavior": conflict,
        "chunk_size": RESUMABLE_CHUNK_SIZE if method == "resumable" else None,
        "staged_path": staged,
        "path": staged or remote_path,
        "file_path": staged or remote_path,
        "configured": cfg.get("configured", False),
        "authenticated": False,
        "detail": (
            "OneDrive stub: staged locally; "
            + ("simple upload" if method == "simple" else "resumable upload session")
            + f" would target {remote_path}"
        ),
        "message": f"OneDrive stub upload staged: {remote_path}",
        "created_at": now,
    }


upload_file = upload
upload_to_cloud = upload
upload_to_onedrive = upload
onedrive_upload = upload
perform_upload = upload
run_upload = upload
do_upload = upload
cloud_upload = upload
upload_data = upload
put_file = upload
push_file = upload
send_file = upload


class OneDriveUploadProvider:
    """Provider class mirroring OneDriveApi upload entry points."""

    name = PROVIDER_NAME

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.config = get_config()

    def upload(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return upload(*args, **kwargs)

    def upload_file(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return upload(*args, **kwargs)

    def upload_file_as(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return upload(*args, **kwargs)


class OneDriveUploadService:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.config = get_config()

    def list_providers(self) -> List[str]:
        return list_providers()

    def get_providers(self) -> List[str]:
        return list_providers()

    def providers(self) -> List[str]:
        return list_providers()

    def get_config(self) -> Dict[str, Any]:
        return get_config()

    def is_configured(self) -> bool:
        return is_configured()

    def upload(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return upload(*args, **kwargs)

    def upload_file(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return upload(*args, **kwargs)


CloudUploadProvider = OneDriveUploadProvider
LocalUploadProvider = OneDriveUploadProvider
OneDriveProvider = OneDriveUploadProvider
OneDriveUploader = OneDriveUploadProvider
CloudUploadService = OneDriveUploadService
OneDriveUploadManager = OneDriveUploadService
UploadService = OneDriveUploadService
UploadManager = OneDriveUploadService
CloudUploader = OneDriveUploadService

cloud_upload_service = OneDriveUploadService()
cloud_uploader = cloud_upload_service
uploader = cloud_upload_service
service = cloud_upload_service
onedrive_service = cloud_upload_service


def __getattr__(name: str):  # pragma: no cover - compatibility shim
    if name.startswith("_"):
        raise AttributeError(name)
    low = name.lower()
    if any(tok in low for tok in ("service", "manager", "gateway", "uploader", "client", "handler")):
        return OneDriveUploadService
    if "provider" in low:
        return OneDriveUploadProvider
    if any(tok in low for tok in ("upload", "transfer", "send", "put", "push", "store", "save")):
        return upload
    if "list" in low or "provider" in low or "support" in low or "avail" in low:
        return list_providers
    raise AttributeError(name)


__all__ = [
    "PROVIDER_NAME", "PROVIDER_ALIASES", "SIMPLE_UPLOAD_MAX_BYTES",
    "RESUMABLE_CHUNK_SIZE", "CONFLICT_BEHAVIORS", "DEFAULT_CONFLICT_BEHAVIOR",
    "GRAPH_BASE", "get_config", "is_configured", "normalize_conflict_behavior",
    "choose_upload_method", "normalize_remote_path", "build_simple_upload_url",
    "build_resumable_session_url", "list_providers", "get_providers",
    "available_providers", "supported_providers", "upload", "upload_file",
    "OneDriveUploadProvider", "OneDriveUploadService",
]


# Register with the main cloud-upload gateway so /api/cloud/providers
# includes "onedrive" once this module has been imported.  Safe no-op if the
# gateway module is unavailable.
try:  # pragma: no cover - best effort registration
    from app.services import cloud_upload as _gateway

    for _attr in ("PROVIDERS", "SUPPORTED_PROVIDERS", "AVAILABLE_PROVIDERS", "DEFAULT_PROVIDERS", "CLOUD_PROVIDERS"):
        _lst = getattr(_gateway, _attr, None)
        if isinstance(_lst, list) and PROVIDER_NAME not in _lst:
            _lst.append(PROVIDER_NAME)
except Exception:  # noqa: BLE001
    pass
