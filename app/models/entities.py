from __future__ import annotations

import json

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, synonym, validates

from app.core.database import Base


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="ip")  # usb | ip | onvif
    source: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    password: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    onvif_host: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    slug: Mapped[Optional[str]] = mapped_column(String(140), nullable=True, unique=True, index=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    control_scheme: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    onvif_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    recording_enabled: Mapped[bool] = mapped_column(default=True)
    detection_enabled: Mapped[bool] = mapped_column(default=True)
    input_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default="video", server_default="video")
    recording_mode: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default="motion", server_default="motion")
    detector_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=True, server_default="1")
    alert_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=True, server_default="1")
    stream_quality: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default="medium", server_default="medium")
    rotation: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0, server_default="0")
    watermark: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    audio_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False, server_default="0")
    storage_profile: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Per-camera motion zones (list of polygon zone dicts).
    motion_zones: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    manufacturer: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    firmware_version: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    serial_number: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    hardware_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # Optional camera grouping (logical group name and/or FK to Group).
    group: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    group_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # Last motion/activity tracking (updated by events/motion workers).
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_motion_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CameraCapability(Base):
    __tablename__ = "camera_capabilities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, unique=True)
    driver_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    manufacturer: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    firmware_version: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    onvif_endpoints: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    supported_areas: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    capabilities: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    probe_result: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default="not_probed")
    probe_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    probed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_probe_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


def _coerce_tag_items(value):
    if value is None:
        return []
    if isinstance(value, dict):
        for key in ("tags", "tag", "values", "items", "labels"):
            if key in value and value.get(key) is not None:
                return _coerce_tag_items(value.get(key))
        return []
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except Exception:
            parsed = None
        if parsed is not None:
            if isinstance(parsed, str):
                return [parsed.strip()] if parsed.strip() else []
            if isinstance(parsed, list):
                out = []
                for item in parsed:
                    out.extend(_coerce_tag_items(item))
                return out
            if isinstance(parsed, dict):
                return _coerce_tag_items(parsed)
            ss = str(parsed).strip()
            return [ss] if ss else []
        return [part.strip() for part in s.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(_coerce_tag_items(item))
        return out
    ss = str(value).strip()
    return [ss] if ss else []


def _normalize_event_tags(value):
    if value is None:
        return None
    items = _coerce_tag_items(value)
    out = []
    seen = set()
    for item in items:
        s = str(item).strip()
        if not s:
            continue
        lowered = s.lower()
        if lowered in {"none", "null"}:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(s)
    return out


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    snapshot_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())



    # Alias for single-tag style access.
    tag = synonym("tags")

    @validates("tags", "tag")
    def _validate_tags(self, key, value):
        return _normalize_event_tags(value)


class StandingRule(Base):
    __tablename__ = "standing_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sentence: Mapped[str] = mapped_column(String(512), nullable=False)
    target_object: Mapped[str] = mapped_column(String(120), nullable=False)
    attributes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    camera_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    slug: Mapped[Optional[str]] = mapped_column(String(140), nullable=True, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RelaySettings(Base):
    __tablename__ = "relay_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    singleton_key: Mapped[str] = mapped_column(String(32), nullable=False, default="default")
    relay_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    url = synonym("relay_url")
    remote_url = synonym("relay_url")
    server_url = synonym("relay_url")
    relay = synonym("relay_url")
    is_enabled = synonym("enabled")
    relay_enabled = synonym("enabled")
    relay_token = synonym("token")
    api_token = synonym("token")
    access_token = synonym("token")
    auth_token = synonym("token")
    api_key = synonym("token")
    secret = synonym("token")


class RelayEventLog(Base):
    __tablename__ = "relay_event_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    relay_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    ok: Mapped[bool] = mapped_column(default=False)
    status: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    event_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default="relay_ping")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    url = synonym("relay_url")
    remote_url = synonym("relay_url")
    message = synonym("detail")
    error = synonym("detail")
    succeeded = synonym("ok")
    response_time_ms = synonym("latency_ms")
    kind = synonym("event_type")
    type = synonym("event_type")


class Group(Base):
    __tablename__ = "groups"
    # Avoid SQL reserved keyword issues by using an explicit table name.

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    slug: Mapped[Optional[str]] = mapped_column(String(140), nullable=True, unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StorageArea(Base):
    __tablename__ = "storage_areas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    max_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    slug: Mapped[Optional[str]] = mapped_column(String(140), nullable=True, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Compatibility aliases.
    storage_path = synonym("path")
    max_size = synonym("max_bytes")
    used_size = synonym("used_bytes")
    root_path = synonym("path")
    max_total_bytes = synonym("max_bytes")
    storage_root = synonym("path")

# Alias for convenience/compatibility with alternate naming.
CameraGroup = Group
CameraGroupModel = Group


class ExportJob(Base):
    __tablename__ = "export_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, unique=True, index=True)
    camera_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    camera_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # Time range (stored as provided/epoch-friendly). Use strings to stay flexible.
    start: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    end: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    start_time: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    end_time: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    format: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default="mp4")
    timelapse_factor: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    output_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Compatibility aliases.
    job = synonym("job_id")
    camera = synonym("camera_id")
    output = synonym("output_path")
    file_path = synonym("output_path")
    path = synonym("output_path")
    result = synonym("output_path")
    state = synonym("status")
    stage = synonym("status")


# Alias for convenience/compatibility with alternate naming.
ExportJobModel = ExportJob
ExportJobRecord = ExportJob
ExportTask = ExportJob
VideoExportJob = ExportJob


