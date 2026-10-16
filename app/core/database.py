from __future__ import annotations

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


async def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
