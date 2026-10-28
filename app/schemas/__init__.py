from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator


class CameraBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    kind: str = Field("ip", pattern="^(usb|ip|onvif)$")
    source: str = Field(..., min_length=1, max_length=512)
    username: Optional[str] = None
    password: Optional[str] = None
    onvif_host: Optional[str] = None
    slug: Optional[str] = None
    ip_address: Optional[str] = None
    control_scheme: Optional[str] = None
    onvif_port: Optional[int] = None
    enabled: bool = True
    recording_enabled: bool = True
    detection_enabled: bool = True
    input_type: Optional[str] = "video"
    recording_mode: Optional[str] = "motion"
    detector_enabled: Optional[bool] = True
    alert_enabled: Optional[bool] = True
    stream_quality: Optional[str] = "medium"
    rotation: Optional[int] = 0
    watermark: Optional[str] = None
    audio_enabled: Optional[bool] = False
    storage_profile: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[int] = None
    group: Optional[str] = None
    group_id: Optional[int] = None
    motion_zones: Optional[Any] = None
    # alias
    motionZones: Optional[Any] = None


class CameraCreate(CameraBase):
    pass


class CameraRead(CameraBase):
    id: int
    created_at: datetime
    last_activity_at: Optional[datetime] = None
    last_motion_at: Optional[datetime] = None
    active: Optional[bool] = None

    model_config = {"from_attributes": True}


class EventRead(BaseModel):
    id: int
    camera_id: Optional[int]
    kind: str
    label: str
    confidence: Optional[float]
    payload: Optional[str]
    tags: Optional[List[str]] = None
    tag: Optional[List[str]] = None
    snapshot_path: Optional[str] = None
    created_at: datetime

    @field_validator("tags", "tag", mode="before")
    @classmethod
    def _normalize_tags(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            try:
                import json
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    v = parsed
                else:
                    return [parsed] if parsed is not None else []
            except Exception:
                v = [part.strip() for part in v.split(",") if part.strip()]
        if isinstance(v, (tuple, set)):
            v = list(v)
        if not isinstance(v, list):
            if v is None:
                return []
            return [str(v)]
        out = []
        seen = set()
        for item in v:
            s = str(item).strip() if item is not None else ""
            if not s:
                continue
            if s.lower() in {"none", "null"}:
                continue
            if s.lower() in seen:
                continue
            seen.add(s.lower())
            out.append(s)
        return out

    model_config = {"from_attributes": True}


class DetectionRequest(BaseModel):
    camera_id: Optional[int] = None
    image_base64: Optional[str] = None
    simulate: bool = True


class DetectionResult(BaseModel):
    camera_id: Optional[int]
    model: str
    detections: list[dict]
    processed_at: datetime


class RecordingRead(BaseModel):
    id: int
    camera_id: int
    path: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    size_bytes: Optional[int] = None

    model_config = {"from_attributes": True}


class RecordingStartRequest(BaseModel):
    camera_id: Optional[Any] = None
    main_url: str = Field(..., min_length=1)
    sub_url: Optional[str] = None


class RecordingStopRequest(BaseModel):
    camera_name: str = Field(..., min_length=1)


class RetentionRunRequest(BaseModel):
    dry_run: bool = False
    max_age_days: Optional[int] = None
    max_total_bytes: Optional[int] = None
    camera_name: Optional[str] = None
    camera_id: Optional[Any] = None


class StandingRuleCreate(BaseModel):
    sentence: str = Field(..., min_length=1)
    camera_id: Optional[Any] = None


class StandingRuleRead(BaseModel):
    id: int
    sentence: str
    target_object: str
    attributes: Optional[str] = None
    camera_id: Optional[int] = None
    expires_at: datetime
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Motion polygon zones (schema mirror of app.services.motion_zoning.PolygonZone)
# ---------------------------------------------------------------------------
class PolygonPoint(BaseModel):
    x: float
    y: float


class PolygonZoneSchema(BaseModel):
    points: list[Any] = Field(default_factory=list)
    mode: str = "include"
    threshold_percentage: float = 5.0
    threshold: Optional[float] = None
    threshold_percent: Optional[float] = None
    name: Optional[str] = None
    label: Optional[str] = None


# Back-compat aliases
MotionPolygonZoneSchema = PolygonZoneSchema
MotionZoneSchema = PolygonZoneSchema
ZonePolygonSchema = PolygonZoneSchema


# ---------------------------------------------------------------------------
# Export jobs
# ---------------------------------------------------------------------------
class ExportRequest(BaseModel):
    camera_id: Optional[Any] = None
    camera: Optional[Any] = None
    cam: Optional[Any] = None
    start: Optional[Any] = None
    start_time: Optional[Any] = None
    end: Optional[Any] = None
    end_time: Optional[Any] = None
    format: Optional[str] = "mp4"
    fmt: Optional[str] = None
    output_format: Optional[str] = None
    type: Optional[str] = None
    timelapse_factor: Optional[float] = None
    timelapse: Optional[float] = None
    speed: Optional[float] = None
    factor: Optional[float] = None

    model_config = {"extra": "allow"}


class ExportJobCreate(ExportRequest):
    pass


class ExportJobRead(BaseModel):
    id: Optional[Any] = None
    job_id: Optional[Any] = None
    camera_id: Optional[Any] = None
    camera: Optional[Any] = None
    camera_name: Optional[str] = None
    start: Optional[Any] = None
    end: Optional[Any] = None
    start_time: Optional[Any] = None
    end_time: Optional[Any] = None
    format: Optional[str] = None
    timelapse_factor: Optional[float] = None
    status: Optional[str] = None
    state: Optional[str] = None
    progress: Optional[float] = None
    output_path: Optional[str] = None
    output: Optional[str] = None
    path: Optional[str] = None
    file_path: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True, "extra": "allow"}


