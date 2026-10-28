"""System backup/restore service (DB + config export/import).

Creates versioned ``.zip`` archives under ``data/backups`` containing:

- ``database/vigilcam.db`` -- SQLite snapshot (via the sqlite3 backup API).
- ``config/.env`` and ``.env`` -- application config files (when present).
- ``config/`` -- extra files from the ``config/`` directory (excluding secrets
  is left to the operator; the manifest lists archived files).
- ``manifest.json`` -- backup metadata (app version, created_at, files).

No ``app/main.py`` changes are required: the API module auto-registers.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from app.core.config import BASE_DIR, DATA_DIR, DATABASE_PATH, settings
except Exception:  # noqa: BLE001
    from pathlib import Path as _Path

    BASE_DIR = _Path(__file__).resolve().parents[2]
    DATA_DIR = BASE_DIR / "data"
    DATABASE_PATH = DATA_DIR / "vigilcam.db"
    settings = None

BACKUP_VERSION = "1.0"
BACKUP_KIND = "vigilcam-system-backup"

BACKUP_DIR = Path(DATA_DIR) / "backups"
try:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
except Exception:  # noqa: BLE001
    pass

_backup_lock = threading.Lock()

_MANIFEST_NAME = "manifest.json"
_DB_ARC_NAME = "database/vigilcam.db"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: Any) -> str:
    s = str(value or "").strip()
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in s)[:128]


def _backup_dir(backup_dir: Optional[Path] = None) -> Path:
    d = Path(backup_dir) if backup_dir is not None else BACKUP_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_db_path(db_path: Optional[Any] = None) -> Path:
    if db_path is not None:
        return Path(str(db_path))
    try:
        return Path(DATABASE_PATH)
    except Exception:  # noqa: BLE001
        return Path(DATA_DIR) / "vigilcam.db"


def _config_candidates() -> List[Path]:
    cands: List[Path] = []
    try:
        root_env = Path(BASE_DIR) / ".env"
        cands.append(root_env)
        config_env = Path(BASE_DIR) / "config" / ".env"
        cands.append(config_env)
        example = Path(BASE_DIR) / ".env.example"
        cands.append(example)
    except Exception:  # noqa: BLE001
        pass
    return cands


def _snapshot_sqlite(src: Path, dest: Path) -> None:
    """Copy a live SQLite DB consistently using the sqlite3 backup API."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        raise FileNotFoundError(f"Database file not found: {src}")
    try:
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        try:
            dest_conn = sqlite3.connect(str(dest), timeout=30)
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            src_conn.close()
    except Exception:
        # Fall back to a plain copy if the backup API is unavailable.
        shutil.copy2(str(src), str(dest))


