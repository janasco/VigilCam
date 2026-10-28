from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, List, Optional

SOURCES_XML_PATH = Path("/opt/others/ispy/XML/Sources.xml")
# aliases / env override
ENV_PATHS = ("ISPY_SOURCES_XML", "SOURCES_XML", "ISPY_SOURCES_PATH")


def _sources_path() -> Path:
    for key in ENV_PATHS:
        val = os.getenv(key)
        if val:
            p = Path(val)
            if p.exists():
                return p
    # common alternatives
    for cand in (SOURCES_XML_PATH, Path("/opt/others/ispy/XML/Sources.xml"), Path("Sources.xml")):
        if cand.exists():
            return cand
    return SOURCES_XML_PATH


def parse_sources(path: Optional[Any] = None) -> List[dict]:
    p = Path(path) if path is not None else _sources_path()
    if not p.exists():
        return []
    try:
        tree = ET.parse(str(p))
    except Exception:  # noqa: BLE001
        return []
    root = tree.getroot()
    out: List[dict] = []
    for mfr in root.findall(".//Manufacturer"):
        manufacturer = mfr.get("name") or mfr.get("manufacturer") or ""
        for url in mfr.findall("url"):
            item = {
                "manufacturer": manufacturer,
                "name": manufacturer,
                "version": url.get("version"),
                "source": url.get("Source") or url.get("source"),
                "prefix": url.get("prefix"),
                "url": url.get("url"),
                "port": url.get("port"),
            }
            # normalize port to int if possible
            try:
                if item["port"] is not None:
                    item["port"] = int(item["port"])
            except (TypeError, ValueError):
                pass
            out.append(item)
    return out


def list_sources(*args: Any, **kwargs: Any) -> List[dict]:
    path = kwargs.get("path") or kwargs.get("file") or (args[0] if args else None)
    return parse_sources(path)


def get_camera_source_presets(*args: Any, **kwargs: Any) -> List[dict]:
    return list_sources(*args, **kwargs)


def camera_source_presets(*args: Any, **kwargs: Any) -> List[dict]:
    return list_sources(*args, **kwargs)


# aliases
list_camera_sources = list_sources
sources = list_sources
presets = list_sources
camera_sources = list_sources
get_sources = list_sources
load_sources = parse_sources
read_sources = parse_sources

__all__ = [
    "SOURCES_XML_PATH",
    "parse_sources",
    "list_sources",
    "get_camera_source_presets",
    "camera_source_presets",
]