# Compatibility aliases
ExportCreate = ExportRequest
ExportJob = ExportJobRead
ExportJobCreateRequest = ExportRequest
ExportJobRequest = ExportRequest
ExportStartRequest = ExportRequest
ExportResponse = ExportJobRead
ExportJobResponse = ExportJobRead
ExportStatus = ExportJobRead
ExportJobStatus = ExportJobRead
ExportRead = ExportJobRead


# ---------------------------------------------------------------------------
# ONVIF PTZ
# ---------------------------------------------------------------------------
class OnvifConnectionParams(BaseModel):
    ip: Optional[str] = None
    host: Optional[str] = None
    username: str = ""
    password: str = ""
    port: Optional[int] = None
    onvif_port: Optional[int] = None
    onvif_host: Optional[str] = None
    scheme: Optional[str] = None
    control_scheme: Optional[str] = None
    control_port: Optional[int] = None

    def resolved_ip(self) -> str:
        return self.ip or self.host or self.onvif_host or ""

    def resolved_port(self):
        return self.port or self.onvif_port or self.control_port

    def resolved_scheme(self):
        return self.scheme or self.control_scheme


def _conn_ip(p: "OnvifPtzRequestBase") -> str:
    return p.ip or p.host or ""


class OnvifPtzRequestBase(OnvifConnectionParams):
    profile_token: Optional[str] = None
    profile: Optional[str] = None
    profile_id: Optional[str] = None

    def resolved_profile(self) -> str:
        return self.profile_token or self.profile or self.profile_id or ""


class OnvifPtzContinuousMoveRequest(OnvifPtzRequestBase):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    pan: Optional[float] = None
    tilt: Optional[float] = None
    zoom: Optional[float] = None
    velocity_x: Optional[float] = None
    velocity_y: Optional[float] = None
    velocity_z: Optional[float] = None

    def resolved_velocity(self):
        x = self.x if self.pan is None and self.velocity_x is None else (self.pan if self.pan is not None else (self.velocity_x if self.velocity_x is not None else self.x))
        y = self.y if self.tilt is None and self.velocity_y is None else (self.tilt if self.tilt is not None else (self.velocity_y if self.velocity_y is not None else self.y))
        z = self.z if self.zoom is None and self.velocity_z is None else (self.zoom if self.zoom is not None else (self.velocity_z if self.velocity_z is not None else self.z))
        return float(x), float(y), float(z)


class OnvifPtzStopRequest(OnvifPtzRequestBase):
    pan_tilt: bool = True
    zoom: bool = True


class OnvifPtzGotoPresetRequest(OnvifPtzRequestBase):
    preset_token: str = Field(..., min_length=1)
    preset: Optional[str] = None
    preset_id: Optional[str] = None

    def resolved_preset(self) -> str:
        return self.preset_token or self.preset or self.preset_id or ""


class OnvifPtzGetPresetsRequest(OnvifPtzRequestBase):
    pass


class OnvifPtzSetPresetRequest(OnvifPtzRequestBase):
    name: str = "Preset"
    preset_name: Optional[str] = None
    preset_token: Optional[str] = None
    preset_id: Optional[str] = None
    token: Optional[str] = None

    def resolved_name(self) -> str:
        return self.preset_name or self.name or "Preset"

    def resolved_token(self) -> Optional[str]:
        return self.preset_token or self.preset_id or self.token


