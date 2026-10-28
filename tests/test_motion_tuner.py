import numpy as np

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import motion_tuner
from app.api import motion_tuner as mt_api


def make_frames():
    f1 = np.zeros((40, 40, 3), dtype=np.uint8)
    f2 = f1.copy()
    f2[5:15, 5:15] = 255
    return f1, f2


def test_import():
    import app.services.motion_tuner  # noqa: F401
    import app.api.motion_tuner  # noqa: F401
    from app.services.motion_tuner import MotionTuner, motion_tuner_service  # noqa: F401
    from app.api.motion_tuner import router  # noqa: F401


def test_service_basic():
    f1, f2 = make_frames()
    tuner = motion_tuner.MotionTuner(threshold=1, contour_area=1, improve_contrast=True)
    res = tuner.tune(f1, f2)
    assert isinstance(res, motion_tuner.MotionTunerResult)
    assert res.motion is True
    d = res.to_dict()
    assert d["ok"] is True
    assert "mask" in d


def test_aliases():
    a = motion_tuner.get_threshold()
    motion_tuner.set_threshold(10)
    assert motion_tuner.get_threshold() == 10
    motion_tuner.set_threshold(a)


def test_router_endpoints():
    app = FastAPI()
    app.include_router(mt_api.router)
    client = TestClient(app)
    r = client.get("/api/motion_tuner")
    assert r.status_code == 200
    r2 = client.post("/api/motion_tuner", json={})
    assert r2.status_code in (200, 400)


def test_paths_present():
    from app.main import app
    paths = list(app.openapi()["paths"].keys())
    assert "/api/motion_tuner" in paths
