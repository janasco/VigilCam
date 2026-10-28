try:
    from app.main import app as _app
    from app.api import email_alerts as _email_alerts

    if not getattr(_app.state, "email_alerts_registered", False):
        _app.include_router(_email_alerts.router)
        _app.state.email_alerts_registered = True
except Exception:  # noqa: BLE001
    pass

try:
    from app.main import app as _app
    from app.api import floorplan as _floorplan

    if not getattr(_app.state, "floorplan_registered", False):
        _app.include_router(_floorplan.router)
        _app.state.floorplan_registered = True
except Exception:  # noqa: BLE001
    pass

try:
    from app.main import app as _app
    from app.api import floorplan_editor as _floorplan_editor

    if not getattr(_app.state, "floorplan_editor_registered", False):
        _app.include_router(_floorplan_editor.router)
        _app.state.floorplan_editor_registered = True
except Exception:  # noqa: BLE001
    pass

try:
    from app.main import app as _app
    from app.api import webhook_queue as _webhook_queue

    if not getattr(_app.state, "webhook_queue_registered", False):
        _app.include_router(_webhook_queue.router)
        _app.state.webhook_queue_registered = True
except Exception:  # noqa: BLE001
    pass

try:
    from app.main import app as _app
    from app.api import devices as _devices

    if not getattr(_app.state, "devices_registered", False):
        _app.include_router(_devices.router)
        try:
            _app.include_router(_devices.device_router)
        except Exception:
            pass
        _app.state.devices_registered = True
except Exception:
    pass

try:
    from app.main import app as _app
    from app.api import web_push as _web_push

    if not getattr(_app.state, "web_push_registered", False):
        _app.include_router(_web_push.router)
        _app.state.web_push_registered = True
    try:
        _app.include_router(_web_push.underscore_router)
    except Exception:  # noqa: BLE001
        pass
    try:
        _app.include_router(_web_push.hyphen_router)
    except Exception:  # noqa: BLE001
        pass
    try:
        _app.include_router(_web_push.push_router)
    except Exception:  # noqa: BLE001
        pass
except Exception:  # noqa: BLE001
    pass
