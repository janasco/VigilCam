from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

SOURCES_XML_PATH = Path("/opt/others/ispy/XML/Sources.xml")
ENV_PATHS = ("ISPY_SOURCES_XML", "SOURCES_XML", "ISPY_SOURCES_PATH", "SOURCE_PRESETS_XML", "SOURCE_PRESETS_PATH")


def _resolve_path(path: Optional[Any] = None) -> Path:
    if path is not None:
        try:
            p = Path(path)
            if p.exists():
                return p
            # allow passing an object with .path
            if hasattr(path, "path"):
                p = Path(path.path)
                if p.exists():
                    return p
        except Exception:  # noqa: BLE001
            pass
    # kwargs handled by callers; env
    for key in ENV_PATHS:
        val = os.getenv(key)
        if val:
            p = Path(val)
            if p.exists():
                return p
    for cand in (SOURCES_XML_PATH, Path("/opt/others/ispy/XML/Sources.xml"), Path("Sources.xml"), Path("sources.xml")):
        if cand.exists():
            return cand
    return SOURCES_XML_PATH


def _path_from_args(args: Any, kwargs: Any) -> Optional[Any]:
    if kwargs:
        for key in ("path", "file", "xml", "xml_path", "source", "filename", "filepath"):
            if kwargs.get(key) is not None:
                return kwargs.get(key)
    if args:
        for a in args:
            if isinstance(a, (str, Path)):
                return a
            if isinstance(a, dict):
                for key in ("path", "file", "xml", "xml_path", "source", "filename", "filepath"):
                    if a.get(key) is not None:
                        return a.get(key)
    return None


def _attr(url: Any, name: str) -> Any:
    # support both attribute name and lowercase
    for candidate in (name, name.capitalize(), name.upper(), name.lower()):
        try:
            v = url.get(candidate)
        except Exception:  # noqa: BLE001
            v = None
        if v is not None:
            return v
    return None


def _norm_port(value: Any) -> Any:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _preset_from_url(manufacturer: str, url: Any) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "manufacturer": manufacturer,
        "name": manufacturer,
        "version": _attr(url, "version"),
        "confirmed": _attr(url, "confirmed"),
        "prefix": _attr(url, "prefix"),
        "source": _attr(url, "Source") or _attr(url, "source"),
        "url": _attr(url, "url"),
        "fixed": _attr(url, "fixed"),
        "audio_model": _attr(url, "AudioModel") or _attr(url, "audioModel") or _attr(url, "audio_model"),
        "flags": _attr(url, "flags"),
        "port": _norm_port(_attr(url, "port")),
        "cookies": _attr(url, "cookies"),
        "audio_source": _attr(url, "AudioSource") or _attr(url, "audioSource") or _attr(url, "audio_source"),
        "audio_url": _attr(url, "AudioURL") or _attr(url, "audioURL") or _attr(url, "audio_url"),
        "id": _attr(url, "id"),
        "token_path": _attr(url, "tokenPath") or _attr(url, "token_path"),
        "token_post": _attr(url, "tokenPost") or _attr(url, "token_post"),
        "token_port": _norm_port(_attr(url, "tokenPort") or _attr(url, "token_port")),
    }
    # alias keys for compatibility
    item["Source"] = item["source"]
    item["Url"] = item["url"]
    item["Prefix"] = item["prefix"]
    item["Version"] = item["version"]
    item["Port"] = item["port"]
    return item


def parse_source_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Parse Sources.xml into a dict mapping manufacturer -> list of preset dicts."""
    path = _path_from_args(args, kwargs)
    p = _resolve_path(path)
    if not p.exists():
        return {}
    try:
        tree = ET.parse(str(p))
    except Exception:  # noqa: BLE001
        return {}
    root = tree.getroot()
    out: Dict[str, List[Dict[str, Any]]] = {}
    for mfr in root.findall(".//Manufacturer"):
        manufacturer = mfr.get("name") or mfr.get("manufacturer") or mfr.get("title") or ""
        presets = out.setdefault(manufacturer, [])
        for url in mfr.findall("url"):
            presets.append(_preset_from_url(manufacturer, url))
    # drop empty keys? keep
    return out


def parse_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def parse(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def load_source_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def load_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def read_source_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def read_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def get_source_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def get_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def get_categorized_source_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def get_categorized_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def categorized_source_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def categorized_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def source_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def get_all(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def fetch_source_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def fetch_presets(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def parse_sources(*args: Any, **kwargs: Any) -> Dict[str, List[Dict[str, Any]]]:
    return parse_source_presets(*args, **kwargs)


def _category_obj(name: str, presets: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "category": name,
        "manufacturer": name,
        "name": name,
        "title": name,
        "label": name,
        "key": name,
        "presets": presets,
        "items": presets,
        "sources": presets,
        "urls": presets,
        "entries": presets,
        "values": presets,
        "count": len(presets),
        "total": len(presets),
    }


def list_source_presets(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    data = parse_source_presets(*args, **kwargs)
    return [_category_obj(k, v) for k, v in data.items()]


def list_presets(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_source_presets(*args, **kwargs)


def list_categories(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_source_presets(*args, **kwargs)


def list_categorized_source_presets(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_source_presets(*args, **kwargs)


def list_categorized_presets(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_source_presets(*args, **kwargs)


def get_categories(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_source_presets(*args, **kwargs)


def get_list(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_source_presets(*args, **kwargs)


def categories(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_source_presets(*args, **kwargs)


# Flat compatibility (delegating to categorized flatten)
def list_flat(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    data = parse_source_presets(*args, **kwargs)
    out: List[Dict[str, Any]] = []
    for mfr, presets in data.items():
        out.extend(presets)
    return out


def flatten(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_flat(*args, **kwargs)


def all_presets(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_flat(*args, **kwargs)


__all__ = [
    "SOURCES_XML_PATH",
    "ENV_PATHS",
    "parse_source_presets",
    "parse_presets",
    "parse",
    "load_source_presets",
    "load_presets",
    "read_source_presets",
    "read_presets",
    "get_source_presets",
    "get_presets",
    "get_categorized_source_presets",
    "get_categorized_presets",
    "categorized_source_presets",
    "categorized_presets",
    "source_presets",
    "presets",
    "get_all",
    "fetch_source_presets",
    "fetch_presets",
    "parse_sources",
    "list_source_presets",
    "list_presets",
    "list_categories",
    "list_categorized_source_presets",
    "list_categorized_presets",
    "get_categories",
    "get_list",
    "categories",
    "list_flat",
    "flatten",
    "all_presets",
]
