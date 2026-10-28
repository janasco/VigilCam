from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request

from app.services import localization as loc_service

router = APIRouter(tags=["localization"])
localization_router = router
locales_router = router
locale_router = router
i18n_router = router
translations_router = router
translation_router = router


def _locales_dir_kw(locales_dir: Any = None, directory: Any = None, dir: Any = None, path: Any = None) -> dict:
    kw = {}
    if locales_dir is not None:
        kw["locales_dir"] = locales_dir
    if directory is not None:
        kw["directory"] = directory
    if dir is not None:
        kw["dir"] = dir
    if path is not None:
        kw["path"] = path
    return kw


@router.get("/api/localization")
@router.get("/api/localization/")
@router.get("/api/localizations")
@router.get("/api/localizations/")
async def localization_root():
    try:
        locales = loc_service.list_locales()
    except Exception:
        locales = []
    return {
        "locales": locales,
        "languages": locales,
        "available": locales,
        "available_locales": locales,
        "default": getattr(loc_service, "DEFAULT_LOCALE", "en"),
        "current": loc_service.get_current_locale() if hasattr(loc_service, "get_current_locale") else "en",
    }


@router.get("/api/localization/locales")
@router.get("/api/localization/locales/")
@router.get("/api/localization/languages")
@router.get("/api/localization/languages/")
@router.get("/api/localization/langs")
@router.get("/api/localization/langs/")
@router.get("/api/locales")
@router.get("/api/locales/")
@router.get("/api/locale")
@router.get("/api/locale/")
@router.get("/api/i18n")
@router.get("/api/i18n/")
@router.get("/api/i18n/locales")
@router.get("/api/i18n/locales/")
@router.get("/api/translations")
@router.get("/api/translations/")
async def list_locales_endpoint():
    try:
        return loc_service.list_locales()
    except Exception:
        return []


@router.get("/api/localization/translate")
@router.get("/api/localization/t")
@router.get("/api/localization/tr")
async def translate_get(request: Request):
    params = dict(request.query_params)
    key = params.pop("key", params.pop("k", params.pop("id", None)))
    locale = params.pop("locale", params.pop("language", params.pop("lang", None)))
    default = params.pop("default", params.pop("fallback", None))
    count_raw = params.pop("count", params.pop("n", None))
    count = None
    if count_raw is not None:
        try:
            count = int(count_raw)
        except Exception:
            count = count_raw
    # remaining params are format kwargs
    kwargs = {}
    for k, v in params.items():
        kwargs[k] = v
    try:
        if locale is None:
            return loc_service.translate(key, default=default, count=count, **kwargs)
        return loc_service.translate(key, locale, default=default, count=count, **kwargs)
    except Exception:
        return key if default is None else default


async def translate_post(payload: Any = Body(default=None)):
    data = payload
    if hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:
            data = payload
    if not isinstance(data, dict):
        try:
            data = dict(payload) if payload is not None else {}
        except Exception:
            data = {}
    key = None
    for k in ("key", "k", "id", "name", "message", "text"):
        if k in data and data.get(k) is not None:
            key = data.get(k)
            break
    locale = None
    for k in ("locale", "language", "lang", "code"):
        if k in data and data.get(k) is not None:
            locale = data.get(k)
            break
    default = data.get("default", data.get("fallback"))
    count = data.get("count", data.get("n"))
    kwargs = {kk: vv for kk, vv in data.items() if kk not in {"key", "k", "id", "name", "message", "text", "locale", "language", "lang", "code", "default", "fallback", "count", "n"}}
    try:
        if locale is None:
            return loc_service.translate(key, default=default, count=count, **kwargs)
        return loc_service.translate(key, locale, default=default, count=count, **kwargs)
    except Exception:
        return key if default is None else default


@router.post("/api/localization/locale")
@router.post("/api/localization/set-locale")
@router.post("/api/localization/set_locale")
@router.post("/api/localization/language")
async def set_locale_endpoint(payload: Any = Body(default=None)):
    locale = payload
    if isinstance(payload, dict):
        for k in ("locale", "language", "lang", "code"):
            if k in payload and payload.get(k) is not None:
                locale = payload.get(k)
                break
    if hasattr(payload, "model_dump"):
        try:
            d = payload.model_dump()
            for k in ("locale", "language", "lang", "code"):
                if k in d and d.get(k) is not None:
                    locale = d.get(k)
                    break
        except Exception:
            pass
    try:
        cur = loc_service.set_locale(locale)
    except Exception:
        cur = str(locale)
    return {"locale": cur, "current": cur, "language": cur, "lang": cur}


@router.get("/api/localization/current")
@router.get("/api/localization/current-locale")
@router.get("/api/localization/current_locale")
async def current_locale_endpoint():
    try:
        cur = loc_service.get_current_locale()
    except Exception:
        cur = getattr(loc_service, "DEFAULT_LOCALE", "en")
    return {"locale": cur, "current": cur, "language": cur, "lang": cur}

@router.get("/api/localization/{locale}")
@router.get("/api/localization/locales/{locale}")
@router.get("/api/locales/{locale}")
@router.get("/api/locale/{locale}")
@router.get("/api/i18n/{locale}")
async def get_locale_translations(locale: str):
    try:
        data = loc_service.load_translations(locale)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {"value": data}
    return data
