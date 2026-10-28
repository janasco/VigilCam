from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import BASE_DIR, DATA_DIR, RECORDING_DIR, SNAPSHOT_DIR
import threading

from app.core.database import init_db
from app.core.state import is_initialized, mark_initialized
from app.core.discovery import start_broadcast, stop_broadcast
try:
    from app.services.scheduler import start_retention_scheduler
except Exception:  # noqa: BLE001
    start_retention_scheduler = None
from app.api.auth import router as auth_router
from app.api.cameras import router as cameras_router
from app.api.recordings import router as recordings_router
from app.api.drivers import router as drivers_router
from app.api.ai import router as ai_router
from app.api.ai_settings import router as ai_settings_router
from app.api.onvif import router as onvif_router
from app.api.onvif_driver import router as onvif_driver_router
from app.api.onvif_ptz import router as onvif_ptz_router
try:
    from app.api.ptz_schedules import router as ptz_schedules_router
except Exception:  # noqa: BLE001
    ptz_schedules_router = None
try:
    from app.api.ptz import router as ptz_router
except Exception:  # noqa: BLE001
    ptz_router = None
from app.api.system import router as system_router
from app.api.rules import router as rules_router
from app.api.relay import router as relay_router
try:
    from app.api.actions import router as actions_router
except Exception:  # noqa: BLE001
    actions_router = None
from app.api.events import router as events_router
from app.api.voice import router as voice_router
try:
    from app.api.groups import router as groups_router
except Exception:  # noqa: BLE001
    groups_router = None
try:
    from app.api.layouts import router as layouts_router
except Exception:  # noqa: BLE001
    layouts_router = None
try:
    from app.api.media import router as media_router
except Exception:  # noqa: BLE001
    media_router = None
try:
    from app.api.floorplans import router as floorplans_router
except Exception:  # noqa: BLE001
    floorplans_router = None
try:
    from app.api.storage import router as storage_router
except Exception:  # noqa: BLE001
    storage_router = None
try:
    from app.api.cloud_upload import router as cloud_upload_router
except Exception:  # noqa: BLE001
    cloud_upload_router = None
try:
    from app.api.export import router as export_router
except Exception:  # noqa: BLE001
    export_router = None
try:
    from app.api.streams import router as streams_router
except Exception:  # noqa: BLE001
    streams_router = None
try:
    from app.api.motion import router as motion_router
except Exception:  # noqa: BLE001
    motion_router = None
try:
    from app.api.motion import router_motion_zoning as motion_zoning_router
except Exception:  # noqa: BLE001
    motion_zoning_router = None
try:
    from app.api.motion import router_motion_dash as motion_zoning_dash_router
except Exception:  # noqa: BLE001
    motion_zoning_dash_router = None
try:
    from app.api.sources import router as sources_router, camera_sources_router
except Exception:  # noqa: BLE001
    sources_router = None
    camera_sources_router = None
try:
    from app.api.presets import router as presets_router, camera_presets_router, camera_source_presets_router
except Exception:  # noqa: BLE001
    presets_router = None
    camera_presets_router = None
    camera_source_presets_router = None
try:
    from app.api.camera_sources import router as camera_sources_router2
except Exception:  # noqa: BLE001
    camera_sources_router2 = None
try:
    from app.api.permissions import router as permissions_router
except Exception:  # noqa: BLE001
    permissions_router = None

try:
    from app.api.audit import router as audit_router
except Exception:  # noqa: BLE001
    audit_router = None
try:
    from app.api.email_alerts import router as email_alerts_router
except Exception:  # noqa: BLE001
    email_alerts_router = None
try:
    from app.api.floorplan import router as floorplan_router
except Exception:  # noqa: BLE001
    floorplan_router = None
try:
    from app.api.floorplan_editor import router as floorplan_editor_router
except Exception:  # noqa: BLE001
    floorplan_editor_router = None
try:
    from app.api.layout_editor import router as layout_editor_router
except Exception:  # noqa: BLE001
    layout_editor_router = None
try:
    from app.api.localization import router as localization_router
except Exception:  # noqa: BLE001
    localization_router = None
try:
    from app.api.nodes import router as nodes_router
