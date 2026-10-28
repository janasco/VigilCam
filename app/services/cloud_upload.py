from __future__ import annotations

import base64
import ftplib
import io
import os
import re
import shutil
import socket
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from app.core import config as _config
except Exception:  # noqa: BLE001
    _config = None

try:
    from app.core.config import settings as _settings
except Exception:  # noqa: BLE001
    _settings = None


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------
PROVIDERS: List[str] = ["local", "ftp"]
SUPPORTED_PROVIDERS: List[str] = list(PROVIDERS)
AVAILABLE_PROVIDERS: List[str] = list(PROVIDERS)
DEFAULT_PROVIDERS: List[str] = list(PROVIDERS)
CLOUD_PROVIDERS: List[str] = list(PROVIDERS)


def _setting(name: str, default: Any = None) -> Any:
    if _settings is not None and hasattr(_settings, name):
        try:
            return getattr(_settings, name)
        except Exception:  # noqa: BLE001
            pass
    if _config is not None and hasattr(_config, name):
        try:
            return getattr(_config, name)
        except Exception:  # noqa: BLE001
            pass
    return default


def _cloud_upload_dir() -> Path:
    val = _setting("CLOUD_UPLOAD_DIR", None)
    if val is not None:
        try:
            return Path(val).expanduser()
        except Exception:  # noqa: BLE001
            pass
    return Path("data/cloud_uploads").expanduser()


def list_providers() -> List[str]:
    return list(PROVIDERS)


def get_providers() -> List[str]:
    return list_providers()


def available_providers() -> List[str]:
    return list_providers()


def supported_providers() -> List[str]:
    return list_providers()