# Response models
class OnvifPtzResponse(BaseModel):
    status: Optional[str] = None
    result: Optional[Any] = None
    data: Optional[Any] = None
    token: Optional[str] = None
    name: Optional[str] = None
    preset: Optional[str] = None
    presets: Optional[list[dict]] = None


# Aliases for various naming
PtzContinuousMoveRequest = OnvifPtzContinuousMoveRequest
ContinuousMoveRequest = OnvifPtzContinuousMoveRequest
PtzStopRequest = OnvifPtzStopRequest
StopRequest = OnvifPtzStopRequest
PtzGotoPresetRequest = OnvifPtzGotoPresetRequest
GotoPresetRequest = OnvifPtzGotoPresetRequest
PtzGetPresetsRequest = OnvifPtzGetPresetsRequest
GetPresetsRequest = OnvifPtzGetPresetsRequest
PtzSetPresetRequest = OnvifPtzSetPresetRequest
SetPresetRequest = OnvifPtzSetPresetRequest
PTZContinuousMoveRequest = OnvifPtzContinuousMoveRequest
PTZStopRequest = OnvifPtzStopRequest
PTZGotoPresetRequest = OnvifPtzGotoPresetRequest
PTZGetPresetsRequest = OnvifPtzGetPresetsRequest
PTZSetPresetRequest = OnvifPtzSetPresetRequest
PtzStopRequest = OnvifPtzStopRequest
PtzGotoPresetRequest = OnvifPtzGotoPresetRequest
PtzGetPresetsRequest = OnvifPtzGetPresetsRequest
PtzSetPresetRequest = OnvifPtzSetPresetRequest
PtzResponse = OnvifPtzResponse
PTZContinuousMoveRequest = OnvifPtzContinuousMoveRequest
PTZStopRequest = OnvifPtzStopRequest
PTZGotoPresetRequest = OnvifPtzGotoPresetRequest
PTZGetPresetsRequest = OnvifPtzGetPresetsRequest
PTZSetPresetRequest = OnvifPtzSetPresetRequest
PTZResponse = OnvifPtzResponse


# ---------------------------------------------------------------------------
# Authentication / RBAC
# ---------------------------------------------------------------------------
class RoleRead(BaseModel):
    id: int
    name: str
    description: Optional[str] = None

    model_config = {"from_attributes": True}


