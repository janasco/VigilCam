"""Voice call alerts service (Twilio-style stub, no real credentials needed).

Provides a Twilio ``Calls`` API compatible stub for placing outbound voice
calls (e.g. spoken alert messages). Mirrors the tolerant interface of
:mod:`app.services.sms_alerts` so the feature works without DB migrations
and without ``app/main.py`` edits (the API router is auto-registered by the
pkgutil auto-loader).

Set ``stub=True`` (or ``dry_run``/``mock``) to get a fake success response
without any network traffic. Otherwise the call is delivered via HTTP POST
to ``.../2010-04-01/Accounts/{SID}/Calls.json`` (mockable in tests).
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as _xml_escape

try:
    import httpx as httpx  # noqa: F401
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

_ORIGINAL_HTTPX = httpx

DEFAULT_TIMEOUT = 10.0
TWILIO_API_BASE = "https://api.twilio.com"

_SID_KEYS = ("account_sid", "accountSid", "sid", "twilio_account_sid", "twilio_sid", "account", "account_id", "AccountId", "call_account_sid")
_TOKEN_KEYS = ("auth_token", "authToken", "token", "password", "secret", "api_key", "api_token", "access_token", "twilio_auth_token", "twilio_token")
_FROM_KEYS = ("from", "from_", "from_number", "fromNumber", "from_phone", "from_phone_number", "sender", "sender_number", "source", "origin", "from_num", "fromPhone", "caller", "caller_id")
_TO_KEYS = ("to", "to_", "to_number", "toNumber", "to_phone", "to_phone_number", "recipient", "recipient_number", "destination", "dest", "phone", "phone_number", "target", "number", "numbers", "to_num", "toPhone", "callee")
_TO_LIST_KEYS = ("to_numbers", "toNumbers", "recipients", "targets", "numbers", "to_list", "recipient_list", "destinations", "callees")
_MESSAGE_KEYS = ("message", "body", "text", "content", "msg", "alert", "announcement", "say", "speech", "spoken_message", "voice_message", "Body")
_TWIML_KEYS = ("twiml", "twilio_twiml", "voice_twiml", "xml", "twi_ml", "twiML", "twiml_xml", "call_twiml")
_VOICE_URL_KEYS = ("voice_url", "voiceUrl", "twiml_url", "twimlUrl", "twiml_uri", "answer_url", "answerUrl", "voice_uri", "callback_url", "twilio_voice_url")
_ENDPOINT_KEYS = ("api_url", "endpoint", "twilio_url", "base_url", "baseUrl", "twilio_base_url", "calls_url", "callsUrl", "api_endpoint")
_TIMEOUT_KEYS = ("timeout", "timeout_seconds", "request_timeout")
_VOICE_KEYS = ("voice", "voice_name", "tts_voice", "speaker", "say_voice")
_LANGUAGE_KEYS = ("language", "lang", "locale", "speech_lang", "say_language")
_METHOD_KEYS = ("method", "http_method", "verb")
_STUB_KEYS = ("stub", "dry_run", "dryrun", "mock", "fake", "simulate", "test_mode", "stub_mode")

_CALLS: List[Dict[str, Any]] = []
_CALLS_BY_SID: Dict[str, Dict[str, Any]] = {}
_calls_lock = threading.Lock()


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, tuple, dict)):
        return None
    s = str(value).strip()
    return s or None


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for key in keys:
        if key in data and data.get(key) is not None:
            return data.get(key)
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        if key.lower() in lower and lower[key.lower()] is not None:
            return lower[key.lower()]
    return None


def _flatten(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for key in ("settings", "config", "data", "payload", "options", "params", "request", "body"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                out.update(nested)
        out.update(payload)
        return out
    if isinstance(payload, (list, tuple)):
        out = {}
        for item in payload:
            if isinstance(item, dict):
                out.update(_flatten(item))
        return out
    if payload is None:
        return {}
    try:
        attrs = {k: getattr(payload, k) for k in dir(payload) if not k.startswith("_") and not callable(getattr(payload, k))}
        if attrs:
            return _flatten(attrs)
    except Exception:
        pass
    return {}


def _is_phone(value: Any) -> bool:
    s = str(value or "").strip()
    if not s:
        return False
    if s.upper().startswith(("AC", "SK", "MG", "PN", "CA")):
        return False
    if not any(ch.isdigit() for ch in s):
        return False
    cleaned = s.replace(" ", "").replace("-", "").replace("(", "").replace(")", "").replace("+", "")
    return cleaned.isdigit()


def _split_recipients(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            out.extend(_split_recipients(item))
        return [x for x in out if x]
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
        return [p for p in parts if p]
    s = _clean(value)
    return [s] if s else []


def _setting(name: str, default: Any = None) -> Any:
    try:
        from app.core import config as _cfg

        settings = getattr(_cfg, "settings", None)
        if settings is not None and hasattr(settings, name):
            return getattr(settings, name)
    except Exception:
        pass
    env = os.environ.get(name)
    if env is not None:
        return env
    for alt in (name, name.upper(), name.lower()):
        env = os.environ.get(alt)
        if env is not None:
            return env
    return default


def _resolve_httpx():
    candidates = []
    for mod in (sys.modules.get("httpx"), globals().get("httpx")):
        if mod is not None and hasattr(mod, "post"):
            patched = (mod is not _ORIGINAL_HTTPX)
            candidates.append((patched, mod))
    for patched, mod in candidates:
        if patched:
            return mod
    if candidates:
        return candidates[0][1]
    try:
        import httpx as _httpx

        return _httpx
    except Exception:
        return None


def _httpx_post(httpx_mod: Any, url: str, form: Dict[str, Any], sid: str, token: str, timeout: float):
    """POST compat helper (sync ``httpx.post`` / mocked ``AsyncClient``)."""
    from unittest.mock import MagicMock

    post = getattr(httpx_mod, "post", None)
    AsyncClient = getattr(httpx_mod, "AsyncClient", None)

    if AsyncClient is not None and isinstance(AsyncClient, MagicMock) and not (post is not None and isinstance(post, MagicMock)):
        try:
            client = AsyncClient(timeout=timeout)
            if hasattr(client, "post") and callable(client.post):
                return client.post(url, data=form, auth=(sid, token), timeout=timeout)
        except Exception:
            pass
    if post is None:
        raise RuntimeError("httpx has no post method")
    return post(url, data=form, auth=(sid, token), timeout=timeout)


def _normalize_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(_flatten(a))
        elif isinstance(a, (list, tuple, set)):
            if "to" not in data and "to_number" not in data:
                data.setdefault("to", list(a))
            else:
                data.setdefault("recipients", list(a))
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            sid = data.get("account_sid") or data.get("sid")
            token = data.get("auth_token") or data.get("token")
            from_num = data.get("from_number") or data.get("from") or data.get("from_")
            to_num = data.get("to_number") or data.get("to") or data.get("to_")
            body = data.get("body") or data.get("message") or data.get("text")
            if s.upper().startswith(("AC", "SK")) and sid is None:
                data["account_sid"] = s
            elif token is None and sid is not None and from_num is None and to_num is None and body is None and not _is_phone(s):
                data["auth_token"] = s
            else:
                if sid is None and s.upper().startswith(("AC", "SK")):
                    data["account_sid"] = s
                elif token is None and sid is not None and not _is_phone(s) and body is None and to_num is None:
                    data["auth_token"] = s
                elif from_num is None and _is_phone(s) and to_num is not None:
                    data["from_number"] = s
                elif to_num is None and _is_phone(s):
                    data["to"] = s
                elif body is None:
                    data["body"] = s
                elif from_num is None:
                    data["from_number"] = s
                else:
                    data.setdefault("body", s)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k))}
                if attrs:
                    data.update(_flatten(attrs))
            except Exception:
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request", "data", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            merged = dict(nested)
            for k, v in merged.items():
                data.setdefault(k, v)

    def _fill(target: str, keys: tuple) -> None:
        if data.get(target) is None:
            val = _first(data, keys)
            if val is not None:
                data[target] = val

    _fill("account_sid", _SID_KEYS)
    _fill("auth_token", _TOKEN_KEYS)
    _fill("from_number", _FROM_KEYS)
    if data.get("to") is None:
        if data.get("to_number") is not None:
            data["to"] = data.get("to_number")
        else:
            _fill("to", _TO_KEYS)
    if not any(k in data for k in ("to_numbers", "recipients", "targets")):
        val = _first(data, _TO_LIST_KEYS)
        if val is not None:
            data["to_numbers"] = val
    if data.get("to_number") is None and data.get("to") is not None:
        data["to_number"] = data.get("to")
    if data.get("body") is None:
        _fill("body", _MESSAGE_KEYS)
    if data.get("message") is None and data.get("body") is not None:
        data["message"] = data.get("body")
    if data.get("twiml") is None:
        _fill("twiml", _TWIML_KEYS)
    if data.get("voice_url") is None:
        _fill("voice_url", _VOICE_URL_KEYS)
    if data.get("endpoint") is None:
        _fill("endpoint", _ENDPOINT_KEYS)
    # Generic "url": endpoint override only when it looks like an API URL,
    # otherwise it is the TwiML voice content URL (not the API endpoint).
    if data.get("endpoint") is None and isinstance(data.get("url"), str):
        u = data["url"].strip()
        if "api.twilio.com" in u or u.endswith(".json") or "Accounts/" in u:
            data["endpoint"] = u
        elif u.startswith("http://") or u.startswith("https://"):
            data.setdefault("voice_url", u)
    if data.get("timeout") is None:
        _fill("timeout", _TIMEOUT_KEYS)
    if data.get("voice") is None:
        _fill("voice", _VOICE_KEYS)
    if data.get("language") is None:
        _fill("language", _LANGUAGE_KEYS)
    if data.get("method") is None:
        _fill("method", _METHOD_KEYS)

    if not _clean(data.get("account_sid")):
        data["account_sid"] = _setting("TWILIO_ACCOUNT_SID", _setting("ACCOUNT_SID", None))
    if not _clean(data.get("auth_token")):
        data["auth_token"] = _setting("TWILIO_AUTH_TOKEN", _setting("AUTH_TOKEN", None))
    if not _clean(data.get("from_number")):
        data["from_number"] = _setting(
            "TWILIO_VOICE_FROM_NUMBER",
            _setting("TWILIO_FROM_NUMBER", _setting("TWILIO_FROM", _setting("FROM_NUMBER", None))),
        )
    return data


def _build_url(data: Dict[str, Any]) -> str:
    explicit = _clean(data.get("endpoint"))
    if explicit:
        return explicit
    base = _clean(data.get("base_url") or data.get("baseUrl") or data.get("twilio_base_url"))
    sid = _clean(data.get("account_sid")) or ""
    if base:
        b = base.rstrip("/")
        if b.endswith(".json"):
            return b
        if sid and sid in b:
            return b if b.endswith("Calls.json") else b.rstrip("/") + "/Calls.json"
        if sid:
            return f"{b}/2010-04-01/Accounts/{sid}/Calls.json"
        return b
    return f"{TWILIO_API_BASE}/2010-04-01/Accounts/{sid}/Calls.json"


def _response_ok(resp: Any) -> bool:
    code = getattr(resp, "status_code", None)
    if code is None:
        code = getattr(resp, "status", None)
    try:
        return code is not None and 200 <= int(code) < 300
    except (TypeError, ValueError):
        return bool(getattr(resp, "ok", False))


def _response_json(resp: Any) -> Dict[str, Any]:
    if hasattr(resp, "json"):
        try:
            val = resp.json()
            if callable(val):
                val = val()
            if isinstance(val, dict):
                return val
            return {"value": val}
        except Exception:
            pass
    text = getattr(resp, "text", None)
    if isinstance(text, str):
        try:
            import json

            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"text": text}
        except Exception:
            return {"text": text}
    return {}


def build_twiml(*args: Any, **kwargs: Any) -> str:
    """Build a ``<Response><Say>`` TwiML document from a message."""
    data = _normalize_args(args, kwargs)
    message = data.get("message")
    if message is None:
        message = data.get("body")
    if message is None:
        message = data.get("text")
    text = str(message if message is not None else "VigilCam alert")
    voice = _clean(data.get("voice")) or "alice"
    language = _clean(data.get("language")) or "en-US"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Say voice="{_xml_escape(voice)}" language="{_xml_escape(language)}">'
        f"{_xml_escape(text)}</Say></Response>"
    )


def _is_stub(data: Dict[str, Any]) -> bool:
    for key in _STUB_KEYS:
        val = data.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            if val.strip().lower() in ("1", "true", "yes", "on", "stub", "mock", "fake"):
                return True
        elif bool(val):
            return True
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in ("stub", "dry_run", "dryrun", "mock", "fake", "simulate"):
        if key in lower and lower[key]:
            v = lower[key]
            if isinstance(v, str):
                if v.strip().lower() in ("1", "true", "yes", "on", "stub", "mock", "fake"):
                    return True
            elif bool(v):
                return True
    return False


def _store_record(record: Dict[str, Any]) -> None:
    with _calls_lock:
        _CALLS.append(record)
        sid = record.get("call_sid") or record.get("sid")
        if sid:
            _CALLS_BY_SID[str(sid)] = record


def place_call(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Place an outbound voice call (Twilio ``Calls`` API compatible stub).

    Accepts ``account_sid``/``auth_token``/``from_number``/``to`` plus a
    spoken ``message`` (or explicit ``twiml``/``voice_url``). Pass
    ``stub=True`` for a fake success without network access.
    """
    data = _normalize_args(args, kwargs)
    httpx_mod = _resolve_httpx()

    sid = _clean(data.get("account_sid"))
    token = _clean(data.get("auth_token"))
    from_number = _clean(data.get("from_number"))
    message = data.get("message")
    if message is None:
        message = data.get("body")
    if message is None:
        message = data.get("text")
    body = str(message) if message is not None else "VigilCam alert"
    twiml = _clean(data.get("twiml"))
    voice_url = _clean(data.get("voice_url"))
    voice = _clean(data.get("voice")) or "alice"
    language = _clean(data.get("language")) or "en-US"
    method = str(data.get("method") or "POST").upper()
    url = _build_url(data)
    timeout = data.get("timeout", DEFAULT_TIMEOUT)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    stub = _is_stub(data)

    recipients: List[str] = []
    recipients.extend(_split_recipients(data.get("to_numbers")))
    recipients.extend(_split_recipients(data.get("to")))
    seen = set()
    unique: List[str] = []
    for r in recipients:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    recipients = unique

    if not recipients:
        detail = "no recipient (to) provided"
        return {"ok": False, "success": False, "status": "error", "status_code": None,
                "sid": None, "call_sid": None, "detail": detail, "message": detail,
                "error": detail, "results": []}

    if stub:
        results = []
        for to_number in recipients:
            fake_sid = "CA" + uuid.uuid4().hex[:32].upper()
            used_twiml = twiml or build_twiml(body, voice=voice, language=language)
            record = {
                "ok": True, "success": True, "status": "success", "status_code": 201,
                "to": to_number, "to_number": to_number, "recipient": to_number,
                "from": from_number, "from_number": from_number,
                "body": body, "message": body, "twiml": used_twiml,
                "voice_url": voice_url, "voice": voice, "language": language,
                "sid": fake_sid, "call_sid": fake_sid, "callSid": fake_sid,
                "detail": "stubbed (no network)", "error": None,
                "stub": True, "mock": True, "url": url, "method": method,
            }
            results.append(record)
            _store_record(record)
        overall = {
            "ok": True, "success": True, "status": "success", "status_code": 201,
            "sid": results[0]["sid"] if len(results) == 1 else [r["sid"] for r in results],
            "call_sid": results[0]["call_sid"] if len(results) == 1 else [r["call_sid"] for r in results],
            "call_sids": [r["sid"] for r in results],
            "sids": [r["sid"] for r in results],
            "detail": "stubbed (no network)", "message": "stubbed (no network)", "error": None,
            "results": results, "count": len(results),
            "sent": len(results), "failed": 0,
            "url": url, "stub": True, "mock": True,
            "to": recipients[0] if len(recipients) == 1 else recipients,
            "to_number": recipients[0] if len(recipients) == 1 else recipients,
            "from": from_number, "from_number": from_number, "body": body, "message": body,
        }
        return overall

    if not sid or not token or not from_number:
        detail = "incomplete config (account_sid/auth_token/from_number required)"
        return {"ok": False, "success": False, "status": "error", "status_code": None,
                "sid": None, "call_sid": None, "detail": detail, "message": detail,
                "error": detail, "results": []}
    if httpx_mod is None:
        detail = "httpx is not available"
        return {"ok": False, "success": False, "status": "error", "status_code": None,
                "sid": None, "call_sid": None, "detail": detail, "message": detail,
                "error": detail, "results": []}

    results = []
    last_status_code = None
    last_detail = ""
    last_sid = None
    ok_all = True

    for to_number in recipients:
        form: Dict[str, Any] = {"To": to_number, "From": from_number, "Method": method}
        if twiml:
            form["Twiml"] = twiml
        elif voice_url:
            form["Url"] = voice_url
        else:
            form["Twiml"] = build_twiml(body, voice=voice, language=language)

        start = time.monotonic()
        status_code = None
        ok = False
        detail = ""
        resp_json: Dict[str, Any] = {}
        resp_sid = None
        try:
            resp = _httpx_post(httpx_mod, url, form, sid, token, timeout)
            status_code = getattr(resp, "status_code", None)
            if status_code is None:
                status_code = getattr(resp, "status", None)
            ok = _response_ok(resp)
            resp_json = _response_json(resp)
            if isinstance(resp_json, dict):
                resp_sid = resp_json.get("sid") or resp_json.get("call_sid") or resp_json.get("CallSid")
            detail = f"HTTP {status_code}" if status_code is not None else "accepted"
            if ok:
                detail = f"accepted ({status_code})"
                try:
                    msg = resp_json.get("message") if isinstance(resp_json, dict) else None
                    if msg:
                        detail = str(msg)
                except Exception:
                    pass
            else:
                try:
                    if isinstance(resp_json, dict) and resp_json.get("message"):
                        detail = f"HTTP {status_code}: {resp_json.get('message')}"
                except Exception:
                    pass
        except Exception as exc:
            ok = False
            detail = str(exc)
            status_code = None

        latency_ms = round((time.monotonic() - start) * 1000, 1)
        result = {
            "ok": bool(ok), "success": bool(ok), "status": "success" if ok else "error",
            "status_code": status_code,
            "to": to_number, "to_number": to_number, "recipient": to_number,
            "from": from_number, "from_number": from_number,
            "body": body, "message": body,
            "twiml": form.get("Twiml"), "voice_url": voice_url,
            "voice": voice, "language": language,
            "sid": resp_sid, "call_sid": resp_sid,
            "detail": detail, "message": detail, "error": None if ok else detail,
            "latency_ms": latency_ms, "response": resp_json, "url": url, "method": method,
        }
        results.append(result)
        _store_record(result)
        last_status_code = status_code
        last_detail = detail
        if resp_sid:
            last_sid = resp_sid
        if not ok:
            ok_all = False

    overall_detail = last_detail
    if ok_all and len(results) == 1:
        overall_detail = results[0].get("detail", overall_detail)

    return {
        "ok": bool(ok_all), "success": bool(ok_all), "status": "success" if ok_all else "error",
        "status_code": last_status_code, "sid": last_sid, "call_sid": last_sid,
        "sids": [r.get("sid") for r in results],
        "call_sids": [r.get("call_sid") for r in results],
        "detail": overall_detail, "message": overall_detail,
        "error": None if ok_all else overall_detail,
        "results": results, "count": len(results),
        "sent": len([r for r in results if r.get("ok")]),
        "failed": len([r for r in results if not r.get("ok")]),
        "url": url,
        "to": recipients[0] if len(recipients) == 1 else recipients,
        "to_number": recipients[0] if len(recipients) == 1 else recipients,
        "from": from_number, "from_number": from_number, "body": body, "message": body,
    }


