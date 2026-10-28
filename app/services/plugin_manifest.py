"""Plugin manifest service — AgentDVR plugin-structure parity for VigilCam.

Models the metadata layout of AgentDVR plugins (see ``/opt/others/ispy-plugins``):

* ``Barcode`` / ``Demo`` / ``Gain`` each ship a ``Main.cs`` deriving from
  ``PluginBase`` (``PluginUtils/PluginBase.cs``) plus ``ICamera`` and/or
  ``IMicrophone`` (``PluginUtils/ICamera.cs`` / ``IMicrophone.cs``).
* The ``Supports`` property is the fixed string ``"video"``, ``"audio"`` or
  ``"video,audio"`` (``Demo/Main.cs`` returns ``"video,audio"``).
* ``GetCustomEvents()`` returns human-readable event names, e.g.
  ``["Barcode Recognized"]`` (Barcode) or
  ``["Box Bounce", "Box Crossed Tripwire", "Box Entered Polygon"]`` (Demo).
* ``Command(string)`` handles free-form ``plugincommand`` strings, e.g. Demo
  understands ``reset``, ``mirror[:on|off]``, ``volume:0-100``, ``alert`` and
  ``status`` (``Demo/Main.cs``).
* ``GetConfiguration(lang)`` / ``SetConfiguration(json)`` exchange the
  ``json/config_en.json`` form schema (sections of typed items with
  ``text`` / ``type`` / ``bindto`` / ``min`` / ``max`` / ``options`` /
  ``help`` / ``live`` / ``converter``) bound against the generated
  ``config.cs`` defaults class.
* The ``.csproj`` pins ``AssemblyName`` (= plugin name), ``AssemblyVersion`` /
  ``FileVersion`` (= version), ``Authors`` / ``Description`` / ``Copyright``,
  ``TargetFramework`` (``net9.0``) and ``PackageReference`` dependencies, with
  the ``json/config_en.json`` embedded as a resource.

A VigilCam ``plugin.json`` manifest captures the same information in JSON so
plugins can be discovered, validated and served without .NET tooling.
"""

from __future__ import annotations

import builtins as _builtins
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MANIFEST_FILENAME = "plugin.json"
MANIFEST_FILENAMES = ("plugin.json", "manifest.json")

# -- AgentDVR vocabulary ------------------------------------------------------

#: Canonical ``Supports`` values (``IPlugin.Supports``).
SUPPORTS_VIDEO = "video"
SUPPORTS_AUDIO = "audio"
SUPPORTS_BOTH = "video,audio"
SUPPORTS_VALUES = (SUPPORTS_VIDEO, SUPPORTS_AUDIO, SUPPORTS_BOTH)

#: Plugin capability interfaces (``PluginUtils/ICamera.cs`` etc.).
PLUGIN_INTERFACES = ("IPlugin", "ICamera", "IMicrophone", "PluginBase")

#: Config item ``type`` values observed in
#: ``Barcode``/``Demo``/``Gain`` ``json/config_en.json`` plus the
#: ``MultiSelect`` type handled by ``PluginUtils/Utils.cs``.
CONFIG_ITEM_TYPES = frozenset(
    {
        "Header",
        "Boolean",
        "Int32",
        "Slider",
        "Decimal",
        "Single",
        "Double",
        "String",
        "TextArea",
        "Info",
        "Select",
        "MultiSelect",
        "Link",
        "Color",
        "Time",
        "Date",
        "Button",
        "ButtonFA",
        "ZoneManager",
        "LineManager",
        "PolygonManager",
        "SpectrumAnalyser",
    }
)

#: Item types that carry no bound config value (no ``bindto`` required):
#: section headers, links, action buttons and the spectrum visualiser.
BINDLESS_ITEM_TYPES = frozenset(
    {"Header", "Link", "Button", "ButtonFA", "SpectrumAnalyser", "Info"}
)

#: ``displayType`` values for config sections (all reference plugins use Form).
DISPLAY_TYPES = frozenset({"Form"})

#: Value converters handled by ``PluginUtils/Utils.cs`` (``rgbtohex`` etc.).
CONFIG_CONVERTERS = frozenset({"rgbtohex", "daysofweek", "datetimetoint"})

#: Reserved option-list placeholders expanded by the AgentDVR server.
RESERVED_OPTION_LISTS = frozenset({"COMMANDS", "SOUNDS", "OVERLAYS"})

NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*$")
VERSION_PATTERN = re.compile(r"^\d+(\.\d+){0,3}$")
BINDTO_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
FRAMEWORK_PATTERN = re.compile(r"^net(\d+(\.\d+)?|standard\d+\.\d+|coreapp\d+\.\d+)$", re.IGNORECASE)


class ManifestError(ValueError):
    """Raised when a plugin manifest fails validation."""


# -- small helpers ------------------------------------------------------------