class UserRead(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
    is_active: bool = True
    is_superuser: bool = False
    roles: List["RoleRead"] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class Register(BaseModel):
    username: str = Field(..., min_length=1, max_length=120)
    password: str = Field(..., min_length=1)
    email: Optional[str] = None
    role: Optional[str] = None


class Login(BaseModel):
    username: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------------------------------------------------------------------------
# Camera groups (minimal)
# ---------------------------------------------------------------------------
class GroupBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    slug: Optional[str] = None
    description: Optional[str] = None


class GroupCreate(GroupBase):
    pass


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None


class GroupRead(GroupBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


# Aliases for compatibility with various naming conventions.
CameraGroupBase = GroupBase
CameraGroupCreate = GroupCreate
CameraGroupRead = GroupRead
CameraGroupUpdate = GroupUpdate
Group = GroupRead
Groups = GroupRead

# Aliases for compatibility with various naming conventions.
RegisterRequest = Register
LoginRequest = Login
TokenResponse = Token
UserResponse = UserRead
RoleResponse = RoleRead
RoleCreate = RoleRead


# ---------------------------------------------------------------------------
# Storage areas (Wave 18)
# ---------------------------------------------------------------------------
class StorageAreaBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    path: str = Field(..., min_length=1, max_length=1024)
    enabled: bool = True
    max_bytes: Optional[int] = None
    used_bytes: Optional[int] = 0
    # aliases accepted
    storage_path: Optional[str] = None
    root_path: Optional[str] = None
    max_size: Optional[int] = None
    max_total_bytes: Optional[int] = None
    used_size: Optional[int] = None
    storage_root: Optional[str] = None


class StorageAreaCreate(StorageAreaBase):
    pass


class StorageAreaUpdate(BaseModel):
    name: Optional[str] = None
    path: Optional[str] = None
    enabled: Optional[bool] = None
    max_bytes: Optional[int] = None
    used_bytes: Optional[int] = None
    storage_path: Optional[str] = None
    root_path: Optional[str] = None
    max_size: Optional[int] = None
    max_total_bytes: Optional[int] = None
    used_size: Optional[int] = None
    storage_root: Optional[str] = None


class StorageAreaRead(BaseModel):
    id: int
    name: str
    path: str
    enabled: bool = True
    max_bytes: Optional[int] = None
    used_bytes: int = 0
    created_at: Optional[datetime] = None
    # computed/audit helpers
    scanned_files: Optional[int] = None
    file_count: Optional[int] = None
    total_bytes: Optional[int] = None
    free_bytes: Optional[int] = None

    model_config = {"from_attributes": True}


# Aliases for compatibility with various naming conventions.
StorageArea = StorageAreaRead
StorageAreaModel = StorageAreaRead
StorageRead = StorageAreaRead
StorageAreaSummary = StorageAreaRead
StorageAreas = StorageAreaRead


# ---------------------------------------------------------------------------
# Camera permissions (Wave 21)
# ---------------------------------------------------------------------------
class CameraPermissionBase(BaseModel):
    user_id: int
    camera_id: Optional[int] = None
    permission: str = Field("view", min_length=1, max_length=64)
    # aliases
    perm: Optional[str] = None
    access: Optional[str] = None
    level: Optional[str] = None
    role: Optional[str] = None
    user: Optional[int] = None
    camera: Optional[int] = None

    def resolved_user_id(self) -> int:
        return self.user_id if self.user_id is not None else int(self.user)

    def resolved_camera_id(self) -> Optional[int]:
        return self.camera_id if self.camera_id is not None else self.camera

    def resolved_permission(self) -> str:
        for v in (self.permission, self.perm, self.access, self.level, self.role):
            if v is not None and str(v).strip():
                return str(v).strip()
        return "view"


class CameraPermissionCreate(CameraPermissionBase):
    pass


class CameraPermissionUpdate(BaseModel):
    permission: Optional[str] = None
    perm: Optional[str] = None
    access: Optional[str] = None
    level: Optional[str] = None
    role: Optional[str] = None

    def resolved_permission(self) -> Optional[str]:
        for v in (self.permission, self.perm, self.access, self.level, self.role):
            if v is not None and str(v).strip():
                return str(v).strip()
        return None


class CameraPermissionRead(BaseModel):
    id: Optional[int] = None
    user_id: int
    camera_id: int
    permission: str
    created_at: Optional[datetime] = None
    # aliases
    perm: Optional[str] = None
    access: Optional[str] = None
    level: Optional[str] = None

    model_config = {"from_attributes": True}


# Aliases for compatibility with various naming conventions.
CameraPermission = CameraPermissionRead
CameraPermissionModel = CameraPermissionRead
CameraPermissions = CameraPermissionRead
CameraPermissionSchema = CameraPermissionRead
CameraPermissionEntry = CameraPermissionRead
PermissionRead = CameraPermissionRead
CameraPermissionResponse = CameraPermissionRead


# ---------------------------------------------------------------------------
# PTZ schedules (Wave 26)
# ---------------------------------------------------------------------------
class PTZScheduleBase(BaseModel):
    camera_id: Optional[int] = None
    camera: Optional[int] = None
    cam: Optional[int] = None
    cameraId: Optional[int] = None
    preset_token: Optional[str] = None
    preset: Optional[str] = None
    preset_id: Optional[str] = None
    presetId: Optional[str] = None
    token: Optional[str] = None
    schedule: Optional[str] = None
    cron: Optional[str] = None
    cron_expression: Optional[str] = None
    schedule_string: Optional[str] = None
    cron_string: Optional[str] = None
    cron_str: Optional[str] = None
    expr: Optional[str] = None
    expression: Optional[str] = None
    enabled: Optional[bool] = True
    active: Optional[bool] = None
    is_enabled: Optional[bool] = None
    name: Optional[str] = None

    def resolved_camera_id(self) -> Optional[int]:
        for v in (self.camera_id, self.camera, self.cam, self.cameraId):
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return v
        return None

    def resolved_preset_token(self) -> Optional[str]:
        for v in (self.preset_token, self.preset, self.preset_id, self.presetId, self.token):
            if v is not None and str(v).strip():
                return str(v).strip()
        return None

    def resolved_schedule(self) -> Optional[str]:
        for v in (self.schedule, self.cron, self.cron_expression, self.schedule_string,
                  self.cron_string, self.cron_str, self.expr, self.expression):
            if v is not None and str(v).strip():
                return str(v).strip()
        return None

    def resolved_enabled(self) -> bool:
        for v in (self.enabled, self.active, self.is_enabled):
            if v is not None:
                return bool(v)
        return True


class PTZScheduleCreate(PTZScheduleBase):
    pass


class PTZScheduleUpdate(BaseModel):
    camera_id: Optional[int] = None
    camera: Optional[int] = None
    cam: Optional[int] = None
    cameraId: Optional[int] = None
    preset_token: Optional[str] = None
    preset: Optional[str] = None
    preset_id: Optional[str] = None
    presetId: Optional[str] = None
    token: Optional[str] = None
    schedule: Optional[str] = None
    cron: Optional[str] = None
    cron_expression: Optional[str] = None
    schedule_string: Optional[str] = None
    cron_string: Optional[str] = None
    cron_str: Optional[str] = None
    expr: Optional[str] = None
    expression: Optional[str] = None
    enabled: Optional[bool] = None
    active: Optional[bool] = None
    is_enabled: Optional[bool] = None
    name: Optional[str] = None

    def resolved_camera_id(self) -> Optional[int]:
        for v in (self.camera_id, self.camera, self.cam, self.cameraId):
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return v
        return None

    def resolved_preset_token(self) -> Optional[str]:
        for v in (self.preset_token, self.preset, self.preset_id, self.presetId, self.token):
            if v is not None and str(v).strip():
                return str(v).strip()
        return None

    def resolved_schedule(self) -> Optional[str]:
        for v in (self.schedule, self.cron, self.cron_expression, self.schedule_string,
                  self.cron_string, self.cron_str, self.expr, self.expression):
            if v is not None and str(v).strip():
                return str(v).strip()
        return None

    def resolved_enabled(self) -> Optional[bool]:
        for v in (self.enabled, self.active, self.is_enabled):
            if v is not None:
                return bool(v)
        return None


class PTZScheduleRead(BaseModel):
    id: Optional[int] = None
    camera_id: Optional[int] = None
    camera: Optional[int] = None
    preset_token: Optional[str] = None
    preset: Optional[str] = None
    preset_id: Optional[str] = None
    token: Optional[str] = None
    schedule: Optional[str] = None
    cron: Optional[str] = None
    cron_expression: Optional[str] = None
    schedule_string: Optional[str] = None
    cron_string: Optional[str] = None
    enabled: Optional[bool] = None
    active: Optional[bool] = None
    is_enabled: Optional[bool] = None
    name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True, "extra": "allow"}

    def model_post_init(self, __context):
        # Normalize from ORM object: ensure all alias fields populated.
        try:
            if self.camera_id is None and getattr(self, "camera", None) is not None:
                self.camera_id = self.camera
            if self.camera is None and self.camera_id is not None:
                self.camera = self.camera_id
            if self.preset_token is None:
                for a in ("preset", "preset_id", "token"):
                    v = getattr(self, a, None)
                    if v is not None:
                        self.preset_token = v
                        break
            for a in ("preset", "preset_id", "token"):
                if getattr(self, a, None) is None:
                    setattr(self, a, self.preset_token)
            if self.schedule is None:
                for a in ("cron", "cron_expression", "schedule_string", "cron_string"):
                    v = getattr(self, a, None)
                    if v is not None:
                        self.schedule = v
                        break
            for a in ("cron", "cron_expression", "schedule_string", "cron_string"):
                if getattr(self, a, None) is None:
                    setattr(self, a, self.schedule)
            if self.enabled is None:
                for a in ("active", "is_enabled"):
                    v = getattr(self, a, None)
                    if v is not None:
                        self.enabled = v
                        break
            for a in ("active", "is_enabled"):
                if getattr(self, a, None) is None:
                    setattr(self, a, self.enabled)
        except Exception:  # noqa: BLE001
            pass


# Aliases for compatibility with various naming conventions.
PTZSchedule = PTZScheduleRead
PTZScheduleModel = PTZScheduleRead
PTZScheduleSchema = PTZScheduleRead
PTZScheduleResponse = PTZScheduleRead
PTZScheduleEntry = PTZScheduleRead
PtzSchedule = PTZScheduleRead
PtzScheduleRead = PTZScheduleRead
PtzScheduleCreate = PTZScheduleCreate
PtzScheduleUpdate = PTZScheduleUpdate
PtzScheduleBase = PTZScheduleBase
PTZScheduleRead = PTZScheduleRead
PTZScheduleCreate = PTZScheduleCreate
PTZScheduleUpdate = PTZScheduleUpdate
PTZScheduleBase = PTZScheduleBase


# ---------------------------------------------------------------------------
# Layouts (Wave 27)
# ---------------------------------------------------------------------------
class LayoutBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    slug: Optional[str] = None
    json_layout: Optional[Any] = None
    # aliases accepted
    layout: Optional[Any] = None
    json: Optional[Any] = None
    data: Optional[Any] = None
    config: Optional[Any] = None
    content: Optional[Any] = None
    definition: Optional[Any] = None
    structure: Optional[Any] = None
    value: Optional[Any] = None
    title: Optional[str] = None
    layout_name: Optional[str] = None
    # Floor-plan camera positions (Wave 50)
    camera_positions: Optional[Any] = None
    cameraPositions: Optional[Any] = None
    cameras: Optional[Any] = None
    positions: Optional[Any] = None
    markers: Optional[Any] = None

    def resolved_name(self) -> str:
        for v in (self.name, self.title, self.layout_name):
            if v is not None and str(v).strip():
                return str(v).strip()
        return self.name

    def resolved_layout(self) -> Optional[Any]:
        for v in (self.json_layout, self.layout, self.json, self.data, self.config,
                  self.content, self.definition, self.structure, self.value):
            if v is not None:
                return v
        for v in (self.camera_positions, self.cameraPositions, self.cameras, self.positions,
                  self.markers):
            if v is not None:
                return {"camera_positions": v}
        return None


class LayoutCreate(LayoutBase):
    pass


class LayoutUpdate(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    layout_name: Optional[str] = None
    slug: Optional[str] = None
    json_layout: Optional[Any] = None
    layout: Optional[Any] = None
    json: Optional[Any] = None
    data: Optional[Any] = None
    config: Optional[Any] = None
    content: Optional[Any] = None
    definition: Optional[Any] = None
    structure: Optional[Any] = None
    value: Optional[Any] = None
    camera_positions: Optional[Any] = None
    cameraPositions: Optional[Any] = None
    cameras: Optional[Any] = None
    positions: Optional[Any] = None
    markers: Optional[Any] = None

    def resolved_name(self) -> Optional[str]:
        for v in (self.name, self.title, self.layout_name):
            if v is not None and str(v).strip():
                return str(v).strip()
        return self.name

    def resolved_layout(self) -> Optional[Any]:
        for v in (self.json_layout, self.layout, self.json, self.data, self.config,
                  self.content, self.definition, self.structure, self.value):
            if v is not None:
                return v
        for v in (self.camera_positions, self.cameraPositions, self.cameras, self.positions,
                  self.markers):
            if v is not None:
                return {"camera_positions": v}
        # distinguish unset vs explicit None? keep None
        return None


class LayoutRead(BaseModel):
    id: Optional[int] = None
    name: Optional[str] = None
    title: Optional[str] = None
    layout_name: Optional[str] = None
    slug: Optional[str] = None
    json_layout: Optional[Any] = None
    layout: Optional[Any] = None
    json: Optional[Any] = None
    data: Optional[Any] = None
    config: Optional[Any] = None
    content: Optional[Any] = None
    definition: Optional[Any] = None
    structure: Optional[Any] = None
    value: Optional[Any] = None
    camera_positions: Optional[Any] = None
    cameraPositions: Optional[Any] = None
    cameras: Optional[Any] = None
    positions: Optional[Any] = None
    markers: Optional[Any] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True, "extra": "allow"}

    def model_post_init(self, __context):
        try:
            if self.name is None:
                for a in ("title", "layout_name"):
                    v = getattr(self, a, None)
                    if v is not None:
                        self.name = v
                        break
            for a in ("title", "layout_name"):
                if getattr(self, a, None) is None:
                    setattr(self, a, self.name)
            if self.json_layout is None:
                for a in ("layout", "json", "data", "config", "content", "definition", "structure", "value"):
                    v = getattr(self, a, None)
                    if v is not None:
                        self.json_layout = v
                        break
            for a in ("layout", "json", "data", "config", "content", "definition", "structure", "value"):
                if getattr(self, a, None) is None:
                    setattr(self, a, self.json_layout)
            # Wave 50: surface camera positions from layout JSON
            try:
                jl = self.json_layout
                if isinstance(jl, str):
                    import json as _j
                    jl = _j.loads(jl)
                if isinstance(jl, dict):
                    for key in ("camera_positions", "cameraPositions", "cameras", "positions", "markers"):
                        if getattr(self, "camera_positions", None) is None and jl.get(key) is not None:
                            self.camera_positions = jl.get(key)
                            break
                for a in ("cameraPositions", "cameras", "positions", "markers"):
                    if getattr(self, a, None) is None:
                        setattr(self, a, self.camera_positions)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass


# Aliases for compatibility with various naming conventions.
Layout = LayoutRead
LayoutModel = LayoutRead
LayoutSchema = LayoutRead
LayoutResponse = LayoutRead
LayoutEntry = LayoutRead
LayoutRecord = LayoutRead
Layouts = LayoutRead


class AuditEntryCreate(BaseModel):
    action: Optional[str] = None
    event_type: Optional[str] = None
    type: Optional[str] = None
    kind: Optional[str] = None
    event: Optional[str] = None
    actor: Optional[str] = None
    user: Optional[str] = None
    username: Optional[str] = None
    user_id: Optional[int] = None
    entity_type: Optional[str] = None
    entity_id: Optional[Any] = None
    target_type: Optional[str] = None
    target_id: Optional[Any] = None
    details: Optional[Any] = None
    message: Optional[str] = None
    metadata: Optional[Any] = None
    meta: Optional[Any] = None
    data: Optional[Any] = None
    payload: Optional[Any] = None
    context: Optional[Any] = None
    extra: Optional[Any] = None
    extras: Optional[Any] = None
    camera_id: Optional[Any] = None
    adapter: Optional[str] = None
    correlation_id: Optional[str] = None
    correlation: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    timestamp: Optional[datetime] = None
    ts: Optional[Any] = None
    created_at: Optional[datetime] = None
    occurred_at: Optional[Any] = None

    model_config = {"extra": "allow", "populate_by_name": True}


class AuditEntryRead(BaseModel):
    id: Optional[int] = None
    action: Optional[str] = None
    event_type: Optional[str] = None
    type: Optional[str] = None
    kind: Optional[str] = None
    event: Optional[str] = None
    actor: Optional[str] = None
    user: Optional[str] = None
    username: Optional[str] = None
    user_id: Optional[int] = None
    entity_type: Optional[str] = None
    entity_id: Optional[Any] = None
    target_type: Optional[str] = None
    target_id: Optional[Any] = None
    details: Optional[Any] = None
    message: Optional[str] = None
    metadata: Optional[Any] = None
    meta: Optional[Any] = None
    camera_id: Optional[Any] = None
    adapter: Optional[str] = None
    correlation_id: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    timestamp: Optional[datetime] = None
    ts: Optional[Any] = None
    created_at: Optional[datetime] = None
    occurred_at: Optional[Any] = None

    model_config = {"from_attributes": True, "extra": "allow", "populate_by_name": True}


# Aliases.
AuditEntryResponse = AuditEntryRead
AuditEntryList = List[AuditEntryRead]
AuditLogCreate = AuditEntryCreate
AuditLogRead = AuditEntryRead
AuditLogResponse = AuditEntryRead
AuditLogList = List[AuditEntryRead]
AuditEventCreate = AuditEntryCreate
AuditEventRead = AuditEntryRead
AuditEventResponse = AuditEntryRead
AuditEntriesResponse = List[AuditEntryRead]
AuditLogsResponse = List[AuditEntryRead]
AuditEventsResponse = List[AuditEntryRead]


# ---------------------------------------------------------------------------
# PTZ tracking (Wave PTZ tracking)
# ---------------------------------------------------------------------------
class PTZTrackingBase(BaseModel):
    camera_id: Optional[int] = None
    camera: Optional[int] = None
    cam: Optional[int] = None
    cameraId: Optional[int] = None
    target: Optional[Any] = None
    object: Optional[Any] = None
    detection: Optional[Any] = None
    bbox: Optional[Any] = None
    box: Optional[Any] = None
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    zoom: Optional[float] = None
    pan: Optional[float] = None
    tilt: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None
    frame_width: Optional[float] = None
    frame_height: Optional[float] = None
    gain: Optional[float] = None
    deadzone: Optional[float] = None
    move: Optional[bool] = None
    enabled: Optional[bool] = None
    active: Optional[bool] = None
    running: Optional[bool] = None
    tracking: Optional[bool] = None
    ip: Optional[str] = None
    host: Optional[str] = None
    onvif_host: Optional[str] = None
    username: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    port: Optional[int] = None
    onvif_port: Optional[int] = None
    control_port: Optional[int] = None
    profile_token: Optional[str] = None
    profile: Optional[str] = None
    profile_id: Optional[str] = None
    id: Optional[Any] = None
    tracking_id: Optional[Any] = None
    session_id: Optional[Any] = None
    track_id: Optional[Any] = None
    tracker_id: Optional[Any] = None
    name: Optional[str] = None
    status: Optional[str] = None
    action: Optional[str] = None
    op: Optional[str] = None
    operation: Optional[str] = None

    def resolved_camera_id(self) -> Optional[int]:
        for v in (self.camera_id, self.camera, self.cam, self.cameraId):
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return v
        return None

    def resolved_target(self) -> Optional[Any]:
        for v in (self.target, self.object, self.detection, self.bbox, self.box):
            if v is not None:
                return v
        return None


class PTZTrackingCreate(PTZTrackingBase):
    pass


class PTZTrackingUpdate(BaseModel):
    camera_id: Optional[int] = None
    camera: Optional[int] = None
    cam: Optional[int] = None
    cameraId: Optional[int] = None
    target: Optional[Any] = None
    object: Optional[Any] = None
    detection: Optional[Any] = None
    bbox: Optional[Any] = None
    box: Optional[Any] = None
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    zoom: Optional[float] = None
    pan: Optional[float] = None
    tilt: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None
    frame_width: Optional[float] = None
    frame_height: Optional[float] = None
    gain: Optional[float] = None
    deadzone: Optional[float] = None
    move: Optional[bool] = None
    enabled: Optional[bool] = None
    active: Optional[bool] = None
    running: Optional[bool] = None
    tracking: Optional[bool] = None
    ip: Optional[str] = None
    host: Optional[str] = None
    onvif_host: Optional[str] = None
    username: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    port: Optional[int] = None
    onvif_port: Optional[int] = None
    control_port: Optional[int] = None
    profile_token: Optional[str] = None
    profile: Optional[str] = None
    profile_id: Optional[str] = None
    id: Optional[Any] = None
    tracking_id: Optional[Any] = None
    session_id: Optional[Any] = None
    track_id: Optional[Any] = None
    tracker_id: Optional[Any] = None
    name: Optional[str] = None
    status: Optional[str] = None
    action: Optional[str] = None
    op: Optional[str] = None
    operation: Optional[str] = None

    def resolved_camera_id(self) -> Optional[int]:
        for v in (self.camera_id, self.camera, self.cam, self.cameraId):
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return v
        return None

    def resolved_target(self) -> Optional[Any]:
        for v in (self.target, self.object, self.detection, self.bbox, self.box):
            if v is not None:
                return v
        return None


class PTZTrackingRead(BaseModel):
    id: Optional[Any] = None
    tracking_id: Optional[Any] = None
    session_id: Optional[Any] = None
    track_id: Optional[Any] = None
    tracker_id: Optional[Any] = None
    camera_id: Optional[int] = None
    camera: Optional[int] = None
    cam: Optional[int] = None
    target: Optional[Any] = None
    object: Optional[Any] = None
    detection: Optional[Any] = None
    bbox: Optional[Any] = None
    box: Optional[Any] = None
    center: Optional[Any] = None
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    zoom: Optional[float] = None
    pan: Optional[float] = None
    tilt: Optional[float] = None
    velocity: Optional[Any] = None
    ptz_velocity: Optional[Any] = None
    velocity_x: Optional[float] = None
    velocity_y: Optional[float] = None
    velocity_z: Optional[float] = None
    status: Optional[str] = None
    state: Optional[str] = None
    tracking: Optional[bool] = None
    active: Optional[bool] = None
    running: Optional[bool] = None
    enabled: Optional[bool] = None
    is_tracking: Optional[bool] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None

    model_config = {"from_attributes": True, "extra": "allow"}


# Aliases
PTZTracking = PTZTrackingRead
PTZTrackingSchema = PTZTrackingRead
PTZTrackingModel = PTZTrackingRead
PTZTrackingResponse = PTZTrackingRead
PTZTrackingEntry = PTZTrackingRead
PTZTracker = PTZTrackingRead
PTZTrackerSchema = PTZTrackingRead
PTZTrackerRead = PTZTrackingRead
PTZTrackerCreate = PTZTrackingCreate
PTZTrackerUpdate = PTZTrackingUpdate
PTZTrackerBase = PTZTrackingBase
TrackingRead = PTZTrackingRead
TrackingCreate = PTZTrackingCreate
TrackingUpdate = PTZTrackingUpdate
TrackingBase = PTZTrackingBase
Tracking = PTZTrackingRead
PTZTrack = PTZTrackingRead
PTZTrackRead = PTZTrackingRead
PTZTrackCreate = PTZTrackingCreate
PTZTrackUpdate = PTZTrackingUpdate
PTZTrackBase = PTZTrackingBase
TrackerRead = PTZTrackingRead
TrackerCreate = PTZTrackingCreate
TrackerUpdate = PTZTrackingUpdate
TrackerBase = PTZTrackingBase
Tracker = PTZTrackingRead
PTZTrackingRequest = PTZTrackingBase
PTZTrackingResult = PTZTrackingRead
