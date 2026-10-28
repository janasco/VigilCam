import os
from unittest.mock import patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.sms_alerts import router as sms_router
from app.services import sms_alerts
from app.services.sms_alerts import send_sms, send, sms_alerts_service


def test_sms_alerts_import():
    import app.services.sms_alerts  # noqa: F401
    import app.api.sms_alerts  # noqa: F401
    from app.services.sms_alerts import send_sms, send, sms_alerts_service  # noqa: F401


def test_sms_service_basic_mocked():
    class Resp:
        status_code = 201

        def json(self):
            return {"sid": "SM123"}

    with patch("app.services.sms_alerts.httpx.post", return_value=Resp()) as m:
        res = send_sms(
            account_sid="AC1",
            auth_token="tok",
            from_number="+1000",
            to="+1911",
            body="hello",
        )
        assert res["ok"] is True
        assert res["sid"] == "SM123"
        assert m.called
        args, kwargs = m.call_args
        assert args[0].endswith("/Messages.json")
        assert kwargs["auth"] == ("AC1", "tok")
        assert kwargs["data"]["To"] == "+1911"
        assert kwargs["data"]["Body"] == "hello"


def test_sms_router_endpoint_mocked():
    app = FastAPI()
    app.include_router(sms_router)
    client = TestClient(app)

    class Resp:
        status_code = 201

        def json(self):
            return {"sid": "SM999"}

    with patch("app.services.sms_alerts.httpx.post", return_value=Resp()):
        r = client.post(
            "/api/alerts/sms",
            json={"account_sid": "AC1", "auth_token": "tok", "from_number": "+1000", "to_number": "+1911", "message": "hi"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert r.json()["sid"] == "SM999"


def test_sms_multiple_recipients():
    class Resp:
        status_code = 201

        def json(self):
            return {"sid": "SMX"}

    with patch("app.services.sms_alerts.httpx.post", return_value=Resp()) as m:
        res = send_sms(
            account_sid="AC1",
            auth_token="tok",
            from_number="+1000",
            to_numbers=["+1", "+2"],
            body="hi",
        )
        assert res["ok"] is True
        assert m.call_count == 2
        assert res["count"] == 2