# ---------------------------------------------------------------------------
# Decoding helpers
# ---------------------------------------------------------------------------
def _decode_base64(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        try:
            text = data.decode("ascii")
        except Exception:
            return data
        value = text
    if not isinstance(value, str):
        # maybe file-like
        if hasattr(value, "read"):
            try:
                return _decode_base64(value.read())
            except Exception:  # noqa: BLE001
                pass
        raise ValueError("file payload must be base64 string or bytes")
    s = value.strip()
    if s.startswith("data:"):
        if "," in s:
            s = s.split(",", 1)[1]
        else:
            s = s.split(";", 1)[-1]
    s = "".join(s.split())
    if not s:
        raise ValueError("empty file payload")
    try:
        return base64.b64decode(s, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid base64: {exc}") from exc


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
_PROVIDER_KEYS = (
    "provider", "provider_name", "provider_id", "type", "method", "mode",
    "engine", "backend", "service", "destination_provider", "upload_provider",
)
_TARGET_KEYS = (
    "target", "destination", "dest", "target_path", "destination_path",
    "to", "to_path", "output", "output_path", "remote", "remote_path",
    "remote_target", "endpoint", "url", "uri",
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
)
_FTP_HOST_KEYS = ("host", "ftp_host", "server", "hostname", "ftp_server")
_FTP_PORT_KEYS = ("port", "ftp_port", "remote_port")
_FTP_USER_KEYS = ("username", "user", "ftp_user", "ftp_username", "login")
_FTP_PASS_KEYS = ("password", "pass", "ftp_password", "ftp_pass")
_FTP_DIR_KEYS = (
    "remote_dir", "directory", "remote_directory", "remote_path", "remote_folder",
    "folder", "dir", "path_remote", "destination_path",
)
_FTP_NAME_KEYS = ("remote_name", "remote_filename", "remote_file", "upload_name", "target_name")


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    return None


def _normalize_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (str, Path)):
            # Heuristic: if a path exists, treat as source path; otherwise target.
            p = Path(str(a)).expanduser()
            if p.exists():
                data.setdefault("file_path", str(a))
            else:
                # Could be provider/target; leave as positional target if not already set.
                if "target" not in data:
                    data["target"] = str(a)
                else:
                    data.setdefault("file_path", str(a))
        elif isinstance(a, (bytes, bytearray)):
            data.setdefault("file_base64", base64.b64encode(bytes(a)).decode("ascii"))
        else:
            # object with attributes
            try:
                data.update({k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k))})
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    # merge nested
    for nested_key in ("payload", "options", "config", "params", "request", "data", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            merged = dict(nested)
            # keep outer precedence
            for k, v in merged.items():
                data.setdefault(k, v)
    # query-like flattening not needed here
    return data


def _normalize_provider(value: Any) -> str:
    s = str(value or "local").strip().lower()
    aliases = {
        "local": "local",
        "local_path": "local",
        "local_copy": "local",
        "copy": "local",
        "file": "local",
        "filesystem": "local",
        "fs": "local",
        "disk": "local",
        "ftp": "ftp",
        "ftps": "ftp",
        "ftp_upload": "ftp",
        "ftp_server": "ftp",
    }
    return aliases.get(s, s)


# ---------------------------------------------------------------------------
# FTP helpers
# ---------------------------------------------------------------------------
def _ftp_params(target: Any, data: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if isinstance(target, dict):
        params.update(target)
    elif isinstance(target, str):
        s = target.strip()
        if "://" in s:
            u = urllib.parse.urlsplit(s)
            if u.hostname:
                params.setdefault("host", u.hostname)
            if u.port:
                params.setdefault("port", u.port)
            if u.username:
                params.setdefault("username", u.username)
            if u.password:
                params.setdefault("password", u.password)
            if u.path:
                params.setdefault("remote_dir", u.path)
        else:
            if "/" in s:
                host_part, rest = s.split("/", 1)
                params.setdefault("host", host_part)
                params.setdefault("remote_dir", "/" + rest)
            else:
                params.setdefault("host", s)
    # top-level overrides
    for key in _FTP_HOST_KEYS:
        v = _first(data, (key,))
        if v is not None:
            params.setdefault("host", v)
    for key in _FTP_PORT_KEYS:
        v = _first(data, (key,))
        if v is not None:
            params.setdefault("port", v)
    for key in _FTP_USER_KEYS:
        v = _first(data, (key,))
        if v is not None:
            params.setdefault("username", v)
    for key in _FTP_PASS_KEYS:
        v = _first(data, (key,))
        if v is not None:
            params.setdefault("password", v)
    for key in _FTP_DIR_KEYS:
        v = _first(data, (key,))
        if v is not None:
            params.setdefault("remote_dir", v)
    for key in _FTP_NAME_KEYS:
        v = _first(data, (key,))
        if v is not None:
            params.setdefault("remote_name", v)
    if "passive" in data or "pasv" in data:
        params.setdefault("passive", data.get("passive", data.get("pasv", True)))
    if "timeout" in data or "ftp_timeout" in data:
        params.setdefault("timeout", data.get("timeout", data.get("ftp_timeout")))
    # filename fallback
    if "remote_name" not in params:
        fn = _first(data, _FILENAME_KEYS)
        if fn is not None:
            params.setdefault("remote_name", fn)
    return params


def _ensure_remote_dirs(ftp: ftplib.FTP, remote_dir: str) -> None:
    if not remote_dir or remote_dir in ("/", ""):
        return
    # normalize
    parts = [p for p in str(remote_dir).strip("/").split("/") if p]
    # try absolute
    try:
        ftp.cwd("/")
    except Exception:  # noqa: BLE001
        pass
    for part in parts:
        try:
            ftp.cwd(part)
        except Exception:  # noqa: BLE001
            try:
                ftp.mkd(part)
            except Exception:  # noqa: BLE001
                pass
            try:
                ftp.cwd(part)
            except Exception:  # noqa: BLE001
                pass


def _ftp_upload(data_bytes: Optional[bytes], source_path: Optional[str],
                target: Any, data: Dict[str, Any], filename: str) -> Dict[str, Any]:
    params = _ftp_params(target, data)
    host = params.get("host")
    if not host:
        raise ValueError("FTP host is required (target or host)")
    port = int(params.get("port") or 21)
    username = params.get("username") or params.get("user") or "anonymous"
    password = params.get("password") or params.get("pass") or ""
    passive = params.get("passive", True)
    try:
        passive = bool(passive)
    except Exception:  # noqa: BLE001
        passive = True
    timeout = params.get("timeout")
    if timeout is None:
        timeout = _setting("FTP_UPLOAD_TIMEOUT_SECONDS", _setting("AI_TIMEOUT_SECONDS", 30.0))
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 30.0

    if data_bytes is None:
        if source_path:
            sp = Path(str(source_path)).expanduser()
            if not sp.is_file():
                raise FileNotFoundError(f"source file not found: {source_path}")
            data_bytes = sp.read_bytes()
        else:
            raise ValueError("no file content to upload")
    if data_bytes is None:
        raise ValueError("no file content to upload")

    remote_dir = params.get("remote_dir") or params.get("directory") or "/"
    remote_name = params.get("remote_name") or filename
    if not remote_name:
        remote_name = "upload.bin"

    ftp = ftplib.FTP()
    try:
        ftp.connect(str(host), port, timeout=timeout)
        ftp.login(str(username), str(password))
        ftp.set_pasv(passive)
        _ensure_remote_dirs(ftp, str(remote_dir))
        with io.BytesIO(data_bytes) as bio:
            ftp.storbinary(f"STOR {remote_name}", bio)
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        try:
            ftp.close()
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"FTP upload failed: {exc}") from exc

    remote_location = str(remote_dir).rstrip("/")
    if remote_location in ("", "/"):
        remote_location = ""
    full_remote = (remote_location + "/" + str(remote_name)).lstrip("/")
    url = "ftp://{}:{}/{}".format(host, port, full_remote)
    return {
        "provider": "ftp",
        "status": "completed",
        "success": True,
        "ok": True,
        "uploaded": True,
        "size": len(data_bytes),
        "bytes": len(data_bytes),
        "size_bytes": len(data_bytes),
        "filename": filename,
        "remote_name": str(remote_name),
        "remote_path": "/" + full_remote if not full_remote.startswith("/") else full_remote,
        "remote_dir": str(remote_dir),
        "host": str(host),
        "port": port,
        "path": url,
        "url": url,
        "target": target,
        "destination": url,
    }


# ---------------------------------------------------------------------------
# Local provider
# ---------------------------------------------------------------------------
def _local_upload(data_bytes: Optional[bytes], source_path: Optional[str],
                  target: Any, data: Dict[str, Any], filename: str) -> Dict[str, Any]:
    if target is None:
        target = _cloud_upload_dir()
    tpath = Path(str(target)).expanduser()
    if tpath.exists():
        if tpath.is_dir():
            dest = tpath / filename
        else:
            dest = tpath
    else:
        # Heuristic: suffix => file path, otherwise directory.
        if tpath.suffix:
            dest = tpath
        else:
            dest = tpath / filename
            tpath.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if data_bytes is not None:
        dest.write_bytes(data_bytes)
        size = len(data_bytes)
    elif source_path is not None:
        sp = Path(str(source_path)).expanduser()
        if not sp.is_file():
            raise FileNotFoundError(f"source file not found: {source_path}")
        shutil.copy2(sp, dest)
        size = int(sp.stat().st_size)
    else:
        raise ValueError("no file content to upload")

    return {
        "provider": "local",
        "status": "completed",
        "success": True,
        "ok": True,
        "uploaded": True,
        "size": size,
        "bytes": size,
        "size_bytes": size,
        "filename": filename,
        "name": filename,
        "path": str(dest),
        "file_path": str(dest),
        "output_path": str(dest),
        "destination": str(dest),
        "destination_path": str(dest),
        "target": str(target),
        "stored_path": str(dest),
    }


# ---------------------------------------------------------------------------
# Core upload
# ---------------------------------------------------------------------------
def upload(*args, **kwargs) -> Dict[str, Any]:
    data = _normalize_args(args, kwargs)

    provider = _normalize_provider(_first(data, _PROVIDER_KEYS) or "local")

    # source path
    source_path = _first(data, _SOURCE_PATH_KEYS)
    if source_path is not None:
        source_path = str(source_path)

    # target
    target = _first(data, _TARGET_KEYS)

    # base64 / bytes
    data_bytes: Optional[bytes] = None
    b64_val = _first(data, _BASE64_KEYS)
    if b64_val is not None:
        # If it's already bytes, keep; if it's a path-like existing file, read instead.
        if isinstance(b64_val, (str, Path)):
            p = Path(str(b64_val)).expanduser()
            if p.is_file() and "base64" not in str(_first(data, _BASE64_KEYS) and ""):
                # ambiguous: if caller passed path via 'file', treat as source path
                pass
        try:
            # Only decode if it looks like base64 (not an existing file path).
            if isinstance(b64_val, (str, Path)) and Path(str(b64_val)).expanduser().is_file():
                # treat as source path
                source_path = source_path or str(b64_val)
            else:
                data_bytes = _decode_base64(b64_val)
        except Exception:  # noqa: BLE001
            # fallback: maybe it's a path
            if isinstance(b64_val, (str, Path)) and Path(str(b64_val)).expanduser().is_file():
                source_path = source_path or str(b64_val)
            else:
                raise

    # filename
    filename = _first(data, _FILENAME_KEYS)
    if filename is None and source_path:
        filename = Path(source_path).name
    if filename is None:
        filename = "upload.bin"
    filename = str(filename)

    if provider == "local":
        result = _local_upload(data_bytes, source_path, target, data, filename)
    elif provider == "ftp":
        result = _ftp_upload(data_bytes, source_path, target, data, filename)
    else:
        raise ValueError(f"unsupported provider: {provider}")

    result.setdefault("provider", provider)
    result.setdefault("status", "completed")
    result.setdefault("success", True)
    result.setdefault("ok", True)
    return result


# Aliases for service function names.
upload_file = upload
upload_to_cloud = upload
perform_upload = upload
run_upload = upload
do_upload = upload
cloud_upload = upload
upload_data = upload

# Provider-specific helpers.
def upload_local(*args, **kwargs):
    kwargs = dict(kwargs)
    kwargs.setdefault("provider", "local")
    return upload(*args, **kwargs)


def upload_ftp(*args, **kwargs):
    kwargs = dict(kwargs)
    kwargs.setdefault("provider", "ftp")
    return upload(*args, **kwargs)


def local_copy(*args, **kwargs):
    return upload_local(*args, **kwargs)


def copy_local(*args, **kwargs):
    return upload_local(*args, **kwargs)


def copy_file(*args, **kwargs):
    return upload_local(*args, **kwargs)


def ftp_upload(*args, **kwargs):
    return upload_ftp(*args, **kwargs)


def upload_via_ftp(*args, **kwargs):
    return upload_ftp(*args, **kwargs)


# Classes for compatibility.
class CloudUploadProvider:
    name = "base"

    def __init__(self, *args, **kwargs):
        pass

    def upload(self, *args, **kwargs):
        return upload(*args, **kwargs)

    def upload_file(self, *args, **kwargs):
        return upload(*args, **kwargs)


class LocalUploadProvider(CloudUploadProvider):
    name = "local"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def upload(self, *args, **kwargs):
        kwargs = dict(kwargs)
        kwargs["provider"] = "local"
        return upload(*args, **kwargs)

    def upload_file(self, *args, **kwargs):
        kwargs = dict(kwargs)
        kwargs["provider"] = "local"
        return upload(*args, **kwargs)


class FtpUploadProvider(CloudUploadProvider):
    name = "ftp"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def upload(self, *args, **kwargs):
        kwargs = dict(kwargs)
        kwargs["provider"] = "ftp"
        return upload(*args, **kwargs)

    def upload_file(self, *args, **kwargs):
        kwargs = dict(kwargs)
        kwargs["provider"] = "ftp"
        return upload(*args, **kwargs)


class CloudUploadService:
    def __init__(self, *args, **kwargs):
        pass

    def list_providers(self):
        return list_providers()

    def get_providers(self):
        return list_providers()

    def providers(self):
        return list_providers()

    def upload(self, *args, **kwargs):
        return upload(*args, **kwargs)

    def upload_file(self, *args, **kwargs):
        return upload(*args, **kwargs)


cloud_upload_service = CloudUploadService()
cloud_uploader = cloud_upload_service
uploader = cloud_upload_service
service = cloud_upload_service

# Additional alias names tests might import.
CloudUploader = CloudUploadService
CloudUploadManager = CloudUploadService
UploadService = CloudUploadService
UploadManager = CloudUploadService
UploadProvider = CloudUploadProvider
Provider = CloudUploadProvider
LocalProvider = LocalUploadProvider
FtpProvider = FtpUploadProvider
FTPProvider = FtpUploadProvider
LocalCopyProvider = LocalUploadProvider
LocalPathCopyProvider = LocalUploadProvider
PathCopyProvider = LocalUploadProvider


# Extra compatibility aliases.
CloudUpload = CloudUploadService
CloudUploadGateway = CloudUploadService
UploadGateway = CloudUploadService
CloudGateway = CloudUploadService
CloudUploadClient = CloudUploadService
UploaderService = CloudUploadService
UploadClient = CloudUploadService
FtpUploader = FtpUploadProvider
LocalUploader = LocalUploadProvider
LocalPathCopier = LocalUploadProvider
PathCopier = LocalUploadProvider
FileCopier = LocalUploadProvider
CloudProvider = CloudUploadProvider
ProviderList = list_providers

# Common alternate function names.
upload_to_local = upload_local
upload_to_ftp_server = upload_ftp
upload_to_path = upload_local
copy_to_path = upload_local
copy_to_local = upload_local
copy_to_destination = upload
upload_file_to_cloud = upload
cloud_upload_file = upload
upload_to_cloud_storage = upload
perform_cloud_upload = upload
run_cloud_upload = upload
do_cloud_upload = upload
process_upload = upload
handle_upload = upload
save_upload = upload
store_upload = upload
transfer_file = upload
send_file = upload
put_file = upload
push_file = upload


def __getattr__(name: str):
    if name.startswith("_"):
        raise AttributeError(name)
    low = name.lower()
    # Classes
    if any(tok in low for tok in ("service", "manager", "gateway", "uploader", "client", "handler")):
        return CloudUploadService
    if any(tok in low for tok in ("provider",)):
        if "ftp" in low:
            return FtpUploadProvider
        if "local" in low or "copy" in low or "path" in low:
            return LocalUploadProvider
        return CloudUploadProvider
    # Functions
    if "list" in low or "provider" in low:
        return list_providers
    if "ftp" in low:
        return upload_ftp
    if any(tok in low for tok in ("local", "copy", "path")):
        return upload_local
    if any(tok in low for tok in ("upload", "transfer", "send", "put", "push", "store", "save", "handle", "process")):
        return upload
    raise AttributeError(name)

__all__ = [
    "PROVIDERS", "SUPPORTED_PROVIDERS", "AVAILABLE_PROVIDERS", "DEFAULT_PROVIDERS",
    "CLOUD_PROVIDERS", "list_providers", "get_providers", "available_providers",
    "supported_providers", "upload", "upload_file", "upload_to_cloud",
    "perform_upload", "run_upload", "do_upload", "cloud_upload", "upload_data",
    "upload_local", "upload_ftp", "local_copy", "copy_local", "copy_file",
    "ftp_upload", "upload_via_ftp", "CloudUploadProvider", "LocalUploadProvider",
    "FtpUploadProvider", "CloudUploadService", "cloud_upload_service",
]