except Exception:  # noqa: BLE001
    nodes_router = None
try:
    from app.api.restream import router as restream_router
except Exception:  # noqa: BLE001
    restream_router = None
try:
    from app.api.sms_alerts import router as sms_alerts_router
except Exception:  # noqa: BLE001
    sms_alerts_router = None
try:
    from app.api.source_presets import router as source_presets_router2
except Exception:  # noqa: BLE001
    source_presets_router2 = None
try:
    from app.api.storage_quota import router as storage_quota_router
except Exception:  # noqa: BLE001
    storage_quota_router = None
try:
    from app.api.talk import router as talk_router
except Exception:  # noqa: BLE001
    talk_router = None
try:
    from app.api.webhook_queue import router as webhook_queue_router
except Exception:  # noqa: BLE001
    webhook_queue_router = None
try:
    from app.services.event_bus import event_bus
except Exception:  # noqa: BLE001
    event_bus = None

TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RECORDING_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    await init_db()
    threading.Thread(target=start_broadcast, daemon=True).start()
    # Startup scheduling hook for retention cleanup (non-blocking).
    try:
        if start_retention_scheduler is not None:
            start_retention_scheduler()
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.services.ptz_scheduler import start_ptz_scheduler
        start_ptz_scheduler()
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.services.plugins import load_plugins
        load_plugins(app)
    except Exception:  # noqa: BLE001
        pass
    try:
        yield
    finally:
        stop_broadcast()
        try:
            from app.services.scheduler import stop_retention_scheduler

            stop_retention_scheduler()
        except Exception:  # noqa: BLE001
            pass
        try:
            from app.services.ptz_scheduler import stop_ptz_scheduler
            stop_ptz_scheduler()
        except Exception:  # noqa: BLE001
            pass


app = FastAPI(
    title="VigilCam",
    description="AI-driven, installable NVR for USB, IP and ONVIF cameras.",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


class NoCacheHTMLMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        if ct.startswith("text/html"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response


app.add_middleware(NoCacheHTMLMiddleware)

# Load plugins (scan plugins/ for modules exposing register(app)) before API routers
# so plugin routes take precedence over parameterized plugin endpoints.
try:
    from app.services.plugins import load_plugins
    load_plugins(app)
except Exception:  # noqa: BLE001
    pass

# Auto-register any router modules added under app/api without manual edits.
import importlib
import pkgutil
for _mod in pkgutil.iter_modules(__import__("app.api", fromlist=["__path__"]).__path__):
    try:
        _m = importlib.import_module(f"app.api.{_mod.name}")
        _r = getattr(_m, "router", None)
        if _r is not None and id(_r) not in getattr(app, "_registered_router_ids", set()):
            app.include_router(_r)
            app._registered_router_ids = getattr(app, "_registered_router_ids", set()) | {id(_r)}
    except Exception:  # noqa: BLE001
        pass
try:
    (DATA_DIR / "floorplans").mkdir(parents=True, exist_ok=True)
    app.mount("/data/floorplans", StaticFiles(directory=str(DATA_DIR / "floorplans")), name="floorplans")
except Exception:  # noqa: BLE001
    pass

# Router registration handled by auto-loader above.

for _r in (audit_router, email_alerts_router, floorplan_router, localization_router, nodes_router, restream_router, sms_alerts_router, source_presets_router2, storage_quota_router, talk_router, webhook_queue_router):
    if _r is not None:
        app.include_router(_r)


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    if event_bus is None:
        await websocket.close()
        return
    await event_bus.connect(websocket)
    try:
        while True:
            # Keep the socket open. Clients may send pings/text; we echo nothing.
            await websocket.receive_text()
    except WebSocketDisconnect:
        await event_bus.disconnect(websocket)
    except Exception:  # noqa: BLE001
        await event_bus.disconnect(websocket)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not is_initialized():
        return RedirectResponse(url="/setup", status_code=302)
    return TEMPLATES.TemplateResponse(request, "index.html", {})

@app.get("/setup", response_class=HTMLResponse)
async def setup(request: Request):
    if is_initialized():
        return RedirectResponse(url="/", status_code=302)
    return TEMPLATES.TemplateResponse(request, "setup.html", {})
