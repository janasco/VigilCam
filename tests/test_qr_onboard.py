from fastapi.testclient import TestClient

from app.main import app
from app.services import qr_onboard as qo


def test_import_qr_onboard():
    import app.services.qr_onboard  # noqa: F401
    import app.api.qr_onboard  # noqa: F401
    from app.services.qr_onboard import (  # noqa: F401
        build_onboarding_payload,
        encode_onboarding_text,
        decode_onboarding_text,
        encode_qr_matrix,
        render_qr_png,
        qr_data_url,
        create_onboarding_code,
        verify_onboarding_code,
    )
    from app.api.qr_onboard import router  # noqa: F401


def test_no_qrcode_dependency():
    import pathlib

    for candidate in (
        pathlib.Path("app/services/qr_onboard.py"),
        pathlib.Path("/opt/repo/vigilcam/app/services/qr_onboard.py"),
    ):
        if candidate.is_file():
            src = candidate.read_text()
            break
    else:
        raise AssertionError("qr_onboard service file not found")
    lowered = src.lower()
    assert "import qrcode" not in lowered
    assert "import segno" not in lowered
    assert "from qrcode" not in lowered
    req_candidates = (
        pathlib.Path("requirements.txt"),
        pathlib.Path("/opt/repo/vigilcam/requirements.txt"),
    )
    for rc in req_candidates:
        if rc.is_file():
            req = rc.read_text().lower()
            assert "qrcode" not in req
            assert "segno" not in req
            break


def test_payload_roundtrip():
    payload = qo.build_onboarding_payload(camera_id=3, host="192.168.1.10", port=554, username="admin", name="Front")
    assert payload["camera_id"] == "3"
    assert payload["host"] == "192.168.1.10"
    text = qo.encode_onboarding_text(payload)
    assert text.startswith("VIGILCAM1:")
    back = qo.decode_onboarding_text(text)
    assert back["host"] == "192.168.1.10"
    assert back["camera_id"] == "3"


def test_qr_matrix_structure():
    info = qo.encode_qr_matrix("VIGILCAM1:HELLO", ec="M", mask=0)
    assert info["version"] >= 1
    assert info["size"] == info["version"] * 4 + 17
    matrix = info["matrix"]
    size = info["size"]
    assert len(matrix) == size
    assert all(len(row) == size for row in matrix)
    # Top-left finder pattern: 7x7 border dark, inner 3x3 dark.
    assert matrix[0][0] is True
    assert matrix[0][6] is True
    assert matrix[6][6] is True
    assert matrix[1][1] is False
    assert matrix[3][3] is True
    # Timing pattern row 6 alternates outside finders.
    assert matrix[6][8] is True
    assert matrix[8][6] is True


def test_png_valid_and_decodable():
    payload = qo.build_onboarding_payload(camera_id="7", host="10.0.0.5")
    text = qo.encode_onboarding_text(payload)
    png = qo.render_qr_png(text, scale=6, border=4)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 200
    # Pillow can open it.
    try:
        from PIL import Image
        import io as _io

        img = Image.open(_io.BytesIO(png))
        img.load()
        assert img.size[0] == img.size[1]
        assert img.size[0] > 100
    except ImportError:
        pass
    # OpenCV (already a VigilCam dep) should decode our QR.
    try:
        import cv2 as _cv
        import numpy as _np

        arr = _np.frombuffer(png, dtype=_np.uint8)
        img_cv = _cv.imdecode(arr, _cv.IMREAD_GRAYSCALE)
        assert img_cv is not None
        detector = _cv.QRCodeDetector()
        decoded, _pts, _qr = detector.detectAndDecode(img_cv)
        assert decoded == text, f"opencv decoded {decoded!r} != {text!r}"
    except ImportError:
        pass

    url = qo.qr_data_url(text)
    assert url.startswith("data:image/png;base64,")


def test_store_create_verify_revoke():
    qo.clear()
    created = qo.create_onboarding_code(camera_id=9, host="192.168.1.11", ttl_seconds=3600)
    assert created.get("id")
    assert created.get("code", "").startswith("VIGILCAM1:")
    assert created.get("data_url", "").startswith("data:image/png;base64,")
    assert created.get("version", 0) >= 1
    code = created["code"]
    fetched = qo.get_onboarding_code(created["id"])
    assert fetched is not None
    verified = qo.verify_onboarding_code(code)
    assert verified is not None
    assert verified.get("valid") is True
    assert qo.verify_onboarding_code("VIGILCAM1:bogus!!") is None
    revoked = qo.revoke_onboarding_code(created["id"])
    assert revoked is not None
    assert qo.verify_onboarding_code(code) is None


def test_endpoints():
    qo.clear()
    client = TestClient(app)

    r = client.get("/api/qr-onboard/status")
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True

    r = client.post("/api/qr-onboard/build", json={"camera_id": 1, "host": "192.168.1.20"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("code", "").startswith("VIGILCAM1:")
    assert body.get("data_url", "").startswith("data:image/png;base64,")

    r = client.post("/api/qr-onboard", json={"camera_id": 2, "host": "192.168.1.21", "ttl_seconds": 600})
    assert r.status_code == 200, r.text
    created = r.json()
    code_id = created.get("id")
    code = created.get("code")
    assert code_id and code

    r2 = client.get("/api/qr-onboard")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)
    assert len(r2.json()) == 1

    r3 = client.get(f"/api/qr-onboard/{code_id}")
    assert r3.status_code == 200, r3.text

    r4 = client.get(f"/api/qr-onboard/{code_id}/png")
    assert r4.status_code == 200, r4.text
    assert r4.headers.get("content-type") == "image/png"
    assert r4.content[:8] == b"\x89PNG\r\n\x1a\n"

    r5 = client.post("/api/qr-onboard/verify", json={"code": code})
    assert r5.status_code == 200, r5.text
    assert r5.json().get("valid") is True

    r6 = client.delete(f"/api/qr-onboard/{code_id}")
    assert r6.status_code == 200, r6.text
    assert r6.json().get("revoked") is True

    r7 = client.post("/api/qr-onboard/verify", json={"code": code})
    assert r7.status_code == 200, r7.text
    assert r7.json().get("valid") is False

    # Underscore alias works too.
    r8 = client.get("/api/qr_onboard/status")
    assert r8.status_code == 200, r8.text