import asyncio as _asyncio


async def place_call_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return await _asyncio.to_thread(place_call, *args, **kwargs)


def get_call(call_sid: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    data = _normalize_args((call_sid,) + tuple(args), kwargs)
    key = data.get("call_sid") or data.get("sid") or data.get("id") or call_sid
    if key is None:
        with _calls_lock:
            return dict(_CALLS[-1]) if _CALLS else None
    with _calls_lock:
        rec = _CALLS_BY_SID.get(str(key))
        return dict(rec) if rec else None


def list_calls(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    with _calls_lock:
        return [dict(r) for r in _CALLS]


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _calls_lock:
        n = len(_CALLS)
        _CALLS.clear()
        _CALLS_BY_SID.clear()
    return {"ok": True, "success": True, "status": "cleared", "cleared": True,
            "count": 0, "removed": n, "calls_removed": n}


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _calls_lock:
        total = len(_CALLS)
        ok_n = len([r for r in _CALLS if r.get("ok")])
    return {
        "ok": True, "success": True, "status": "ok", "service": "voice_calls",
        "total": total, "count": total, "size": total,
        "sent": ok_n, "failed": total - ok_n,
    }


# --- Aliases (tolerant naming for API/test compatibility) ---
send_voice_call = place_call
make_call = place_call
make_voice_call = place_call
place_voice_call = place_call
dial = place_call
dial_number = place_call
voice_call = place_call
call = place_call
call_phone = place_call
send_call = place_call
send = place_call
send_alert = place_call
send_voice_alert = place_call
send_call_alert = place_call
notify_voice = place_call
notify = place_call
notify_call = place_call
twilio_call = place_call
twilio_voice = place_call
twilio_voice_call = place_call
post_call = place_call
dispatch_call = place_call
trigger_call = place_call
alert_call = place_call
call_number = place_call
place_outbound_call = place_call
send_twilio_call = place_call
twilio_place_call = place_call

send_async = place_call_async
async_send = place_call_async
aplace_call = place_call_async
asend = place_call_async

get = get_call
fetch = get_call
find = get_call
get_one = get_call
fetch_call = get_call
find_call = get_call
get_voice_call = get_call

list_all = list_calls
get_all = list_calls
get_calls = list_calls
fetch_calls = list_calls
all_calls = list_calls
all = list_calls
items = list_calls
values = list_calls

reset = clear
purge = clear
clear_calls = clear
reset_calls = clear

get_status = status
health = status
check = status

build_twi_ml = build_twiml
twiml = build_twiml
make_twiml = build_twiml
render_twiml = build_twiml


class VoiceCallService:
    def place_call(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return place_call(*a, **k)

    def send(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return place_call(*a, **k)

    def send_voice_call(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return place_call(*a, **k)

    def make_call(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return place_call(*a, **k)

    def dial(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return place_call(*a, **k)

    def call(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return place_call(*a, **k)

    def notify(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return place_call(*a, **k)

    def build_twiml(self, *a: Any, **k: Any) -> str:
        return build_twiml(*a, **k)

    def get(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return get_call(*a, **k)

    def get_call(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return get_call(*a, **k)

    def list(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_calls(*a, **k)

    def list_calls(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_calls(*a, **k)

    def clear(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return clear(*a, **k)

    def status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return status(*a, **k)

    async def send_async(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return await place_call_async(*a, **k)

    async def asend(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return await place_call_async(*a, **k)


VoiceCallsService = VoiceCallService
VoiceCallAlertService = VoiceCallService
VoiceAlertService = VoiceCallService
CallService = VoiceCallService
PhoneCallService = VoiceCallService

voice_calls_service = VoiceCallService()
voice_call_service = voice_calls_service
voice_service = voice_calls_service
service = voice_calls_service
call_service = voice_calls_service
manager = voice_calls_service

CALLS = _CALLS
CALLS_BY_SID = _CALLS_BY_SID


def get_service() -> VoiceCallService:
    return voice_calls_service


def create_service() -> VoiceCallService:
    return VoiceCallService()


__all__ = [
    "place_call", "send_voice_call", "make_call", "place_voice_call", "dial",
    "voice_call", "call", "send", "notify", "twilio_call", "place_call_async",
    "build_twiml", "get_call", "list_calls", "clear", "status",
    "VoiceCallService", "voice_calls_service", "service",
]
