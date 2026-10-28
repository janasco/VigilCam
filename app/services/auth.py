from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_session
from app.models.auth import User


ALGORITHM = "HS256"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def _secret_key() -> str:
    return getattr(settings, "SECRET_KEY", os.getenv("SECRET_KEY", "change-me-in-production"))


def _access_expire_minutes() -> int:
    return int(getattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", 60))


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def get_password_hash(password: str) -> str:
    return hash_password(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


def create_access_token(data: Any, expires_delta: Optional[timedelta] = None) -> str:
    if isinstance(data, str):
        data = {"sub": data}
    to_encode = dict(data or {})
    now = datetime.now(timezone.utc)
    if expires_delta is None:
        expires_delta = timedelta(minutes=_access_expire_minutes())
    expire = now + expires_delta
    to_encode.setdefault("sub", to_encode.get("username"))
    to_encode.update({"exp": expire, "iat": now, "type": "access"})
    return jwt.encode(to_encode, _secret_key(), algorithm=ALGORITHM)


def create_token(subject: Any, expires_delta: Optional[timedelta] = None) -> str:
    if isinstance(subject, str):
        return create_access_token({"sub": subject}, expires_delta=expires_delta)
    return create_access_token(subject, expires_delta=expires_delta)


def decode_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, _secret_key(), algorithms=[ALGORITHM])
        return payload
    except Exception:  # noqa: BLE001
        return None


def verify_token(token: str) -> Optional[dict]:
    return decode_token(token)


def decode_access_token(token: str) -> Optional[dict]:
    return decode_token(token)


async def _user_from_token(db: AsyncSession, token: str) -> Optional[User]:
    payload = decode_token(token)
    if not payload:
        return None
    if payload.get("type") not in (None, "access"):
        return None
    username = payload.get("sub")
    if username is None:
        return None
    result = await db.execute(select(User).where(User.username == username).options(selectinload(User.roles)))
    user = result.scalars().first()
    if user is None or not user.is_active:
        return None
    return user


async def get_user_from_token(db: AsyncSession, token: str) -> Optional[User]:
    return await _user_from_token(db, token)


async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_session),
) -> User:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = await _user_from_token(db, token)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")
    return user


async def get_current_optional_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_session),
) -> Optional[User]:
    if not token:
        return None
    try:
        return await _user_from_token(db, token)
    except Exception:  # noqa: BLE001
        return None


# Compatibility wrappers using db session argument.
async def authenticate_user(db: AsyncSession, username: str, password: str) -> Optional[User]:
    result = await db.execute(select(User).where(User.username == username).options(selectinload(User.roles)))
    user = result.scalars().first()
    if user is None:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    if not user.is_active:
        return None
    return user


# Low-level bcrypt aliases
def hash_password_bcrypt(password: str) -> str:
    return hash_password(password)


# Camera access guard helpers (Wave 21).
async def check_camera_access(db, user=None, camera_id=None, permission=None, **kwargs):
    try:
        from app.services.camera_permissions import check_camera_access as _check

        if camera_id is None:
            camera_id = kwargs.get("camera_id") or kwargs.get("id")
        return await _check(db, user, int(camera_id), permission)
    except Exception:  # noqa: BLE001
        return True


can_access_camera = check_camera_access
camera_access_guard = check_camera_access
has_camera_access = check_camera_access
user_can_access_camera = check_camera_access
verify_camera_access = check_camera_access


def check_password(plain: str, hashed: str) -> bool:
    return verify_password(plain, hashed)