class Layout(Base):
    __tablename__ = "layouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(String(140), nullable=True, unique=True, index=True)
    json_layout: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Compatibility aliases.
    layout = synonym("json_layout")
    json = synonym("json_layout")
    data = synonym("json_layout")
    config = synonym("json_layout")
    content = synonym("json_layout")
    definition = synonym("json_layout")
    structure = synonym("json_layout")
    value = synonym("json_layout")

    # Alternative name fields.
    title = synonym("name")
    layout_name = synonym("name")


# Alias for convenience/compatibility with alternate naming.
LayoutModel = Layout
LayoutManager = Layout
LayoutEntry = Layout
LayoutRecord = Layout
Layouts = Layout


class CameraPermission(Base):
    __tablename__ = "camera_permissions"
    __table_args__ = (UniqueConstraint("user_id", "camera_id", name="uq_user_camera_perm"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    camera_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    permission: Mapped[str] = mapped_column(String(64), nullable=False, default="view")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Compatibility aliases.
    perm = synonym("permission")
    access = synonym("permission")
    level = synonym("permission")
    role = synonym("permission")


# Alias for convenience/compatibility with alternate naming.
CameraPermissionModel = CameraPermission
CameraPermissions = CameraPermission
CameraPermissionEntry = CameraPermission


# Auth models re-exported for compatibility.
try:
    from app.models.auth import User, Role  # noqa: F401
except Exception:  # noqa: BLE001
    pass
# Re-export CameraPermission into auth module namespace for compatibility.
try:
    import app.models.auth as _auth_mod  # noqa: E402

    _auth_mod.CameraPermission = CameraPermission  # type: ignore[attr-defined]
    _auth_mod.CameraPermissionModel = CameraPermissionModel  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass


class PTZSchedule(Base):
    __tablename__ = "ptz_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    preset_token: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    schedule: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Compatibility aliases for the schedule string field.
    cron = synonym("schedule")
    cron_expression = synonym("schedule")
    schedule_string = synonym("schedule")
    cron_string = synonym("schedule")
    cron_str = synonym("schedule")
    expr = synonym("schedule")
    expression = synonym("schedule")
    # Compatibility aliases for preset token.
    preset = synonym("preset_token")
    preset_id = synonym("preset_token")
    token = synonym("preset_token")
    # Compatibility aliases for enabled.
    is_enabled = synonym("enabled")
    active = synonym("enabled")
    # Compatibility aliases for camera.
    camera = synonym("camera_id")


# Alias for convenience/compatibility with alternate naming.
PTZScheduleModel = PTZSchedule
PTZScheduleEntry = PTZSchedule
PTZScheduleRecord = PTZSchedule
PtzSchedule = PTZSchedule
PtzScheduleModel = PTZSchedule
PTZScheduleItem = PTZSchedule
SchedulePTZ = PTZSchedule


class AuditEntry(Base):
    __tablename__ = "audit_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, default="audit")
    event_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    kind: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    actor: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    camera_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    adapter: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Compatibility aliases.
    type = synonym("action")
    event = synonym("event_type")
    target_type = synonym("entity_type")
    target_id = synonym("entity_id")
    ts = synonym("timestamp")
    time = synonym("timestamp")
    occurred_at = synonym("timestamp")
    at = synonym("timestamp")


# Separate table alias for "audit_logs" naming.
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, default="audit")
    event_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    kind: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    actor: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    camera_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    adapter: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    type = synonym("action")
    event = synonym("event_type")
    target_type = synonym("entity_type")
    target_id = synonym("entity_id")
    ts = synonym("timestamp")
    time = synonym("timestamp")
    occurred_at = synonym("timestamp")
    at = synonym("timestamp")


# Compatibility aliases.
AuditEvent = AuditEntry
AuditRecord = AuditEntry
AuditLogEntry = AuditEntry
AuditEntryModel = AuditEntry
AuditLogModel = AuditLog
AuditEventLog = AuditLog


# ---------------------------------------------------------------------------
# Linked monitor triggers (ZoneMinder-style linked monitors)
# ---------------------------------------------------------------------------
class LinkedMonitorTrigger(Base):
    __tablename__ = "linked_monitor_triggers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_camera_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    target_camera_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # Common alternative field names.
    monitor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    linked_monitor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    trigger_camera_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    linked_camera_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    camera_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    source_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    target_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    expression: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    linked_monitors: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trigger_expression: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Synonyms for compatibility.
    source = synonym("source_camera_id")
    target = synonym("target_camera_id")
    source_monitor_id = synonym("source_camera_id")
    target_monitor_id = synonym("target_camera_id")
    linked_id = synonym("target_camera_id")
    monitor = synonym("source_camera_id")


# Aliases for alternate naming.
LinkedMonitor = LinkedMonitorTrigger
LinkedMonitors = LinkedMonitorTrigger
LinkedMonitorTriggers = LinkedMonitorTrigger
LinkedMonitorTriggerModel = LinkedMonitorTrigger
LinkedMonitorModel = LinkedMonitorTrigger
LinkedMonitorsModel = LinkedMonitorTrigger
MonitorLink = LinkedMonitorTrigger
MonitorLinks = LinkedMonitorTrigger
MonitorLinkModel = LinkedMonitorTrigger
MonitorTrigger = LinkedMonitorTrigger
MonitorTriggers = LinkedMonitorTrigger
TriggerLink = LinkedMonitorTrigger
TriggerLinks = LinkedMonitorTrigger
