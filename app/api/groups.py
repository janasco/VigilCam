from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.models.entities import Group, Camera
from app.schemas import GroupCreate, GroupRead, GroupUpdate

router = APIRouter(tags=["groups"])


def _group_to_read(g: Group) -> GroupRead:
    return GroupRead.model_validate(g)


@router.get("/api/groups")
@router.get("/api/camera-groups")
@router.get("/api/cameras/groups")
async def list_groups(db: AsyncSession = Depends(db_session)):
    result = await db.execute(select(Group).order_by(Group.id))
    return [GroupRead.model_validate(g) for g in result.scalars().all()]


@router.post("/api/groups", status_code=status.HTTP_201_CREATED)
@router.post("/api/camera-groups", status_code=status.HTTP_201_CREATED)
@router.post("/api/cameras/groups", status_code=status.HTTP_201_CREATED)
async def create_group(payload: GroupCreate, db: AsyncSession = Depends(db_session)):
    from app.services.slugs import ensure_unique_slug, slugify_name

    slug = payload.slug or slugify_name(payload.name)
    slug = await ensure_unique_slug(db, Group, slug)
    group = Group(name=payload.name, description=payload.description, slug=slug)
    db.add(group)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Group already exists")
    await db.refresh(group)
    return GroupRead.model_validate(group)


@router.get("/api/groups/by-slug/{slug}")
async def get_group_by_slug(slug: str, db: AsyncSession = Depends(db_session)):
    from app.services.slugs import get_by_slug

    group = await get_by_slug(db, Group, slug)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return GroupRead.model_validate(group)


@router.get("/api/groups/{group_id}")
@router.get("/api/camera-groups/{group_id}")
async def get_group(group_id: int, db: AsyncSession = Depends(db_session)):
    group = await db.get(Group, group_id)
    if not group:
        # allow lookup by name? id only
        raise HTTPException(status_code=404, detail="Group not found")
    return GroupRead.model_validate(group)


@router.patch("/api/groups/{group_id}")
@router.patch("/api/camera-groups/{group_id}")
async def update_group(group_id: int, payload: GroupUpdate, db: AsyncSession = Depends(db_session)):
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    data = payload.model_dump(exclude_unset=True)
    old_name = group.name
    if data.get("slug"):
        from app.services.slugs import ensure_unique_slug, slugify_name

        data["slug"] = await ensure_unique_slug(db, Group, slugify_name(data["slug"]), exclude_id=group_id)
    elif data.get("name") is not None and data.get("name") != old_name and not group.slug:
        from app.services.slugs import ensure_unique_slug, slugify_name

        data["slug"] = await ensure_unique_slug(db, Group, slugify_name(data["name"]), exclude_id=group_id)
    for key, value in data.items():
        if value is not None or key in data:
            if hasattr(group, key):
                setattr(group, key, value)
    # Keep camera.group text in sync when the group is renamed.
    if "name" in data and data.get("name") is not None and data.get("name") != old_name:
        new_name = data.get("name")
        result = await db.execute(select(Camera).where((Camera.group_id == group_id) | (Camera.group == old_name)))
        for cam in result.scalars().all():
            cam.group = new_name
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Group name conflict")
    await db.refresh(group)
    return GroupRead.model_validate(group)


@router.delete("/api/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/api/camera-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(group_id: int, db: AsyncSession = Depends(db_session)):
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    # Unassign cameras from this group.
    result = await db.execute(select(Camera).where(Camera.group_id == group_id))
    for cam in result.scalars().all():
        cam.group_id = None
        # keep textual group? clear too
        if cam.group == group.name:
            cam.group = None
    await db.delete(group)
    await db.commit()
    return None


@router.get("/api/groups/{group_id}/cameras")
@router.get("/api/camera-groups/{group_id}/cameras")
async def group_cameras(group_id: int, db: AsyncSession = Depends(db_session)):
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    result = await db.execute(select(Camera).where((Camera.group_id == group_id) | (Camera.group == group.name)).order_by(Camera.id))
    from app.schemas import CameraRead
    return [CameraRead.model_validate(c) for c in result.scalars().all()]


# Convenience assignment endpoints.
@router.post("/api/cameras/{camera_id}/group")
async def assign_camera_group(camera_id: int, payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
    from fastapi import Body
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    data = {}
    if payload is not None:
        if hasattr(payload, "model_dump"):
            data = payload.model_dump()
        elif isinstance(payload, dict):
            data = payload
    # Support plain body.
    group_id = data.get("group_id") if isinstance(data, dict) else None
    group_name = data.get("group") if isinstance(data, dict) else None
    if group_id is None and isinstance(payload, dict):
        group_id = payload.get("group_id")
        group_name = payload.get("group", group_name)
    if group_id is not None:
        g = await db.get(Group, int(group_id))
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        camera.group_id = g.id
        camera.group = g.name
    elif group_name is not None:
        # find or create group by name
        res = await db.execute(select(Group).where(Group.name == str(group_name)))
        g = res.scalars().first()
        if g is None:
            g = Group(name=str(group_name))
            db.add(g)
            await db.commit()
            await db.refresh(g)
        camera.group_id = g.id
        camera.group = g.name
    await db.commit()
    await db.refresh(camera)
    from app.schemas import CameraRead
    return CameraRead.model_validate(camera)


@router.delete("/api/cameras/{camera_id}/group", status_code=200)
async def unassign_camera_group(camera_id: int, db: AsyncSession = Depends(db_session)):
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    camera.group_id = None
    camera.group = None
    await db.commit()
    await db.refresh(camera)
    from app.schemas import CameraRead
    return CameraRead.model_validate(camera)