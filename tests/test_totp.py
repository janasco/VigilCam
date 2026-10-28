"""Tests for stdlib TOTP service + API router."""
from __future__ import annotations

import base64
import time

from fastapi.testclient import TestClient

from app.main import app
from app.services import totp as totp_service


def test_generate_secret_is_base32_unique():
    s1 = totp_service.generate_secret()
    s2 = totp_service.generate_secret()
    assert s1 != s2
    assert len(s1) >= 26  # 160-bit base32 without padding
    # Must decode as base32.
    padded = s1 + "=" * (-len(s1) % 8)
    base64.b32decode(padded, casefold=True)


def test_provisioning_uri_format():
    secret = totp_service.generate_secret()
    uri = totp_service.provisioning_uri(secret, "alice@example.com", issuer="VigilCam")
    assert uri.startswith("otpauth://totp/")
    assert f"secret={secret}" in uri
    assert "issuer=VigilCam" in uri
    assert "algorithm=SHA1" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


def test_generate_and_verify_roundtrip():
    secret = totp_service.generate_secret()
    code = totp_service.generate_code(secret)
    assert len(code) == 6
    assert totp_service.verify_code(secret, code) is True
    assert totp_service.verify_code(secret, "000000") in (True, False)  # smoke: returns bool


def test_wrong_code_rejected():
    secret = totp_service.generate_secret()
    code = totp_service.generate_code(secret)
    # Flip last digit to guarantee a different code.
    bad = code[:-1] + ("0" if code[-1] != "0" else "1")
    assert bad != code
    # Extremely unlikely the flipped code is also valid for an adjacent window;
    # verify with window=0 for determinism against current counter.
    assert totp_service.verify_code(secret, bad, window=0) is False


def test_time_window_behavior():
    secret = totp_service.generate_secret()
    now = time.time()
    interval = 30
    prev_code = totp_service.generate_code(secret, at=now - interval, interval=interval)
    # Previous-interval code accepted with window=1 but not window=0.
    assert totp_service.verify_code(secret, prev_code, at=now, window=1, interval=interval) is True
    assert totp_service.verify_code(secret, prev_code, at=now, window=0, interval=interval) is False


def test_rfc6238_vector_sha1():
    # RFC 6238 Appendix B: SHA1 secret "12345678901234567890", T=1 -> 94287082 (8-digit).
    raw_secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    code = totp_service.generate_code(raw_secret, at=59, interval=30, digits=8, digest="sha1")
    assert code == "94287082"


def test_api_setup_and_verify():
    client = TestClient(app)
    r = client.post("/api/auth/totp/setup", json={"account": "bob", "issuer": "VigilCam"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("secret")
    assert body.get("provisioning_uri", "").startswith("otpauth://totp/")

    secret = body["secret"]
    code = totp_service.generate_code(secret)
    v = client.post("/api/auth/totp/verify", json={"secret": secret, "code": code})
    assert v.status_code == 200, v.text
    assert v.json().get("valid") is True

    bad = client.post("/api/auth/totp/verify", json={"secret": secret, "code": "000000", "window": 0})
    assert bad.status_code == 200, bad.text
    assert bad.json().get("valid") is False


def test_api_verify_requires_fields():
    client = TestClient(app)
    r = client.post("/api/auth/totp/verify", json={})
    assert r.status_code == 400, r.text