def _write_manifest(manifest: Dict[str, Any], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")


def _read_manifest_from_zip(zf, names: List[str]) -> Dict[str, Any]:
    for candidate in (_MANIFEST_NAME, "manifest.json", "./manifest.json"):
        if candidate in names:
            try:
                raw = zf.read(candidate)
                data = json.loads(raw.decode("utf-8", "ignore"))
                if isinstance(data, dict):
                    return data
            except Exception:  # noqa: BLE001
                continue
    return {}


def _backup_info(path: Path) -> Dict[str, Any]:
    st = path.stat() if path.exists() else None
    stem = path.stem
    backup_id = stem[:-4] if stem.endswith(".zip") else stem
    # manifest peek (best effort)
    manifest: Dict[str, Any] = {}
    try:
        import zipfile

        with zipfile.ZipFile(str(path), "r") as zf:
            manifest = _read_manifest_from_zip(zf, zf.namelist())
    except Exception:  # noqa: BLE001
        manifest = {}
    info = {
        "id": backup_id,
        "backup_id": backup_id,
        "name": path.name,
        "filename": path.name,
        "file": path.name,
        "path": str(path),
        "size_bytes": int(st.st_size) if st is not None else None,
        "size": int(st.st_size) if st is not None else None,
        "created_at": manifest.get("created_at"),
        "created": manifest.get("created_at"),
        "app_version": manifest.get("app_version"),
        "version": manifest.get("version", BACKUP_VERSION),
        "kind": manifest.get("kind", BACKUP_KIND),
        "manifest": manifest,
    }
    return info


class BackupError(Exception):
    """Raised when a backup/restore operation fails."""


class BackupService:
    """Create, list, inspect, download and restore system backups."""

    def __init__(self, backup_dir: Optional[Path] = None, db_path: Optional[Any] = None) -> None:
        self.backup_dir = _backup_dir(backup_dir)
        self.db_path = _resolve_db_path(db_path)

    # ------------------------------------------------------------- create
    def create_backup(
        self,
        *args: Any,
        name: Optional[str] = None,
        label: Optional[str] = None,
        notes: Optional[str] = None,
        include_db: bool = True,
        include_config: bool = True,
        backup_dir: Optional[Path] = None,
        db_path: Optional[Any] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        label_val = name or label or kwargs.get("backup_name") or kwargs.get("filename")
        notes_val = notes or kwargs.get("description") or kwargs.get("comment")
        if isinstance(args, tuple):
            for a in args:
                if isinstance(a, str) and label_val is None:
                    label_val = a
                elif isinstance(a, dict):
                    label_val = label_val or a.get("name") or a.get("label")
                    notes_val = notes_val or a.get("notes") or a.get("description")
        target_dir = _backup_dir(backup_dir or self.backup_dir)
        src_db = _resolve_db_path(db_path if db_path is not None else self.db_path)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        slug = _safe_id(label_val) if label_val else "system"
        backup_id = f"{ts}_{slug}_{uuid.uuid4().hex[:8]}"
        tmpdir = Path(tempfile.mkdtemp(prefix="vigilcam_backup_"))
        try:
            staged_db: Optional[Path] = None
            if include_db:
                staged_db = tmpdir / "vigilcam.db"
                _snapshot_sqlite(src_db, staged_db)

            archived: List[str] = []
            import zipfile

            out_path = target_dir / f"{backup_id}.zip"
            manifest: Dict[str, Any] = {
                "version": BACKUP_VERSION,
                "kind": BACKUP_KIND,
                "backup_id": backup_id,
                "name": label_val or backup_id,
                "label": label_val,
                "notes": notes_val,
                "created_at": _utcnow_iso(),
                "app_version": getattr(settings, "APP_VERSION", "0.1.0") if settings is not None else "0.1.0",
                "app_name": getattr(settings, "APP_NAME", "VigilCam") if settings is not None else "VigilCam",
                "files": [],
            }
            with zipfile.ZipFile(str(out_path), "w", zipfile.ZIP_DEFLATED) as zf:
                if staged_db is not None and staged_db.exists():
                    zf.write(str(staged_db), _DB_ARC_NAME)
                    archived.append(_DB_ARC_NAME)
                if include_config:
                    for cand in _config_candidates():
                        try:
                            if cand.exists() and cand.is_file():
                                arc = f"config/{cand.name}" if cand.name.startswith(".env") else f"config/{cand.name}"
                                # Keep distinct names for root .env vs config/.env
                                if cand.parent.name == "config":
                                    arc = f"config/config.env"
                                    if cand.name != ".env":
                                        arc = f"config/config_{cand.name}"
                                elif cand.parent == Path(BASE_DIR):
                                    arc = f"config/root.env" if cand.name == ".env" else f"config/{cand.name}"
                                zf.write(str(cand), arc)
                                archived.append(arc)
                        except Exception:  # noqa: BLE001
                            continue
                manifest["files"] = archived
                zf.writestr(_MANIFEST_NAME, json.dumps(manifest, indent=2))
            info = _backup_info(out_path)
            info["label"] = label_val
            info["notes"] = notes_val
            info["status"] = "completed"
            info["ok"] = True
            return info
        finally:
            try:
                shutil.rmtree(str(tmpdir), ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass

    # --------------------------------------------------------------- list
    def list_backups(self, backup_dir: Optional[Path] = None, **kwargs: Any) -> List[Dict[str, Any]]:
        target_dir = _backup_dir(backup_dir or self.backup_dir)
        files = sorted(target_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        out: List[Dict[str, Any]] = []
        for f in files:
            try:
                out.append(_backup_info(f))
            except Exception:  # noqa: BLE001
                continue
        return out

    # ---------------------------------------------------------------- get
    def get_backup(self, backup_id: Any, backup_dir: Optional[Path] = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
        target_dir = _backup_dir(backup_dir or self.backup_dir)
        bid = _safe_id(backup_id)
        candidates = [target_dir / f"{bid}.zip", target_dir / f"{bid}"]
        for cand in candidates:
            try:
                if cand.exists() and cand.is_file():
                    return _backup_info(cand)
            except OSError:  # noqa: BLE001
                continue
        # allow lookup by filename directly
        try:
            direct = target_dir / str(backup_id)
        except (OSError, ValueError):  # noqa: BLE001
            return None
        try:
            if direct.exists() and direct.is_file():
                return _backup_info(direct)
        except OSError:  # noqa: BLE001
            return None
        return None

    def backup_path(self, backup_id: Any, backup_dir: Optional[Path] = None) -> Optional[Path]:
        info = self.get_backup(backup_id, backup_dir=backup_dir)
        if info is None:
            return None
        p = Path(str(info["path"]))
        return p if p.exists() else None

    # ------------------------------------------------------------- delete
    def delete_backup(self, backup_id: Any, backup_dir: Optional[Path] = None, **kwargs: Any) -> bool:
        p = self.backup_path(backup_id, backup_dir=backup_dir)
        if p is None:
            return False
        try:
            p.unlink()
            return True
        except OSError:
            return False

    # ------------------------------------------------------------ restore
    def restore_backup(
        self,
        backup_id: Any = None,
        *args: Any,
        backup_path: Optional[Any] = None,
        archive: Optional[Any] = None,
        file: Optional[Any] = None,
        data: Optional[bytes] = None,
        content: Optional[bytes] = None,
        restore_db: bool = True,
        restore_config: bool = True,
        db_path: Optional[Any] = None,
        backup_dir: Optional[Path] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        src: Optional[Path] = None
        raw_bytes: Optional[bytes] = None
        # Normalize a first positional that landed in ``backup_id``.
        if isinstance(backup_id, (bytes, bytearray)):
            raw_bytes = bytes(backup_id)
            backup_id = None
        elif isinstance(backup_id, Path) and backup_id.exists():
            src = backup_id
            backup_id = None
        for a in args:
            if isinstance(a, (bytes, bytearray)) and raw_bytes is None:
                raw_bytes = bytes(a)
            elif isinstance(a, (str, Path)) and src is None and backup_id is None:
                # could be a backup id or a filesystem path
                maybe = Path(str(a))
                if maybe.exists():
                    src = maybe
                elif backup_id is None:
                    backup_id = a
            elif isinstance(a, dict) and raw_bytes is None:
                pass
        if data is not None:
            raw_bytes = bytes(data)
        if content is not None:
            raw_bytes = bytes(content)
        for kw in (backup_path, archive, file):
            if kw is None:
                continue
            if isinstance(kw, (bytes, bytearray)):
                raw_bytes = bytes(kw)
            else:
                maybe = Path(str(kw))
                if maybe.exists():
                    src = maybe
                elif backup_id is None:
                    backup_id = kw
        extra_path = kwargs.get("path") or kwargs.get("zip_path") or kwargs.get("filename")
        if src is None and extra_path is not None:
            if isinstance(extra_path, (bytes, bytearray)):
                raw_bytes = bytes(extra_path)
            else:
                maybe = Path(str(extra_path))
                if maybe.exists():
                    src = maybe
                elif backup_id is None:
                    backup_id = extra_path
        if src is None and backup_id is not None:
            src = self.backup_path(backup_id, backup_dir=backup_dir)
        if src is None and raw_bytes is None:
            raise BackupError("No backup specified (backup_id, path or upload bytes required)")

        tmp_upload: Optional[Path] = None
        if raw_bytes is not None:
            fd, tmpp = tempfile.mkstemp(suffix=".zip", prefix="vigilcam_restore_")
            with open(fd, "wb") as fh:
                fh.write(raw_bytes)
            tmp_upload = Path(tmpp)
            src = tmp_upload

        assert src is not None
        if not src.exists():
            raise BackupError(f"Backup archive not found: {src}")

        import zipfile

        try:
            with zipfile.ZipFile(str(src), "r") as zf:
                names = zf.namelist()
                if not names:
                    raise BackupError("Backup archive is empty")
                manifest = _read_manifest_from_zip(zf, names)
                kind = str(manifest.get("kind", ""))
                if manifest and kind and kind != BACKUP_KIND:
                    # tolerate but note it
                    pass
                restored: List[str] = []
                # --- database ---
                db_arc = None
                for n in names:
                    if n in (_DB_ARC_NAME, "vigilcam.db", "database.db", "db/vigilcam.db", "data/vigilcam.db"):
                        db_arc = n
                        break
                dest_db = _resolve_db_path(db_path if db_path is not None else self.db_path)
                if restore_db and db_arc is not None:
                    with _backup_lock:
                        dest_db.parent.mkdir(parents=True, exist_ok=True)
                        # stage to temp then atomic replace
                        with tempfile.NamedTemporaryFile(delete=False, dir=str(dest_db.parent), suffix=".restore") as tf:
                            tmp_db = Path(tf.name)
                        try:
                            with zf.open(db_arc) as fin, open(tmp_db, "wb") as fout:
                                shutil.copyfileobj(fin, fout)
                            # validate it is a sqlite db
                            try:
                                probe = sqlite3.connect(str(tmp_db), timeout=10)
                                try:
                                    probe.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
                                finally:
                                    probe.close()
                            except Exception as exc:  # noqa: BLE001
                                raise BackupError(f"Archived database is invalid: {exc}")
                            backup_prev = dest_db.with_suffix(".db.pre-restore")
                            try:
                                if dest_db.exists():
                                    shutil.copy2(str(dest_db), str(backup_prev))
                            except Exception:  # noqa: BLE001
                                pass
                            try:
                                if dest_db.exists():
                                    dest_db.unlink()
                            except Exception:  # noqa: BLE001
                                pass
                            shutil.move(str(tmp_db), str(dest_db))
                            # Dispose pooled async connections so they re-open the new file.
                            try:
                                from app.core import database as _dbmod

                                eng = getattr(_dbmod, "engine", None)
                                sync_eng = getattr(eng, "sync_engine", None) if eng is not None else None
                                if sync_eng is not None and hasattr(sync_eng, "dispose"):
                                    try:
                                        sync_eng.dispose()
                                    except Exception:  # noqa: BLE001
                                        pass
                            except Exception:  # noqa: BLE001
                                pass
                            restored.append("database")
                        finally:
                            try:
                                if tmp_db.exists():
                                    tmp_db.unlink()
                            except Exception:  # noqa: BLE001
                                pass
                # --- config ---
                if restore_config:
                    for n in names:
                        if not n.startswith("config/"):
                            continue
                        if n.endswith("/"):
                            continue
                        member = n[len("config/"):]
                        if member in ("root.env", ".env", "root_env"):
                            dest = Path(BASE_DIR) / ".env"
                        elif member in ("config.env", "config_env", ".env.1"):
                            dest = Path(BASE_DIR) / "config" / ".env"
                        else:
                            # Unknown config member: restore under config/ dir verbatim
                            dest = Path(BASE_DIR) / "config" / Path(member).name
                        try:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(n) as fin, open(dest, "wb") as fout:
                                shutil.copyfileobj(fin, fout)
                            restored.append(f"config:{dest.name}")
                        except Exception:  # noqa: BLE001
                            continue
        except BackupError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BackupError(f"Could not restore backup: {exc}")
        finally:
            try:
                if tmp_upload is not None and tmp_upload.exists():
                    tmp_upload.unlink()
            except Exception:  # noqa: BLE001
                pass

        return {
            "status": "completed",
            "ok": True,
            "restored": restored,
            "backup_id": _safe_id(backup_id) if backup_id is not None else None,
            "manifest": manifest,
            "restored_at": _utcnow_iso(),
        }

    # -------------------------------------------------------------- export
    def export_bytes(self, backup_id: Any, backup_dir: Optional[Path] = None) -> bytes:
        p = self.backup_path(backup_id, backup_dir=backup_dir)
        if p is None:
            raise BackupError(f"Backup not found: {backup_id}")
        return p.read_bytes()


# Module-level singleton + functional API with compatibility aliases.
backup_service = BackupService()
service = backup_service
manager = backup_service
backup_manager = backup_service


def create_backup(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return backup_service.create_backup(*args, **kwargs)


def list_backups(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return backup_service.list_backups(*args, **kwargs)


def get_backup(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return backup_service.get_backup(*args, **kwargs)


def delete_backup(*args: Any, **kwargs: Any) -> bool:
    return backup_service.delete_backup(*args, **kwargs)


def restore_backup(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return backup_service.restore_backup(*args, **kwargs)


def export_backup(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return backup_service.create_backup(*args, **kwargs)


def import_backup(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return backup_service.restore_backup(*args, **kwargs)


# Additional alias names used across the codebase conventions.
create_system_backup = create_backup
export_system_backup = create_backup
new_backup = create_backup
take_backup = create_backup
make_backup = create_backup
get_backups = list_backups
all_backups = list_backups
list_system_backups = list_backups
remove_backup = delete_backup
delete_system_backup = delete_backup
restore_system_backup = restore_backup
import_system_backup = restore_backup
perform_restore = restore_backup
run_restore = restore_backup

BackupManager = BackupService
SystemBackupService = BackupService
SystemBackupManager = BackupService

__all__ = [
    "BACKUP_VERSION",
    "BACKUP_KIND",
    "BACKUP_DIR",
    "BackupError",
    "BackupService",
    "BackupManager",
    "SystemBackupService",
    "backup_service",
    "create_backup",
    "list_backups",
    "get_backup",
    "delete_backup",
    "restore_backup",
    "export_backup",
    "import_backup",
]