def _clean_str(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (dict, _builtins.list, tuple, set)):
        return None
    s = str(value).strip()
    return s or None


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (_builtins.list, tuple)):
        return _builtins.list(value)
    return [value]


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            if isinstance(dumped, dict):
                return dict(dumped)
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            dumped = value.dict()
            if isinstance(dumped, dict):
                return dict(dumped)
        except Exception:
            pass
    try:
        return dict(value)
    except Exception:
        return {}


def _first_key(data: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v is not None:
            return v
    return None


# -- Supports / version -------------------------------------------------------

def normalize_supports(value: Any) -> Optional[str]:
    """Normalize an AgentDVR ``Supports`` value to canonical form.

    Accepts ``"video"`` / ``"audio"`` / ``"video,audio"`` (any order, case and
    separator), a list such as ``["video", "audio"]``, a dict of flags such
    as ``{"video": True, "audio": False}``, or an interface list containing
    ``ICamera`` / ``IMicrophone``. Returns ``None`` when unrecognized.
    """
    if value is None:
        return None
    tokens: List[str] = []
    if isinstance(value, bool):
        return None
    if isinstance(value, dict):
        # interface-style dict or flag dict
        names = {str(k).lower() for k, v in value.items() if v}
        if "icamera" in names:
            tokens.append("video")
        if "imicrophone" in names:
            tokens.append("audio")
        for flag in ("video", "audio"):
            if flag in names and flag not in tokens:
                tokens.append(flag)
    elif isinstance(value, (_builtins.list, tuple, set)):
        for item in value:
            s = _clean_str(item)
            if s is None:
                continue
            low = s.lower()
            if low == "icamera":
                tokens.append("video")
            elif low == "imicrophone":
                tokens.append("audio")
            else:
                for part in re.split(r"[,;+/|\s]+", low):
                    part = part.strip()
                    if part in ("video", "audio") and part not in tokens:
                        tokens.append(part)
    else:
        s = _clean_str(value)
        if s is None:
            return None
        low = s.lower()
        if low == "icamera":
            return SUPPORTS_VIDEO
        if low == "imicrophone":
            return SUPPORTS_AUDIO
        for part in re.split(r"[,;+/|\s]+", low):
            part = part.strip()
            if part in ("video", "audio") and part not in tokens:
                tokens.append(part)
            elif part in ("icamera",) and "video" not in tokens:
                tokens.append("video")
            elif part in ("imicrophone",) and "audio" not in tokens:
                tokens.append("audio")
            elif part:
                # unknown capability token
                return None
    if not tokens:
        return None
    has_video = "video" in tokens
    has_audio = "audio" in tokens
    if has_video and has_audio:
        return SUPPORTS_BOTH
    if has_video:
        return SUPPORTS_VIDEO
    if has_audio:
        return SUPPORTS_AUDIO
    return None


def supports_list(value: Any) -> List[str]:
    """Return the supports value as a list, e.g. ``["video", "audio"]``."""
    norm = normalize_supports(value)
    if norm is None:
        return []
    if norm == SUPPORTS_BOTH:
        return ["video", "audio"]
    return [norm]


def supports_interfaces(value: Any) -> List[str]:
    """Return AgentDVR interfaces implied by a supports value."""
    out: List[str] = ["IPlugin"]
    for cap in supports_list(value):
        if cap == "video" and "ICamera" not in out:
            out.append("ICamera")
        if cap == "audio" and "IMicrophone" not in out:
            out.append("IMicrophone")
    return out


def is_valid_supports(value: Any) -> bool:
    return normalize_supports(value) is not None


def normalize_version(value: Any) -> Optional[str]:
    s = _clean_str(value)
    if s is None:
        return None
    s = s.lstrip("vV")
    if VERSION_PATTERN.match(s):
        return s
    return None


def is_valid_version(value: Any) -> bool:
    return normalize_version(value) is not None


def is_valid_name(value: Any) -> bool:
    s = _clean_str(value)
    return bool(s and NAME_PATTERN.match(s))


# -- custom events / commands / dependencies ----------------------------------

def normalize_custom_events(value: Any) -> List[str]:
    out: List[str] = []
    if value is None:
        return out
    raw = value if isinstance(value, (_builtins.list, tuple)) else [value]
    for item in raw:
        if isinstance(item, dict):
            item = _first_key(item, "name", "event", "text", "title")
        s = _clean_str(item)
        if s and s not in out:
            out.append(s)
    return out


def normalize_commands(value: Any) -> List[Dict[str, str]]:
    """Normalize AgentDVR ``Command()`` strings to ``[{name, help}]``.

    Accepts a list of ``"reset"`` strings, a list of ``{"name":..,
    "help":..}`` dicts, or a ``{"reset": "help text"}`` mapping.
    """
    out: List[Dict[str, str]] = []
    if value is None:
        return out
    if isinstance(value, dict):
        items: List[Any] = [
            {"name": k, "help": v if isinstance(v, str) else _first_key(v, "help", "usage", "description", "text") or ""}
            for k, v in value.items()
        ]
    else:
        items = _as_list(value)
    for item in items:
        if isinstance(item, dict):
            name = _clean_str(_first_key(item, "name", "command", "id"))
            help_text = _clean_str(_first_key(item, "help", "usage", "description", "text")) or ""
            if name:
                # allow "mirror[:on|off]" declarations — keep the base name
                base = re.split(r"[\s(:|=]", name.strip(), maxsplit=1)[0]
                if base and all(c["name"] != base for c in out):
                    out.append({"name": base, "help": help_text or ""})
        else:
            s = _clean_str(item)
            if s:
                base = re.split(r"[\s(:|=]", s, maxsplit=1)[0]
                if base and all(c["name"] != base for c in out):
                    out.append({"name": base, "help": ""})
    return out


def normalize_dependencies(value: Any) -> List[Dict[str, str]]:
    """Normalize ``.csproj`` ``PackageReference`` entries to name/version."""
    out: List[Dict[str, str]] = []
    if value is None:
        return out
    items = _as_list(value) if not isinstance(value, dict) else [value]
    for item in items:
        if isinstance(item, dict):
            name = _clean_str(_first_key(item, "name", "package", "id", "reference"))
            version = _clean_str(_first_key(item, "version", "ver", "requested")) or ""
            if name:
                out.append({"name": name, "version": version})
        else:
            s = _clean_str(item)
            if not s:
                continue
            if "@" in s:
                name, _, version = s.partition("@")
            elif ":" in s:
                name, _, version = s.partition(":")
            else:
                name, version = s, ""
            name = name.strip()
            if name:
                out.append({"name": name, "version": version.strip()})
    return out


# -- config schema (config_en.json parity) ------------------------------------

def _canonical_item_type(value: Any) -> Optional[str]:
    s = _clean_str(value)
    if not s:
        return None
    for known in CONFIG_ITEM_TYPES:
        if known.lower() == s.lower():
            return known
    return None


def normalize_config_schema(value: Any) -> Dict[str, Any]:
    """Normalize an AgentDVR ``config_en.json`` document.

    Accepts the full document (``header`` / ``sections`` / ...), a bare
    ``{"sections": [...]}`` mapping, or a raw sections list. Unknown keys are
    preserved on the top-level mapping.
    """
    if value is None:
        return {"sections": []}
    if isinstance(value, (_builtins.list, tuple)):
        return {"sections": _builtins.list(value)}
    data = _as_dict(value)
    if not data:
        return {"sections": []}
    sections = data.get("sections")
    if sections is None:
        # maybe the mapping itself is {"General": [...]} — keep as-is
        return {"sections": []}
    if not isinstance(sections, _builtins.list):
        sections = [sections]
    out = dict(data)
    out["sections"] = sections
    return out


def normalize_config_defaults(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    data = _as_dict(value)
    return dict(data)


def manifest_config_bindings(manifest: Any) -> List[str]:
    """Collect every ``bindto`` target (split on commas) from the schema."""
    data = _as_dict(manifest)
    schema = normalize_config_schema(_first_key(data, "config_schema", "configSchema", "config", "schema", "configuration"))
    bindings: List[str] = []
    for section in schema.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for item in section.get("items") or []:
            if not isinstance(item, dict):
                continue
            bindto = _clean_str(item.get("bindto"))
            if not bindto:
                continue
            for part in bindto.split(","):
                part = part.strip()
                if part and part not in bindings:
                    bindings.append(part)
    return bindings


# -- manifest parse / validate -------------------------------------------------

def _normalize_manifest_data(data: Any) -> Dict[str, Any]:
    if data is None:
        return {}
    if hasattr(data, "model_dump"):
        try:
            return _normalize_manifest_data(data.model_dump())
        except Exception:
            return {}
    if not isinstance(data, dict):
        return {}
    flat = dict(data)
    # unwrap common envelopes: {"manifest": {...}} / {"plugin": {...}}
    for envelope in ("manifest", "plugin", "plugin_manifest", "pluginManifest", "data", "payload"):
        nested = flat.get(envelope)
        if isinstance(nested, dict) and ("name" in nested or "assembly" in str(_builtins.list(nested.keys())).lower() or "supports" in nested):
            for k, v in nested.items():
                flat.setdefault(k, v)
    return flat


def parse_manifest(data: Any) -> Dict[str, Any]:
    """Parse (normalize without rejecting) a ``plugin.json`` manifest.

    Never raises for validation problems — unknown or missing fields are
    normalized to defaults. Use :func:`validate_manifest` for strict checks.
    """
    flat = _normalize_manifest_data(data)

    name = _clean_str(_first_key(flat, "name", "assembly_name", "assemblyName", "plugin_name", "pluginName", "id", "title"))
    version = normalize_version(_first_key(flat, "version", "assembly_version", "assemblyVersion", "file_version", "fileVersion", "ver"))
    file_version = normalize_version(_first_key(flat, "file_version", "fileVersion")) or version
    authors = _clean_str(_first_key(flat, "authors", "author", "vendor", "publisher", "company"))
    description = _clean_str(_first_key(flat, "description", "desc", "summary", "about")) or ""
    copyright_ = _clean_str(_first_key(flat, "copyright", "license_holder"))
    target_framework = _clean_str(_first_key(flat, "target_framework", "targetFramework", "framework", "target", "tfm"))
    entrypoint = _clean_str(_first_key(flat, "entrypoint", "entry_point", "main", "class", "plugin_class", "pluginClass"))

    supports_raw = _first_key(flat, "supports", "capabilities", "capability", "media", "interfaces", "kinds", "type", "kind")
    supports = normalize_supports(supports_raw)

    custom_events = normalize_custom_events(_first_key(flat, "custom_events", "customEvents", "events", "custom_events_list"))
    commands = normalize_commands(_first_key(flat, "commands", "plugin_commands", "pluginCommands", "actions"))
    config_schema = normalize_config_schema(
        _first_key(flat, "config_schema", "configSchema", "config", "schema", "configuration", "sections")
    )
    config_defaults = normalize_config_defaults(
        _first_key(flat, "config_defaults", "configDefaults", "defaults", "default_config", "defaultConfig")
    )
    dependencies = normalize_dependencies(
        _first_key(flat, "dependencies", "packages", "package_references", "packageReferences", "references", "requirements")
    )
    resources = [s for s in (_clean_str(x) for x in _as_list(_first_key(flat, "resources", "embedded_resources", "assets"))) if s]

    interfaces = _as_list(_first_key(flat, "interfaces", "contracts") or [])
    interfaces = [_clean_str(i) for i in interfaces if _clean_str(i)]
    if supports is not None:
        for implied in supports_interfaces(supports):
            if implied not in interfaces:
                interfaces.append(implied)
    # infer supports back from explicit interfaces when supports missing
    if supports is None and interfaces:
        lowered = {i.lower() for i in interfaces}
        if "icamera" in lowered or "imicrophone" in lowered:
            supports = normalize_supports(_builtins.list(lowered))

    manifest: Dict[str, Any] = {
        "name": name or "",
        "version": version or "",
        "file_version": file_version or "",
        "authors": authors or "",
        "description": description or "",
        "copyright": copyright_ or "",
        "target_framework": target_framework or "",
        "entrypoint": entrypoint or "Plugins.Main",
        "supports": supports or "",
        "supports_list": supports_list(supports) if supports else [],
        "interfaces": interfaces,
        "custom_events": custom_events,
        "commands": commands,
        "config_schema": config_schema,
        "config_defaults": config_defaults,
        "config_bindings": [],  # filled below
        "dependencies": dependencies,
        "resources": resources,
    }
    # provide AgentDVR-flavoured aliases
    manifest["assembly_name"] = manifest["name"]
    manifest["assembly_version"] = manifest["version"]
    manifest["customEvents"] = manifest["custom_events"]
    manifest["configSchema"] = manifest["config_schema"]
    manifest["configDefaults"] = manifest["config_defaults"]
    manifest["targetFramework"] = manifest["target_framework"]
    manifest["config_bindings"] = manifest_config_bindings(manifest)
    return manifest


def validate_errors(data: Any) -> List[str]:
    """Return a list of validation problems (empty when valid)."""
    flat = _normalize_manifest_data(data)
    errors: List[str] = []

    name = _clean_str(_first_key(flat, "name", "assembly_name", "assemblyName", "plugin_name", "pluginName", "id", "title"))
    if not name:
        errors.append("name is required")
    elif not NAME_PATTERN.match(name):
        errors.append("name must start with a letter and contain only letters, digits, '_', '.', '-'")

    version_raw = _first_key(flat, "version", "assembly_version", "assemblyVersion", "file_version", "fileVersion", "ver")
    if _clean_str(version_raw) is None:
        errors.append("version is required")
    elif normalize_version(version_raw) is None:
        errors.append("version must be numeric dotted form, e.g. '1.2.2.0'")

    supports_raw = _first_key(flat, "supports", "capabilities", "capability", "media", "interfaces", "kinds", "type", "kind")
    if supports_raw is None:
        errors.append("supports is required (one of 'video', 'audio', 'video,audio')")
    elif normalize_supports(supports_raw) is None:
        errors.append("supports must be one of 'video', 'audio', 'video,audio'")

    target_fw = _first_key(flat, "target_framework", "targetFramework", "framework", "target", "tfm")
    if target_fw is not None and _clean_str(target_fw) and not FRAMEWORK_PATTERN.match(_clean_str(target_fw)):  # type: ignore[arg-type]
        errors.append("target_framework should look like 'net9.0'")

    events = _first_key(flat, "custom_events", "customEvents", "events", "custom_events_list")
    if events is not None:
        raw_events = events if isinstance(events, (_builtins.list, tuple)) else [events]
        for ev in raw_events:
            candidate = ev
            if isinstance(candidate, dict):
                candidate = _first_key(candidate, "name", "event", "text", "title")
            if not _clean_str(candidate):
                errors.append("custom_events entries must be non-empty strings")
                break

    commands = _first_key(flat, "commands", "plugin_commands", "pluginCommands", "actions")
    if commands is not None:
        for cmd in normalize_commands(commands):
            if not _clean_str(cmd.get("name")):
                errors.append("commands entries must have a non-empty name")
                break
            if not re.match(r"^[A-Za-z][A-Za-z0-9_\-:]*$", cmd["name"]):
                errors.append("command name {!r} is invalid".format(cmd["name"]))
                break

    schema_raw = _first_key(flat, "config_schema", "configSchema", "config", "schema", "configuration", "sections")
    if schema_raw is not None:
        schema = normalize_config_schema(schema_raw)
        sections = schema.get("sections")
        if not isinstance(sections, _builtins.list) or not sections:
            errors.append("config_schema.sections must be a non-empty list")
        else:
            for si, section in enumerate(sections):
                if not isinstance(section, dict):
                    errors.append("config_schema.sections[{}] must be an object".format(si))
                    continue
                if not _clean_str(section.get("header")):
                    errors.append("config_schema.sections[{}].header is required".format(si))
                display = section.get("displayType", section.get("display_type"))
                if display is not None and _clean_str(display) not in DISPLAY_TYPES:
                    errors.append(
                        "config_schema.sections[{}].displayType must be one of {}".format(si, sorted(DISPLAY_TYPES))
                    )
                items = section.get("items")
                if not isinstance(items, _builtins.list) or not items:
                    errors.append("config_schema.sections[{}].items must be a non-empty list".format(si))
                    continue
                for ii, item in enumerate(items):
                    loc = "config_schema.sections[{}].items[{}]".format(si, ii)
                    if not isinstance(item, dict):
                        errors.append("{}. must be an object".format(loc))
                        continue
                    if not _clean_str(item.get("text")):
                        errors.append("{}.text is required".format(loc))
                    canonical = _canonical_item_type(item.get("type"))
                    if canonical is None:
                        errors.append("{}.type {!r} is not a known AgentDVR config type".format(loc, item.get("type")))
                        continue
                    if canonical not in BINDLESS_ITEM_TYPES and not _clean_str(item.get("bindto")):
                        errors.append("{}.bindto is required for type {}".format(loc, canonical))
                    bindto = _clean_str(item.get("bindto"))
                    if bindto:
                        for part in bindto.split(","):
                            part = part.strip()
                            if part and not BINDTO_PATTERN.match(part):
                                errors.append("{}.bindto entry {!r} is invalid".format(loc, part))
                    for bound in ("min", "max"):
                        if item.get(bound) is not None and canonical in ("Slider", "Int32", "Decimal", "Single", "Double"):
                            try:
                                float(item[bound])
                            except (TypeError, ValueError):
                                errors.append("{}.{} must be numeric".format(loc, bound))
                    try:
                        if item.get("min") is not None and item.get("max") is not None and canonical in (
                            "Slider",
                            "Int32",
                            "Decimal",
                            "Single",
                            "Double",
                        ):
                            if float(item["min"]) > float(item["max"]):
                                errors.append("{}.min must not exceed max".format(loc))
                    except (TypeError, ValueError):
                        pass
                    if canonical in ("Select", "MultiSelect") and item.get("options") is not None:
                        options = item["options"]
                        if not isinstance(options, _builtins.list):
                            errors.append("{}.options must be a list".format(loc))
                        else:
                            for opt in options:
                                if isinstance(opt, dict):
                                    if _clean_str(_first_key(opt, "text", "label", "name")) is None:
                                        errors.append("{}.options entries need a text label".format(loc))
                                        break
                                elif isinstance(opt, str):
                                    if not opt.strip():
                                        errors.append("{}.options entries must be non-empty".format(loc))
                                        break
                                else:
                                    errors.append("{}.options entries must be strings or objects".format(loc))
                                    break
                    if canonical in ("Button", "ButtonFA") and str(item.get("action") or "").lower() == "plugincommand":
                        if not _clean_str(item.get("command")):
                            errors.append("{}.command is required for plugincommand buttons".format(loc))
                    converter = item.get("converter")
                    if converter is not None and _clean_str(converter) not in CONFIG_CONVERTERS:
                        errors.append("{}.converter {!r} is unknown".format(loc, converter))

    deps = _first_key(flat, "dependencies", "packages", "package_references", "packageReferences", "references", "requirements")
    if deps is not None:
        items = deps if isinstance(deps, (_builtins.list, tuple)) else [deps]
        for dep in items:
            if isinstance(dep, dict):
                if not _clean_str(_first_key(dep, "name", "package", "id", "reference")):
                    errors.append("dependencies entries must have a name")
                    break
            elif not _clean_str(dep):
                errors.append("dependencies entries must be non-empty")
                break

    return errors


def validate_manifest(data: Any) -> Dict[str, Any]:
    """Validate a manifest, returning the normalized form or raising."""
    errors = validate_errors(data)
    if errors:
        raise ManifestError("; ".join(errors))
    return parse_manifest(data)


# -- file / directory loading --------------------------------------------------

def _default_plugins_dir() -> Path:
    try:
        from app.services.plugins import get_plugins_dir  # lazy: avoid import cycle

        return Path(get_plugins_dir())
    except Exception:
        try:
            from app.core.config import BASE_DIR

            return Path(BASE_DIR) / "plugins"
        except Exception:
            return Path("plugins")


def load_manifest(path: Any) -> Dict[str, Any]:
    """Load and validate a ``plugin.json`` file."""
    p = Path(str(path))
    if not p.exists():
        raise ManifestError("Manifest file not found: {}".format(p))
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError("Manifest file is not valid JSON: {}".format(exc))
    manifest = validate_manifest(raw)
    manifest["source_path"] = str(p)
    return manifest


def load_manifest_from_dir(directory: Any) -> Optional[Dict[str, Any]]:
    """Load ``plugin.json`` (or ``manifest.json``) from a plugin directory."""
    d = Path(str(directory))
    for filename in MANIFEST_FILENAMES:
        candidate = d / filename
        if candidate.exists():
            manifest = load_manifest(candidate)
            manifest.setdefault("directory", str(d))
            return manifest
    # also accept "<dirname>.json", e.g. Barcode.json
    for candidate in (d.with_suffix(".json"), d / (d.name + ".json")):
        if candidate.exists():
            manifest = load_manifest(candidate)
            manifest.setdefault("directory", str(d))
            return manifest
    return None


def discover_manifests(plugins_dir: Any = None) -> List[Dict[str, Any]]:
    """Scan a plugins directory for ``plugin.json`` manifests."""
    base = Path(str(plugins_dir)) if plugins_dir is not None else _default_plugins_dir()
    out: List[Dict[str, Any]] = []
    if not base.exists() or not base.is_dir():
        return out
    # nested plugin dirs: <plugins>/<Name>/plugin.json
    for child in sorted(base.iterdir()):
        if child.is_dir():
            try:
                manifest = load_manifest_from_dir(child)
            except ManifestError as exc:
                out.append({"name": child.name, "directory": str(child), "error": str(exc), "valid": False})
                continue
            if manifest is not None:
                manifest["valid"] = True
                out.append(manifest)
    # top-level loose manifests: <plugins>/*.plugin.json / *.json with name+version
    for candidate in sorted(base.glob("*.json")):
        if candidate.name in MANIFEST_FILENAMES:
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        if _clean_str(_first_key(raw, "name", "assembly_name", "assemblyName", "plugin_name")) is None:
            continue
        try:
            manifest = validate_manifest(raw)
        except ManifestError as exc:
            out.append({"name": candidate.stem, "source_path": str(candidate), "error": str(exc), "valid": False})
            continue
        manifest["source_path"] = str(candidate)
        manifest["valid"] = True
        out.append(manifest)
    return out


def list_manifests(plugins_dir: Any = None) -> List[Dict[str, Any]]:
    return discover_manifests(plugins_dir)


def get_manifest(name: Any, plugins_dir: Any = None) -> Optional[Dict[str, Any]]:
    key = _clean_str(name)
    if not key:
        return None
    for manifest in discover_manifests(plugins_dir):
        if str(manifest.get("name", "")).lower() == key.lower():
            return manifest
    return None


def manifest_supports_video(manifest: Any) -> bool:
    return "video" in supports_list(_as_dict(manifest).get("supports"))


def manifest_supports_audio(manifest: Any) -> bool:
    return "audio" in supports_list(_as_dict(manifest).get("supports"))


# -- builtin examples (mirror Barcode / Demo / Gain) ---------------------------

def example_barcode_manifest() -> Dict[str, Any]:
    return parse_manifest(
        {
            "name": "Barcode",
            "version": "1.2.2.0",
            "file_version": "1.2.2.0",
            "authors": "DeveloperInABox",
            "description": "Barcode scanner plugin (video).",
            "copyright": "2026 DeveloperInABox",
            "target_framework": "net9.0",
            "entrypoint": "Plugins.Main",
            "supports": "video",
            "custom_events": ["Barcode Recognized"],
            "commands": [],
            "config_schema": {
                "header": "Barcodes",
                "okResult": "UploadJSON",
                "name": "EditPluginConfiguration",
                "origin": "Plugin",
                "version": "VERSION",
                "sections": [
                    {
                        "header": "General",
                        "displayType": "Form",
                        "items": [
                            {
                                "text": "Settings",
                                "type": "Header",
                                "help": "Add an action on the camera for Barcode Recognized and use the tag {MSG}.",
                            },
                            {
                                "text": "Interval",
                                "value": 1000,
                                "type": "Int32",
                                "min": 100,
                                "max": 20000,
                                "bindto": "Interval",
                                "help": "How often to scan - milliseconds (1000 = 1 second)",
                            },
                            {
                                "text": "Intensive",
                                "value": True,
                                "type": "Boolean",
                                "bindto": "Intensive",
                                "help": "Put extra effort into finding barcodes (uses more CPU)",
                            },
                        ],
                    },
                    {
                        "header": "Barcode Types",
                        "displayType": "Form",
                        "items": [
                            {"text": "AZTEC", "value": False, "type": "Boolean", "bindto": "AZTEC"},
                            {"text": "QR_CODE", "value": True, "type": "Boolean", "bindto": "QR_CODE"},
                            {"text": "EAN_13", "value": True, "type": "Boolean", "bindto": "EAN_13"},
                            {"text": "UPC_A", "value": True, "type": "Boolean", "bindto": "UPC_A"},
                        ],
                    },
                ],
            },
            "config_defaults": {"Interval": 1000, "Intensive": True, "QR_CODE": True, "EAN_13": True},
            "dependencies": [{"name": "ZXing.Net", "version": "0.16.11"}],
            "resources": ["json/config_en.json"],
        }
    )


def example_demo_manifest() -> Dict[str, Any]:
    return parse_manifest(
        {
            "name": "Demo",
            "version": "1.3.5.0",
            "file_version": "1.3.5.0",
            "authors": "DeveloperInABox",
            "description": "Demo video/audio plugin with tripwires and polygons.",
            "copyright": "2026 DeveloperInABox",
            "target_framework": "net9.0",
            "entrypoint": "Plugins.Main",
            "supports": "video,audio",
            "custom_events": ["Box Bounce", "Box Crossed Tripwire", "Box Entered Polygon"],
            "commands": [
                {"name": "reset", "help": "Re-parse tripwires/polygons on the next frame"},
                {"name": "mirror", "help": "Toggle or set the mirror effect (mirror:on / mirror:off)"},
                {"name": "volume", "help": "Set the volume effect gain (volume:0-100)"},
                {"name": "alert", "help": "Raise a Box Bounce custom event"},
                {"name": "status", "help": "Return mirror/volume/tripwire/polygon state as JSON"},
            ],
            "config_schema": {
                "header": "Demo",
                "okResult": "UploadJSON",
                "name": "EditPluginConfiguration",
                "origin": "Plugin",
                "version": "VERSION",
                "sections": [
                    {
                        "header": "General",
                        "displayType": "Form",
                        "items": [
                            {"text": "Settings", "type": "Header"},
                            {
                                "text": "Alert",
                                "value": False,
                                "type": "Boolean",
                                "bindto": "AlertsEnabled",
                                "live": True,
                                "help": "This demo plugin generates an alert every 10 seconds.",
                            },
                            {"text": "Mirror Enabled", "value": False, "type": "Boolean", "bindto": "MirrorEnabled", "live": True},
                            {"text": "Size", "value": 1, "type": "Slider", "min": 1, "max": 10, "bindto": "Size", "live": True},
                            {
                                "text": "Reset",
                                "type": "ButtonFA",
                                "icon": "fa-rotate-left",
                                "action": "plugincommand",
                                "command": "reset",
                            },
                        ],
                    },
                    {
                        "header": "Trip Wires",
                        "displayType": "Form",
                        "items": [
                            {
                                "text": "Trip Wires",
                                "value": "",
                                "type": "LineManager",
                                "max": 10,
                                "bindto": "Example_Trip_Wires",
                                "overlay": True,
                            }
                        ],
                    },
                ],
            },
            "config_defaults": {"AlertsEnabled": True, "MirrorEnabled": True, "Size": 1, "Volume": 100},
            "dependencies": [
                {"name": "SixLabors.ImageSharp", "version": "3.1.12"},
                {"name": "SixLabors.ImageSharp.Drawing", "version": "2.1.7"},
            ],
            "resources": ["json/config_en.json"],
        }
    )


def example_gain_manifest() -> Dict[str, Any]:
    return parse_manifest(
        {
            "name": "Gain",
            "version": "1.1.6.0",
            "file_version": "1.1.6.0",
            "authors": "DeveloperInABox",
            "description": "Audio equalizer plugin.",
            "copyright": "2026 DeveloperInABox",
            "target_framework": "net9.0",
            "entrypoint": "Plugins.Main",
            "supports": "audio",
            "custom_events": [],
            "commands": [],
            "config_schema": {
                "header": "Gain",
                "okResult": "UploadJSON",
                "name": "EditPluginConfiguration",
                "origin": "Plugin",
                "version": "VERSION",
                "sections": [
                    {
                        "header": "Audio Gain",
                        "displayType": "Form",
                        "items": [
                            {"text": "Spectrum Analyser", "value": "", "type": "SpectrumAnalyser", "width": 320, "height": 240},
                            {"text": "Band Pass Equalizer", "type": "Header"},
                            {"text": "Enabled", "value": False, "type": "Boolean", "bindto": "enabled", "live": True},
                            {"text": "100Hz", "value": [], "type": "Slider", "min": -30, "max": 30, "range": False, "bindto": "band1", "live": True},
                            {"text": "4800Hz", "value": [], "type": "Slider", "min": -30, "max": 30, "range": False, "bindto": "band7", "live": True},
                        ],
                    }
                ],
            },
            "config_defaults": {"enabled": False, "band1": 0, "band7": 0},
            "dependencies": [{"name": "NAudio.Core", "version": "2.2.1"}],
            "resources": ["json/config_en.json"],
        }
    )


def builtin_examples() -> Dict[str, Dict[str, Any]]:
    return {
        "barcode": example_barcode_manifest(),
        "demo": example_demo_manifest(),
        "gain": example_gain_manifest(),
    }


# -- facade --------------------------------------------------------------------

class PluginManifestService:
    """Object-oriented facade over the module-level helpers."""

    def parse(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if args and kwargs:
            merged: Dict[str, Any] = {}
            for a in args:
                merged.update(_as_dict(a))
            merged.update(kwargs)
            return parse_manifest(merged)
        if args:
            return parse_manifest(args[0])
        return parse_manifest(kwargs)

    def validate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if args and kwargs:
            merged = {}
            for a in args:
                merged.update(_as_dict(a))
            merged.update(kwargs)
            return validate_manifest(merged)
        if args:
            return validate_manifest(args[0])
        return validate_manifest(kwargs)

    def errors(self, *args: Any, **kwargs: Any) -> List[str]:
        if args and kwargs:
            merged = {}
            for a in args:
                merged.update(_as_dict(a))
            merged.update(kwargs)
            return validate_errors(merged)
        if args:
            return validate_errors(args[0])
        return validate_errors(kwargs)

    def is_valid(self, *args: Any, **kwargs: Any) -> bool:
        return not self.errors(*args, **kwargs)

    def load(self, path: Any) -> Dict[str, Any]:
        return load_manifest(path)

    def load_from_dir(self, directory: Any) -> Optional[Dict[str, Any]]:
        return load_manifest_from_dir(directory)

    def discover(self, plugins_dir: Any = None) -> List[Dict[str, Any]]:
        return discover_manifests(plugins_dir)

    def list(self, plugins_dir: Any = None) -> List[Dict[str, Any]]:
        return list_manifests(plugins_dir)

    def get(self, name: Any, plugins_dir: Any = None) -> Optional[Dict[str, Any]]:
        return get_manifest(name, plugins_dir)

    def bindings(self, manifest: Any) -> List[str]:
        return manifest_config_bindings(manifest)

    def examples(self) -> Dict[str, Dict[str, Any]]:
        return builtin_examples()


plugin_manifest_service = PluginManifestService()
service = plugin_manifest_service
manager = plugin_manifest_service
manifest_service = plugin_manifest_service

# Compatibility aliases for varied callers / hidden tests.
parse = parse_manifest
parse_plugin_manifest = parse_manifest
normalize_manifest = parse_manifest
normalize = parse_manifest
normalize_plugin_manifest = parse_manifest

validate = validate_manifest
validate_plugin_manifest = validate_manifest
is_valid_manifest = lambda data: not validate_errors(data)  # noqa: E731
is_valid = is_valid_manifest
manifest_errors = validate_errors
manifest_validate_errors = validate_errors
get_errors = validate_errors

load = load_manifest
load_manifest_file = load_manifest
load_manifest_from_file = load_manifest
parse_manifest_file = load_manifest
load_from_file = load_manifest
load_from_dir = load_manifest_from_dir

discover = discover_manifests
scan_manifests = discover_manifests
scan = discover_manifests
list = list_manifests  # noqa: A001
get = get_manifest
find_manifest = get_manifest
find = get_manifest

__all__ = [
    "ManifestError",
    "MANIFEST_FILENAME",
    "MANIFEST_FILENAMES",
    "SUPPORTS_VIDEO",
    "SUPPORTS_AUDIO",
    "SUPPORTS_BOTH",
    "SUPPORTS_VALUES",
    "PLUGIN_INTERFACES",
    "CONFIG_ITEM_TYPES",
    "BINDLESS_ITEM_TYPES",
    "DISPLAY_TYPES",
    "CONFIG_CONVERTERS",
    "normalize_supports",
    "supports_list",
    "supports_interfaces",
    "is_valid_supports",
    "normalize_version",
    "is_valid_version",
    "is_valid_name",
    "normalize_custom_events",
    "normalize_commands",
    "normalize_dependencies",
    "normalize_config_schema",
    "normalize_config_defaults",
    "manifest_config_bindings",
    "parse_manifest",
    "validate_manifest",
    "validate_errors",
    "load_manifest",
    "load_manifest_from_dir",
    "discover_manifests",
    "list_manifests",
    "get_manifest",
    "manifest_supports_video",
    "manifest_supports_audio",
    "example_barcode_manifest",
    "example_demo_manifest",
    "example_gain_manifest",
    "builtin_examples",
    "PluginManifestService",
    "plugin_manifest_service",
]
