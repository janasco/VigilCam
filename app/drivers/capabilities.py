# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
Capability probe — talk to the camera, record what its driver can read/drive,
and refresh the device metadata on the Camera row.

Persisted to the ``camera_capabilities`` table (result + probed_at, mirroring
the transport-security probe). Never mutates the camera's IP/network — this is
a read-only interrogation that only writes metadata (manufacturer/model/fw)
back locally.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.entities import Camera, CameraCapability

from .registry import get_driver_for_camera


def _is_async(db) -> bool:
    return isinstance(db, AsyncSession)


async def _get_or_create_row(db, camera_id: int) -> CameraCapability:
    if _is_async(db):
        result = await db.execute(
            select(CameraCapability).where(CameraCapability.camera_id == camera_id)
        )
        row = result.scalars().first()
    else:
        row = (
            db.query(CameraCapability)
            .filter(CameraCapability.camera_id == camera_id)
            .first()
        )
    if row is None:
        row = CameraCapability(camera_id=camera_id, probe_result="not_probed")
        db.add(row)
        if _is_async(db):
            await db.flush()
        else:
            db.flush()
    return row


async def _get_camera(db, camera_id: int):
    if _is_async(db):
        return await db.get(Camera, camera_id)
    return db.get(Camera, camera_id)


async def probe_and_store(db, camera_id: int) -> CameraCapability:
    """Probe the camera and upsert its capability row. Always returns a row
    (with ``probe_result`` reflecting success/failure) rather than raising for
    an unreachable camera, so the UI can show a 'refresh' affordance."""
    camera = await _get_camera(db, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    row = await _get_or_create_row(db, camera_id)
    try:
        driver = await get_driver_for_camera(db, camera_id, force=True)
        info = await driver.get_info()
        caps = await driver.get_capabilities()

        row.driver_name = driver.driver_name
        row.manufacturer = info.manufacturer
        row.model = info.model
        row.firmware_version = info.firmware_version
        row.onvif_endpoints = caps.onvif_endpoints
        row.supported_areas = caps.supported_areas
        row.capabilities = caps.detail
        row.probe_result = "ok"
        row.probe_error = None

        # Refresh device METADATA on the camera (never touches IP/network).
        if info.manufacturer:
            camera.manufacturer = info.manufacturer
        if info.model:
            camera.model = info.model
        if info.firmware_version:
            camera.firmware_version = info.firmware_version
        if getattr(info, "serial_number", None):
            camera.serial_number = info.serial_number
        if getattr(info, "hardware_id", None):
            camera.hardware_id = info.hardware_id
    except HTTPException as e:
        row.probe_result = (
            "unreachable" if e.status_code in (503, 504) else "error"
        )
        row.probe_error = str(e.detail)[:500]
    except Exception as e:  # never let a probe crash the request
        row.probe_result = "error"
        row.probe_error = f"{type(e).__name__}: {e}"[:500]

    now = datetime.now(UTC)
    row.probed_at = now
    row.last_probe_at = now

    try:
        if _is_async(db):
            await db.commit()
            await db.refresh(row)
            await db.refresh(camera)
        else:
            db.commit()
            db.refresh(row)
            db.refresh(camera)
    except Exception:  # noqa: BLE001
        pass
    return row


async def get_or_probe(db, camera_id: int) -> CameraCapability:
    """Return the cached capability row, probing once if never probed."""
    if _is_async(db):
        result = await db.execute(
            select(CameraCapability).where(CameraCapability.camera_id == camera_id)
        )
        row = result.scalars().first()
    else:
        row = (
            db.query(CameraCapability)
            .filter(CameraCapability.camera_id == camera_id)
            .first()
        )
    if row is None or row.probe_result in (None, "not_probed"):
        return await probe_and_store(db, camera_id)
    return row
