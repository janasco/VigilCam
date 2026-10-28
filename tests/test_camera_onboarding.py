from app.services import camera_onboarding as co


def test_import_camera_onboarding():
    import app.main  # noqa: F401
    from app.main import app
    paths = list(app.openapi()["paths"].keys())
    assert "/api/cameras/{camera_id}/profiles" in paths


def test_derive_ip_address():
    assert co.derive_ip_address("rtsp://192.168.1.10:554/live", None) == "192.168.1.10"
    assert co.derive_ip_address(None, "192.168.1.20:8088") == "192.168.1.20"
    assert co.derive_ip_address("10.0.0.3", None) == "10.0.0.3"
    # onvif_host takes precedence
    assert co.derive_ip_address("rtsp://1.1.1.1/x", "2.2.2.2") == "2.2.2.2"
    assert co.derive_ip_address(None, None) is None
