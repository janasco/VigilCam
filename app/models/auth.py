from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Table, Column, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

try:
    from app.models.entities import CameraPermission, CameraPermissionModel  # noqa: F401
except Exception:  # noqa: BLE001
    pass


user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    users: Mapped[List["User"]] = relationship(
        "User",
        secondary=user_roles,
        back_populates="roles",
        lazy="selectin",
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, unique=True)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    roles: Mapped[List[Role]] = relationship(
        "Role",
        secondary=user_roles,
        back_populates="users",
        lazy="selectin",
    )

    # Convenience aliases for password field.
    @property
    def password_hash(self) -> str:
        return self.hashed_password

    @password_hash.setter
    def password_hash(self, value: str) -> None:
        self.hashed_password = value
