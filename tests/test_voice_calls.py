from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.voice_calls import router as voice_calls_router
from app.services import voice_calls
from app.services.voice_calls import (
    build_twiml,
    clear,
    list_calls,
    place_call,
    send,
    status,
    voice_calls_service,
)


def test_voice_calls_import():
    import app.services.voice_calls  # noqa: F401
    import app.api.voice_calls  # noqa: F401
    from app.services.voice_calls import place_call, send, voice_calls_service  # noqa: F401
    from app.api.voice_calls import router  # noqa: F401


def _resp(sid="CAX123", code=201):
    class Resp:
        status_code = code

        def json(self):
            return {"sid": sid}

    return Resp()


def test_voice_call_service_basic_mocked():
    clear()
    with patch("app.services.voice_calls.httpx.post", return_value=_resp("CAX123")) as m:
        res = place_call(
            account_sid="AC1",
            auth_token="tok",
            from_number="+1000",
            to="+1911",
            message="intruder alert",
        )
        assert res["ok"] is True
        assert res["call_sid"] == "CAX123"
        assert m.called
        args, kwargs = m.call_args
        assert args[0].endswith("/Calls.json")
        assert kwargs["auth"] == ("AC1", "tok")
        assert kwargs["data"]["To"] == "+1911"
        assert kwargs["data"]["From"] == "+1000"
        assert "intruder alert" in kwargs["data"]["Twiml"]
    clear()


def test_voice_call_build_twiml():
    xml = build_twiml("hello there")
    assert "<Say" in xml
    assert "hello there" in xml


def test_voice_call_stub_mode_no_network():
    clear()
    with patch("app.services.voice_calls.httpx.post", side_effect=AssertionError("no network")):
        res = place_call(
            from_number="+1000",
            to="+1911",
            message="test",
            stub=True,
        )
        assert res["ok"] is True
        assert res["stub"] is True
        assert res["call_sid"].startswith("CA")
    clear()


def test_voice_call_requires_recipient():
    clear()
    res = place_call(account_sid="AC1", auth_token="t", from_number="+1", stub=True)
    assert res["ok"] is False
    clear()


def test_voice_call_router_endpoint_mocked():
    clear()
    app = FastAPI()
    app.include_router(voice_calls_router)
    client = TestClient(app)

    with patch("app.services.voice_calls.httpx.post", return_value=_resp("CAX999")):
        r = client.post(
            "/api/alerts/voice-call",
            json={"account_sid": "AC1", "auth_token": "tok", "from_number": "+1000",
                  "to_number": "+1911", "message": "hi"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert r.json()["call_sid"] == "CAX999"

    with patch("app.services.voice_calls.httpx.post", return_value=_resp("CAX888")):
        r = client.post(
            "/api/voice-calls",
            json={"account_sid": "AC1", "auth_token": "tok", "from_number": "+1000",
                  "to_number": "+1911", "message": "hi"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

    r = client.get("/api/alerts/voice-call/health")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    clear()


def test_voice_call_multiple_recipients():
    clear()
    with patch("app.services.voice_calls.httpx.post", return_value=_resp("CAX")) as m:
        res = place_call(
            account_sid="AC1",
            auth_token="tok",
            from_number="+1000",
            to_numbers=["+1", "+2"],
            message="hi",
        )
        assert res["ok"] is True
        assert m.call_count == 2
        assert res["count"] == 2
    clear()


def test_voice_call_history_and_status():
    clear()
    assert voice_calls.status()["ok"] is True
    assert list_calls() == []
    place_call(from_number="+1", to="+2", message="x", stub=True)
    assert len(list_calls()) == 1
    assert status()["total"] == 1
    clear()
