from app.main import app


def test_onvif_ptz_imports():
    import app.api.onvif_ptz  # noqa: F401
    import app.api.ptz  # noqa: F401
    from app.schemas import OnvifPtzContinuousMoveRequest, OnvifPtzResponse  # noqa: F401


def test_onvif_ptz_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("continuous" in p and "ptz" in p for p in paths)
    assert any(p.endswith("/ptz/stop") for p in paths)
    assert any("preset" in p for p in paths)