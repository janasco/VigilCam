from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import db_session
from app.models.auth import Role, User, user_roles
from app.schemas import Login, Register, Token, UserRead
from app.services.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


async def _get_or_create_role(db: AsyncSession, name: str) -> Role:
    result = await db.execute(select(Role).options(selectinload(Role.users)).where(Role.name == name))
    role = result.scalars().first()
    if role is None:
        role = Role(name=name)
        db.add(role)
        await db.flush()
    return role


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(payload: Register, db: AsyncSession = Depends(db_session)):
    # username uniqueness
    existing = await db.execute(select(User).where(User.username == payload.username))
    if existing.scalars().first() is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already exists")

    role_name = getattr(payload, "role", None) or "user"

    existing_count = len((await db.execute(select(User))).scalars().all())

    user = User(
        username=payload.username,
        email=getattr(payload, "email", None),
        hashed_password=hash_password(payload.password),
        is_active=True,
        is_superuser=(existing_count == 0),
    )
    db.add(user)
    await db.flush()

    role = await _get_or_create_role(db, role_name)

    await db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))

    await db.commit()
    result = await db.execute(select(User).options(selectinload(User.roles)).where(User.id == user.id))
    return result.scalars().first()


@router.post("/login", response_model=Token)
async def login(
    request: Request,
    db: AsyncSession = Depends(db_session),
    username: Optional[str] = Form(default=None),
    password: Optional[str] = Form(default=None),
):
    # Support JSON body as well.
    if username is None or password is None:
        try:
            body = await request.json()
            if isinstance(body, dict):
                username = username or body.get("username")
                password = password or body.get("password")
        except Exception:  # noqa: BLE001
            pass
    if not username or not password:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="username and password required")

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalars().first()
    if user is None or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Inactive user")

    token = create_access_token({"sub": user.username})
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me", response_model=UserRead)
async def me(current_user: User = Depends(get_current_user)):
    return current_user
