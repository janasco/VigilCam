from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings, BASE_DIR, DATA_DIR

_database_url = settings.DATABASE_URL
if _database_url.startswith("sqlite"):
    # Make relative sqlite paths resolve against the project root.
    if ":///" in _database_url and not _database_url.startswith("sqlite:////"):
        rel = _database_url.split(":///", 1)[1]
        abs_path = (BASE_DIR / rel).resolve()
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        _database_url = f"sqlite+aiosqlite:///{abs_path}"

engine = create_async_engine(_database_url, echo=settings.DEBUG, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass

# Ensure ORM models are registered with Base.metadata before create_all.
try:
    import app.models.auth  # noqa: E402,F401
except Exception:  # noqa: BLE001
    pass
try:
    import app.models.entities  # noqa: E402,F401
except Exception:  # noqa: BLE001
    pass


_ADD_COLUMNS = {
    "cameras": [
        ("name", "VARCHAR(120)"),
        ("kind", "VARCHAR(32)"),
        ("source", "VARCHAR(512)"),
        ("username", "VARCHAR(120)"),
        ("password", "VARCHAR(256)"),
        ("onvif_host", "VARCHAR(256)"),
        ("slug", "VARCHAR(140)"),
        ("ip_address", "VARCHAR(256)"),
        ("control_scheme", "VARCHAR(32)"),
        ("onvif_port", "INTEGER"),
        ("enabled", "BOOLEAN"),
        ("recording_enabled", "BOOLEAN"),
        ("detection_enabled", "BOOLEAN"),
        ("input_type", "VARCHAR(64) DEFAULT 'video'"),
        ("recording_mode", "VARCHAR(32) DEFAULT 'motion'"),
        ("detector_enabled", "BOOLEAN DEFAULT 1"),
        ("alert_enabled", "BOOLEAN DEFAULT 1"),
        ("stream_quality", "VARCHAR(32) DEFAULT 'medium'"),
        ("rotation", "INTEGER DEFAULT 0"),
        ("watermark", "VARCHAR(512)"),
        ("audio_enabled", "BOOLEAN DEFAULT 0"),
        ("storage_profile", "VARCHAR(120)"),
        ("width", "INTEGER"),
        ("height", "INTEGER"),
        ("fps", "INTEGER"),
        ("manufacturer", "VARCHAR(120)"),
        ("model", "VARCHAR(120)"),
        ("firmware_version", "VARCHAR(120)"),
        ("serial_number", "VARCHAR(120)"),
        ("hardware_id", "VARCHAR(120)"),
        ("group", "VARCHAR(120)"),
        ("group_id", "INTEGER"),
        ("last_activity_at", "DATETIME"),
        ("last_motion_at", "DATETIME"),
        ("created_at", "DATETIME"),
        ("motion_zones", "JSON"),
    ],
    "users": [
        ("email", "VARCHAR(256)"),
        ("hashed_password", "VARCHAR(256)"),
        ("is_active", "BOOLEAN"),
        ("is_superuser", "BOOLEAN"),
        ("created_at", "DATETIME"),
    ],
    "roles": [
        ("name", "VARCHAR(64)"),
        ("description", "VARCHAR(256)"),
        ("created_at", "DATETIME"),
    ],
    "groups": [
        ("slug", "VARCHAR(140)"),
    ],
    "layouts": [
        ("slug", "VARCHAR(140)"),
    ],
    "camera_capabilities": [
        ("onvif_endpoints", "JSON"),
        ("supported_areas", "JSON"),
        ("capabilities", "JSON"),
        ("probe_result", "VARCHAR(32)"),
        ("probe_error", "TEXT"),
        ("probed_at", "DATETIME"),
    ],
    "storage_areas": [
        ("name", "VARCHAR(255)"),
        ("path", "VARCHAR(1024)"),
        ("enabled", "BOOLEAN"),
        ("max_bytes", "BIGINT"),
        ("used_bytes", "BIGINT"),
        ("slug", "VARCHAR(140)"),
        ("created_at", "DATETIME"),
    ],
    "standing_rules": [
        ("sentence", "VARCHAR(512)"),
        ("target_object", "VARCHAR(120)"),
        ("attributes", "TEXT"),
        ("camera_id", "INTEGER"),
        ("slug", "VARCHAR(140)"),
        ("expires_at", "DATETIME"),
        ("is_active", "BOOLEAN"),
        ("created_at", "DATETIME"),
    ],
    "events": [
        ("camera_id", "INTEGER"),
        ("kind", "VARCHAR(64)"),
        ("label", "VARCHAR(120)"),
        ("confidence", "FLOAT"),
        ("payload", "TEXT"),
        ("tags", "JSON"),
        ("snapshot_path", "VARCHAR(1024)"),
        ("created_at", "DATETIME"),
    ],
}


async def _migrate_columns(conn):
    for table, cols in _ADD_COLUMNS.items():
        # table may not exist yet; create_all already ran
        for name, coltype in cols:
            try:
                await conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {coltype}'))
            except Exception:  # noqa: BLE001
                # already exists or table missing
                pass


async def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_columns(conn)
        # Backfill slugs for cameras created before slugs existed.
        try:
            from sqlalchemy import select as _select

            from app.models.entities import Camera as _Camera

            rows = (await conn.execute(_select(_Camera.id, _Camera.name, _Camera.slug))).all()
            used = {r[2] for r in rows if r[2]}
            for cid, name, slug in rows:
                if slug:
                    continue
                import re
                import unicodedata

                s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
                base = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "camera"
                cand, i = base, 2
                while cand in used:
                    cand, i = f"{base}-{i}", i + 1
                used.add(cand)
                await conn.execute(
                    text('UPDATE "cameras" SET "slug" = :slug WHERE "id" = :cid'),
                    {"slug": cand, "cid": cid},
                )
        except Exception:  # noqa: BLE001
            pass
        # Backfill slugs for groups and layouts.
        try:
            from app.models.entities import Group as _Group, Layout as _Layout

            for _model, _table in ((_Group, "groups"), (_Layout, "layouts")):
                rows = (await conn.execute(_select(_model.id, _model.name))).all()
                used = set()
                existing = (await conn.execute(_select(_model.slug))).all()
                used.update(r[0] for r in existing if r[0])
                for cid, name in rows:
                    import re as _re
                    import unicodedata as _ud

                    s = _ud.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
                    base = _re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "item"
                    cand, i = base, 2
                    while cand in used:
                        cand, i = f"{base}-{i}", i + 1
                    used.add(cand)
                    # only fill where null
                    cur = (await conn.execute(_select(_model.slug).where(_model.id == cid))).scalar()
                    if not cur:
                        await conn.execute(
                            text(f'UPDATE "{_table}" SET "slug" = :slug WHERE "id" = :cid'),
                            {"slug": cand, "cid": cid},
                        )
        except Exception:  # noqa: BLE001
            pass


_db_initialized: bool = False


async def _ensure_db_initialized() -> None:
    global _db_initialized
    if _db_initialized:
        return
    await init_db()
    _db_initialized = True


async def get_session() -> AsyncSession:
    await _ensure_db_initialized()
    async with SessionLocal() as session:
        yield session
