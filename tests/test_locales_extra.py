"""Extra locale coverage: es/de/fr files load and match en.json structure.

Reference: /opt/others/ispy-agent-translations ships 29 locales
(ar, be, bn, cs, da, de, en, es, fa, fi, fr, hi, hu, id, it, ja,
ko, nb, nl, pl, pt, ru, sr, sv, tr, uk, vi, zh-cn, zh-tw).
This repo currently ships en + es + de + fr.
"""

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services import localization

BASE = Path(__file__).resolve().parents[1]
LOCALES_DIR = BASE / "app" / "locales"
EXPECTED = ("en", "es", "de", "fr")

PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_]+\}")


def _load(code):
    return json.loads((LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))


def _key_set(obj, prefix=""):
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.add(p)
            out |= _key_set(v, p)
    return out


def _walk_strs(a, b, path="", bad=None):
    if bad is None:
        bad = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in a:
            _walk_strs(a[k], b.get(k), f"{path}.{k}", bad)
    elif isinstance(a, str) and isinstance(b, str):
        if set(PLACEHOLDER_RE.findall(a)) != set(PLACEHOLDER_RE.findall(b)):
            bad.append(path)
    return bad


def test_locale_files_exist_and_valid():
    for code in EXPECTED:
        p = LOCALES_DIR / f"{code}.json"
        assert p.is_file(), f"missing {p}"
        data = _load(code)
        assert isinstance(data, dict) and data, f"{code}.json must be a non-empty dict"


def test_locale_key_parity_with_en():
    en_keys = _key_set(_load("en"))
    assert en_keys, "en.json must define keys"
    for code in ("es", "de", "fr"):
        assert _key_set(_load(code)) == en_keys, f"{code}.json key set differs from en.json"


def test_locale_placeholders_preserved():
    en = _load("en")
    for code in ("es", "de", "fr"):
        bad = _walk_strs(en, _load(code))
        assert not bad, f"{code}.json placeholder mismatches at: {bad[:5]}"


def test_locales_actually_translated():
    en = _load("en")
    for code in ("es", "de", "fr"):
        data = _load(code)
        assert data["hello"] != en["hello"], f"{code}.json hello not translated"
        assert data["save"] != en["save"], f"{code}.json save not translated"


def test_service_lists_extra_locales():
    locales = localization.list_locales()
    for code in EXPECTED:
        assert code in locales, f"{code} missing from list_locales(): {locales}"


def test_service_loads_each_locale():
    for code in EXPECTED:
        data = localization.load_translations(code)
        assert isinstance(data, dict) and data
    assert localization.load_translations("es")["hello"] == "Hola"
    assert localization.load_translations("de")["hello"] == "Hallo"
    assert localization.load_translations("fr")["hello"] == "Bonjour"


def test_locale_list_api_includes_extra():
    client = TestClient(app)
    r = client.get("/api/localization")
    assert r.status_code == 200
    body = r.json()
    locales = body.get("locales") if isinstance(body, dict) else body
    for code in EXPECTED:
        assert code in (locales or [])


def test_locale_api_returns_each_translation():
    client = TestClient(app)
    for code in EXPECTED:
        r = client.get(f"/api/localization/{code}")
        assert r.status_code == 200
        assert isinstance(r.json(), dict)
