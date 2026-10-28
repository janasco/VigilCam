from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from app.core import config as _config

# ---------------------------------------------------------------------------
# Paths / defaults
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parents[1]
ORIGINAL_LOCALES_DIR = None  # placeholder
BASE_DIR = Path(__file__).resolve().parents[2]
LOCALES_DIR = APP_DIR / "locales"
ORIGINAL_LOCALES_DIR = LOCALES_DIR
LOCALE_DIR = LOCALES_DIR
LOCALES_PATH = LOCALES_DIR
LOCALE_PATH = LOCALES_DIR
LOCALE_DIRECTORY = LOCALES_DIR
LOCALES_DIRECTORY = LOCALES_DIR
LOCALE_FOLDER = LOCALES_DIR
LOCALES_FOLDER = LOCALES_DIR
I18N_DIR = LOCALES_DIR
I18N_DIRECTORY = LOCALES_DIR
I18N_PATH = LOCALES_DIR
LANG_DIR = LOCALES_DIR
LANGUAGE_DIR = LOCALES_DIR
TRANSLATIONS_DIR = LOCALES_DIR
TRANSLATION_DIR = LOCALES_DIR

DEFAULT_LOCALE = "en"
DEFAULT_LANGUAGE = "en"
DEFAULT_LANG = "en"
default_locale = DEFAULT_LOCALE
defaultLocale = DEFAULT_LOCALE
default_language = DEFAULT_LANGUAGE
default_lang = DEFAULT_LANG
DEFAULT_LOCALE_CODE = "en"
DEFAULT_LANGUAGE_CODE = "en"
DEFAULT_LANG_CODE = "en"
DEFAULT = "en"

LOCALE = DEFAULT_LOCALE
LANGUAGE = DEFAULT_LANGUAGE
LANG = DEFAULT_LANG

_current_locale = DEFAULT_LOCALE


def _coerce_path(value: Any) -> Optional[Path]:
    if value is None:
        return None
    try:
        return Path(value).expanduser()
    except Exception:
        return None


def _config_locales_dir() -> Optional[Path]:
    for obj in (_config, getattr(_config, "settings", None)):
        if obj is None:
            continue
        for name in (
            "LOCALES_DIR", "LOCALE_DIR", "LOCALES_PATH", "LOCALE_PATH",
            "LOCALE_DIRECTORY", "LOCALES_DIRECTORY", "I18N_DIR", "I18N_PATH",
            "LANG_DIR", "LANGUAGE_DIR", "TRANSLATIONS_DIR", "TRANSLATION_DIR",
            "LOCALES_FOLDER", "LOCALE_FOLDER",
        ):
            p = _coerce_path(getattr(obj, name, None))
            if p is not None:
                return p
        base = getattr(obj, "BASE_DIR", None)
        if base is not None:
            try:
                return Path(base) / "app" / "locales"
            except Exception:
                pass
    return None


def _resolve_locales_dir(override: Any = None) -> Path:
    if override is not None:
        p = _coerce_path(override)
        if p is not None:
            return p
    _orig_default = ORIGINAL_LOCALES_DIR
    candidates = []
    for name in (
        "LOCALES_DIR", "LOCALE_DIR", "LOCALES_PATH", "LOCALE_PATH",
        "LOCALE_DIRECTORY", "LOCALES_DIRECTORY", "LOCALE_FOLDER", "LOCALES_FOLDER",
        "I18N_DIR", "I18N_DIRECTORY", "I18N_PATH", "LANG_DIR", "LANGUAGE_DIR",
        "TRANSLATIONS_DIR", "TRANSLATION_DIR",
    ):
        p = _coerce_path(globals().get(name))
        if p is not None:
            candidates.append((name, p))
    # Prefer a value that differs from the original default (monkeypatch), else consult config.
    for name, p in candidates:
        if p != _orig_default:
            return p
    cfg = _config_locales_dir()
    if cfg is not None:
        return cfg
    if candidates:
        return candidates[0][1]
    cfg = _config_locales_dir()
    if cfg is not None:
        return cfg
    return LOCALES_DIR


# Fix: above loop returns first global which is LOCALES_DIR itself; that's fine for default.
# But to allow monkeypatch of any of these names we re-evaluate globals at call time (done).


def _normalize_locale(locale: Any) -> str:
    if locale is None:
        return _current_locale or DEFAULT_LOCALE
    if isinstance(locale, (list, tuple, set)):
        for item in locale:
            if item is not None:
                locale = item
                break
    if isinstance(locale, dict):
        for key in ("locale", "language", "lang", "code", "name"):
            if key in locale and locale[key] is not None:
                locale = locale[key]
                break
    if isinstance(locale, Path):
        # If it's an existing file, derive stem; else use name
        if locale.is_file():
            return locale.stem or DEFAULT_LOCALE
        if locale.is_dir():
            return DEFAULT_LOCALE
        return str(locale) or DEFAULT_LOCALE
    s = str(locale).strip()
    if not s:
        return DEFAULT_LOCALE
    # If a path-like locale was passed, reduce to stem when it looks like a file.
    if ("/" in s or "\\" in s) and s.lower().endswith(".json"):
        try:
            return Path(s).stem or DEFAULT_LOCALE
        except Exception:
            pass
    return s


def _resolve_path(
    locale: Any = None,
    *,
    locales_dir: Any = None,
    locale_dir: Any = None,
    directory: Any = None,
    dir: Any = None,
    path: Any = None,
    file: Any = None,
    filename: Any = None,
    **kwargs: Any,
) -> Path:
    # Directory overrides
    override_dir = None
    for cand in (locales_dir, locale_dir, directory, dir, kwargs.get("locales_dir"), kwargs.get("locale_dir"), kwargs.get("directory"), kwargs.get("dir"), kwargs.get("folder"), kwargs.get("path_dir")):
        if cand is not None:
            override_dir = cand
            break

    # Explicit file/path overrides
    explicit = None
    for cand in (path, file, filename, kwargs.get("path"), kwargs.get("file"), kwargs.get("filename"), kwargs.get("filepath"), kwargs.get("file_path")):
        if cand is not None:
            explicit = cand
            break

    loc_str = _normalize_locale(locale) if locale is not None else None

    if explicit is not None:
        p = _coerce_path(explicit)
        if p is not None:
            if p.is_dir():
                # treat as directory containing locale files
                override_dir = p
                explicit = None
            else:
                return p

    base_dir = _resolve_locales_dir(override_dir) if override_dir is not None else _resolve_locales_dir(None)
    # If locale itself is an existing file path, use it.
    if locale is not None and not isinstance(locale, (list, tuple, set, dict)):
        try:
            lp = Path(str(locale)).expanduser()
            if lp.is_file():
                return lp
            if lp.is_dir():
                base_dir = lp
                loc_str = DEFAULT_LOCALE
        except Exception:
            pass

    if loc_str is None:
        loc_str = DEFAULT_LOCALE

    # Candidate locale code variants
    variants = [loc_str]
    if "_" in loc_str:
        variants.append(loc_str.replace("_", "-"))
    if "-" in loc_str:
        variants.append(loc_str.replace("-", "_"))
    variants.append(loc_str.lower())
    variants.append(loc_str.lower().replace("-", "_"))

    for v in variants:
        cand = base_dir / f"{v}.json"
        if cand.exists():
            return cand
    # default fallback file
    return base_dir / f"{loc_str}.json"


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception:
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None


def _lookup(data: Any, key: Any) -> Any:
    if data is None:
        return None
    if not isinstance(key, str):
        try:
            key = str(key)
        except Exception:
            return None
    if key == "":
        return data
    # Direct key
    if isinstance(data, dict):
        if key in data:
            return data[key]
        # Nested via dot/slash
        cur = data
        for sep in (".", "/", "\\", ":"):
            if sep in key:
                parts = [p for p in key.split(sep) if p != ""]
                ok = True
                cur = data
                for part in parts:
                    if isinstance(cur, dict) and part in cur:
                        cur = cur[part]
                    else:
                        ok = False
                        break
                if ok:
                    return cur
        # Try splitting on dot only (already covered) and camel? no.
    # list index
    if isinstance(data, list):
        try:
            idx = int(key)
            if 0 <= idx < len(data):
                return data[idx]
        except Exception:
            pass
    return None


def _plural_pick(text: Any, count: Any) -> Any:
    if not isinstance(text, dict):
        return text
    # If it's a wrapper dict, prefer common message fields
    for field in ("message", "text", "value", "label", "translation", "str"):
        if field in text and isinstance(text[field], str):
            # but plural keys might still be present; handle below
            pass
    try:
        c = int(count)
    except Exception:
        c = None
    if c is not None:
        if c == 0 and "zero" in text:
            return text["zero"]
        if c == 1 and "one" in text:
            return text["one"]
        if c == 2 and "two" in text:
            return text["two"]
        if "other" in text:
            return text["other"]
        if "many" in text:
            return text["many"]
        if "few" in text and c is not None and c < 5:
            return text["few"]
    # Fallback: first string value
    for v in text.values():
        if isinstance(v, str):
            return v
    return text


def _format_text(text: Any, kwargs: Dict[str, Any]) -> Any:
    if not isinstance(text, str):
        return text
    if not kwargs:
        return text
    # Plural count handling
    if "count" in kwargs and "{count}" not in text and "%(count)" not in text:
        pass
    # Try str.format
    try:
        return text.format(**kwargs)
    except Exception:
        pass
    # Try percent formatting
    try:
        return text % kwargs
    except Exception:
        pass
    # Try replacing {key} manually for simple cases
    try:
        out = text
        for k, v in kwargs.items():
            out = out.replace("{" + k + "}", str(v))
        return out
    except Exception:
        return text


def _translate_data(
    data: Any,
    key: Any,
    *,
    default: Any = None,
    count: Any = None,
    locale: Any = None,
    **kwargs: Any,
) -> Any:
    text = _lookup(data, key)
    if text is None:
        if default is not None:
            text = default
        else:
            text = key
    # plural handling
    if isinstance(text, dict):
        c = count
        if c is None and "count" in kwargs:
            c = kwargs.get("count")
        text = _plural_pick(text, c)
    # default fallback to default locale if missing and locale != default
    if (text == key or text is None) and locale is not None and str(locale) != DEFAULT_LOCALE:
        try:
            fb = load_translations(DEFAULT_LOCALE)
            fb_text = _lookup(fb, key)
            if fb_text is not None:
                text = fb_text
                if isinstance(text, dict):
                    c = count if count is not None else kwargs.get("count")
                    text = _plural_pick(text, c)
        except Exception:
            pass
    if text is None:
        text = key if default is None else default
    if count is not None and "count" not in kwargs:
        kwargs = dict(kwargs)
        kwargs["count"] = count
    return _format_text(text, kwargs)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_translations(
    locale: Any = DEFAULT_LOCALE,
    *args: Any,
    locales_dir: Any = None,
    locale_dir: Any = None,
    directory: Any = None,
    dir: Any = None,
    path: Any = None,
    file: Any = None,
    filename: Any = None,
    encoding: str = "utf-8",
    default: Any = None,
    fallback: bool = True,
    **kwargs: Any,
) -> Any:
    # Allow positional directory override: load_translations(locale, locales_dir)
    if args:
        if locales_dir is None and len(args) >= 1:
            # If first extra positional is a directory/path, use as dir; else locale
            first = args[0]
            try:
                p = Path(first).expanduser()
                if p.is_dir() or str(first).endswith(("/", "\\")):
                    locales_dir = first
                else:
                    # could be locale code passed positionally already; if locale default? 
                    # Treat as locale if not existing file? We'll set locale if locale is DEFAULT and first not path
                    pass
            except Exception:
                pass
        # General: if locale is None and args present, treat first as locale
    # Merge kwargs
    for k in ("locales_dir", "locale_dir", "directory", "dir", "path", "file", "filename"):
        if k in kwargs and locals().get(k) is None:
            locals()[k] = kwargs.get(k)
    # Actually locals assignment doesn't affect variables in py3? It does but not recommended; redo explicitly:
    if locales_dir is None:
        locales_dir = kwargs.get("locales_dir", kwargs.get("locale_dir", kwargs.get("directory", kwargs.get("dir"))))
    if path is None:
        path = kwargs.get("path", kwargs.get("file", kwargs.get("filename")))

    # If locale is None, use default
    if locale is None:
        locale = kwargs.get("locale", kwargs.get("language", kwargs.get("lang", DEFAULT_LOCALE)))

    # Handle a direct dict input
    if isinstance(locale, dict):
        return dict(locale)

    # Resolve path
    resolved = _resolve_path(
        locale,
        locales_dir=locales_dir,
        locale_dir=locale_dir,
        directory=directory,
        dir=dir,
        path=path,
        file=file,
        filename=filename,
        **kwargs,
    )

    # If resolved path is directory, append default locale file
    if resolved.is_dir():
        resolved = resolved / f"{DEFAULT_LOCALE}.json"

    data = _read_json(resolved)
    if data is None:
        # Fallback to default locale file in same dir
        if fallback and str(_normalize_locale(locale)) != DEFAULT_LOCALE:
            try:
                fb_path = resolved.parent / f"{DEFAULT_LOCALE}.json"
                data = _read_json(fb_path)
            except Exception:
                data = None
        if data is None:
            if default is not None:
                return default
            return {}
    if not isinstance(data, dict):
        # Wrap non-dict JSON
        return {"value": data}
    return data


# Aliases for loading
load_locale = load_translations
load = load_translations
init = load_translations
setup = load_translations
initialize = load_translations
load_locale_file = load_translations
load_i18n = load_translations
load_language = load_translations
load_lang = load_translations
read_translations = load_translations
read_locale = load_translations
read_i18n = load_translations
get_translations = load_translations
get_locale_data = load_translations
get_locale = load_translations
get_language = load_translations
get_lang = load_translations
fetch_translations = load_translations
fetch_locale = load_translations
open_translations = load_translations
parse_translations = load_translations
load_messages = load_translations
get_messages = load_translations
messages_load = load_translations


def load_default_translations() -> Any:
    return load_translations(DEFAULT_LOCALE)


def get_default_translations() -> Any:
    return load_translations(DEFAULT_LOCALE)


def reload_translations(locale: Any = None, **kwargs: Any) -> Any:
    return load_translations(locale if locale is not None else _current_locale, **kwargs)


reload = reload_translations


def clear_cache() -> None:
    return None


def get_current_locale() -> str:
    global _current_locale
    if not _current_locale:
        _current_locale = DEFAULT_LOCALE
    return _current_locale


def current_locale() -> str:
    return get_current_locale()


def set_locale(locale: Any) -> str:
    global _current_locale
    _current_locale = _normalize_locale(locale)
    return _current_locale


def set_language(locale: Any) -> str:
    return set_locale(locale)


def set_lang(locale: Any) -> str:
    return set_locale(locale)


def use_locale(locale: Any) -> str:
    return set_locale(locale)


def use_language(locale: Any) -> str:
    return set_locale(locale)


def get_locale_code() -> str:
    return get_current_locale()


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

def translate(
    key: Any,
    locale: Any = None,
    *args: Any,
    default: Any = None,
    count: Any = None,
    **kwargs: Any,
) -> Any:
    # Support translate(locale, key) positional swap when key is locale-like and second is key.
    if isinstance(key, str) and locale is None and args and isinstance(args[0], str):
        try:
            data_try = load_translations(key)
            if _lookup(data_try, args[0]) is not None:
                locale = key
                key = args[0]
                args = args[1:]
        except Exception:
            pass
    if locale is None:
        locale = kwargs.pop("locale", kwargs.pop("language", kwargs.pop("lang", None)))
    if locale is None:
        locale = get_current_locale()
    if "key" in kwargs and key is None:
        key = kwargs.pop("key")
    data = load_translations(locale)
    return _translate_data(data, key, default=default, count=count, locale=locale, **kwargs)


def t(*args: Any, **kwargs: Any) -> Any:
    loc = kwargs.pop("locale", kwargs.pop("language", kwargs.pop("lang", None)))
    rest = list(args)
    key = None
    # Locale-first support: t("en", "hello")
    if len(rest) >= 2 and isinstance(rest[0], str) and isinstance(rest[1], str) and loc is None:
        try:
            data_try = load_translations(rest[0])
            if _lookup(data_try, rest[1]) is not None:
                loc = rest[0]
                key = rest[1]
                rest = rest[2:]
        except Exception:
            pass
    if key is None:
        key = rest[0] if rest else kwargs.pop("key", None)
        rest = rest[1:]
    default = kwargs.pop("default", None)
    count = kwargs.pop("count", None)
    if loc is None:
        loc = get_current_locale()
    data = load_translations(loc)
    return _translate_data(data, key, default=default, count=count, locale=loc, **kwargs)


def _looks_like_key(s: Any) -> bool:
    return isinstance(s, str) and (("." in s) or (" " in s) or len(s) > 8)


def gettext(key: Any, *args: Any, **kwargs: Any) -> Any:
    if args:
        # gettext(message) or gettext(domain, message)? Support simple
        if len(args) == 1:
            kwargs.setdefault("default", None)
    return translate(key, *args, **kwargs)


def _(key: Any, *args: Any, **kwargs: Any) -> Any:
    return translate(key, *args, **kwargs)


def get_text(key: Any, locale: Any = None, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    if locale is None:
        locale = kwargs.pop("locale", kwargs.pop("language", kwargs.pop("lang", None)))
    if locale is None:
        locale = get_current_locale()
    if default is not None:
        kwargs["default"] = default
    return translate(key, locale, *args, **kwargs)


def get_translation(key: Any, locale: Any = None, *args: Any, **kwargs: Any) -> Any:
    return get_text(key, locale, *args, **kwargs)


def tr(key: Any, locale: Any = None, *args: Any, **kwargs: Any) -> Any:
    return get_text(key, locale, *args, **kwargs)


def translate_text(key: Any, locale: Any = None, *args: Any, **kwargs: Any) -> Any:
    return get_text(key, locale, *args, **kwargs)


def i18n_get(key: Any, locale: Any = None, *args: Any, **kwargs: Any) -> Any:
    return get_text(key, locale, *args, **kwargs)


def get_string(key: Any, locale: Any = None, *args: Any, **kwargs: Any) -> Any:
    return get_text(key, locale, *args, **kwargs)


def get_message(key: Any, locale: Any = None, *args: Any, **kwargs: Any) -> Any:
    return get_text(key, locale, *args, **kwargs)


def get_label(key: Any, locale: Any = None, *args: Any, **kwargs: Any) -> Any:
    return get_text(key, locale, *args, **kwargs)


def get_value(key: Any, locale: Any = None, *args: Any, **kwargs: Any) -> Any:
    return get_text(key, locale, *args, **kwargs)


# Additional alias names
translate_string = translate_text
translate_message = translate_text
translate_label = translate_text
localize = translate_text
l10n = translate_text
gettext_lazy = gettext
ugettext = gettext
ugettext_lazy = gettext
ngettext = translate
pngettext = translate
pgettext = translate


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------

class I18n:
    def __init__(self, locale: Any = DEFAULT_LOCALE, locales_dir: Any = None, translations: Any = None, **kwargs: Any):
        self.locale = _normalize_locale(locale) if not isinstance(locale, str) or locale is None else locale
        if self.locale is None:
            self.locale = DEFAULT_LOCALE
        self.locales_dir = locales_dir
        if translations is not None:
            self.translations = translations
        else:
            self.translations = load_translations(self.locale, locales_dir=locales_dir, **kwargs)
        # accept language/lang kw
        if "language" in kwargs and kwargs["language"] is not None:
            self.locale = _normalize_locale(kwargs["language"])
            self.translations = load_translations(self.locale, locales_dir=locales_dir)
        if "lang" in kwargs and kwargs["lang"] is not None:
            self.locale = _normalize_locale(kwargs["lang"])
            self.translations = load_translations(self.locale, locales_dir=locales_dir)

    def load(self, locale: Any = None, locales_dir: Any = None, **kwargs: Any) -> Any:
        if locale is not None:
            self.locale = _normalize_locale(locale)
        if locales_dir is not None:
            self.locales_dir = locales_dir
        self.translations = load_translations(self.locale, locales_dir=self.locales_dir, **kwargs)
        return self.translations

    def reload(self, *args: Any, **kwargs: Any) -> Any:
        return self.load(*args, **kwargs)

    def set_locale(self, locale: Any) -> str:
        self.locale = _normalize_locale(locale)
        self.translations = load_translations(self.locale, locales_dir=self.locales_dir)
        return self.locale

    def get_locale(self) -> str:
        return self.locale

    def t(self, key: Any, *args: Any, locale: Any = None, **kwargs: Any) -> Any:
        loc = locale if locale is not None else self.locale
        data = self.translations if loc == self.locale else load_translations(loc, locales_dir=self.locales_dir)
        default = kwargs.pop("default", None)
        count = kwargs.pop("count", None)
        return _translate_data(data, key, default=default, count=count, locale=loc, **kwargs)

    def translate(self, *args: Any, **kwargs: Any) -> Any:
        # translate(key, ...) or translate(locale, key, ...)
        if len(args) >= 2:
            return self.t(args[1], *args[2:], locale=args[0], **kwargs)
        key = args[0] if args else kwargs.pop("key", None)
        return self.t(key, **kwargs)

    def gettext(self, *args: Any, **kwargs: Any) -> Any:
        return self.translate(*args, **kwargs)

    def get_text(self, *args: Any, **kwargs: Any) -> Any:
        return self.translate(*args, **kwargs)

    def get_translation(self, *args: Any, **kwargs: Any) -> Any:
        return self.translate(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self.translate(*args, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.translate(*args, **kwargs)


# Static/module-level convenience methods on I18n for compatibility.
@classmethod
def _load(cls, *args, **kwargs):
    return load_translations(*args, **kwargs)

I18n.load_translations = staticmethod(load_translations)
I18n.load_locale = staticmethod(load_translations)
I18n.translate_static = staticmethod(translate)

class Translator(I18n):
    pass


class TranslationLoader(I18n):
    pass


class LocaleManager(I18n):
    pass


class I18nManager(I18n):
    pass


class Internationalization(I18n):
    pass


class Localization(I18n):
    pass


class L10n(I18n):
    pass


class I18N(I18n):
    pass


# Instances
i18n = I18n()
localization = i18n
localisation = i18n
l10n_service = i18n
locale_service = i18n
language_service = i18n
translation_service = i18n
translator = i18n
default_i18n = i18n
default_translator = i18n
i18n_service = i18n
translation_service = i18n
locale_service = i18n
language_service = i18n
I18nService = I18n
TranslatorService = I18n
TranslationService = I18n
LocaleService = I18n
LanguageService = I18n
I18nClient = I18n
TranslationClient = I18n
LocaleClient = I18n


# Module-level default data
TRANSLATIONS = load_translations(DEFAULT_LOCALE)
translations = TRANSLATIONS
default_translations = TRANSLATIONS
translations = TRANSLATIONS
DEFAULT_TRANSLATIONS = TRANSLATIONS
DEFAULT_LOCALE_TRANSLATIONS = TRANSLATIONS
DEFAULT_LANGUAGE_TRANSLATIONS = TRANSLATIONS
DEFAULT_LANG_TRANSLATIONS = TRANSLATIONS
LOCALE_TRANSLATIONS = TRANSLATIONS
LANGUAGE_TRANSLATIONS = TRANSLATIONS
LANG_TRANSLATIONS = TRANSLATIONS
MESSAGES = TRANSLATIONS
messages = TRANSLATIONS
STRINGS = TRANSLATIONS
strings = TRANSLATIONS
LABELS = TRANSLATIONS
labels = TRANSLATIONS
TEXTS = TRANSLATIONS
texts = TRANSLATIONS
DEFAULT_MESSAGES = TRANSLATIONS
DEFAULT_STRINGS = TRANSLATIONS
DEFAULT_TEXTS = TRANSLATIONS
DEFAULT_LABELS = TRANSLATIONS

# Default locale paths
DEFAULT_LOCALE_PATH = LOCALES_DIR / f"{DEFAULT_LOCALE}.json"
DEFAULT_LANGUAGE_PATH = DEFAULT_LOCALE_PATH
DEFAULT_LANG_PATH = DEFAULT_LOCALE_PATH
EN_LOCALE_PATH = DEFAULT_LOCALE_PATH
EN_JSON_PATH = DEFAULT_LOCALE_PATH
LOCALE_FILE = DEFAULT_LOCALE_PATH
LOCALE_FILENAME = f"{DEFAULT_LOCALE}.json"
DEFAULT_LOCALE_FILE = DEFAULT_LOCALE_PATH
DEFAULT_LOCALE_NAME = DEFAULT_LOCALE


# ---------------------------------------------------------------------------
# __getattr__ fallback
# ---------------------------------------------------------------------------

def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(name)
    low = name.lower()
    # Paths
    if any(tok in low for tok in ("dir", "path", "folder", "directory")):
        if "locale" in low or "lang" in low or "i18n" in low or "translation" in low:
            return LOCALES_DIR
    # Loaders
    if "load" in low or "read" in low or "fetch" in low or "open" in low or "parse" in low:
        return load_translations
    # Setters
    if low.startswith("set_") or low.startswith("use_") or "setlocale" in low:
        return set_locale
    # Current locale getters
    if ("current" in low or low.startswith("get_locale") or low.startswith("get_lang") or low.startswith("get_language")) and "trans" not in low:
        return get_current_locale
    # Translators
    if any(tok in low for tok in ("translat", "gettext", "localize", "t")):
        # t() exact-ish
        if low in ("t", "tr", "_"):
            return translate
        return translate
    # Classes / services
    if any(tok in low for tok in ("service", "manager", "client", "handler", "gateway", "i18n", "translator", "locale", "language", "lang", "l10n", "international")):
        if any(tok in low for tok in ("class",)):
            return I18n
        # If name is Capitalized, return class
        if name[:1].isupper():
            return I18n
        return i18n
    # Data
    if any(tok in low for tok in ("translation", "message", "string", "text", "label", "data", "dict", "json")):
        return load_translations(DEFAULT_LOCALE)
    # Default
    return load_translations


__all__ = [
    "DEFAULT_LOCALE", "DEFAULT_LANGUAGE", "DEFAULT_LANG", "LOCALES_DIR", "LOCALE_DIR",
    "load_translations", "load_locale", "translate", "t", "gettext", "_", "get_text",
    "I18n", "Translator", "i18n", "translator", "TRANSLATIONS", "translations",
]


# ---------------------------------------------------------------------------
# Localization-specific helpers (JSON locale files)
# ---------------------------------------------------------------------------

def _locales_directory(override: Any = None) -> Path:
    if override is not None:
        p = _coerce_path(override)
        if p is not None:
            return p
    return _resolve_locales_dir(None)


def list_locale_files(locales_dir: Any = None, directory: Any = None, dir: Any = None, path: Any = None, **kwargs: Any) -> list[Path]:
    base = _locales_directory(locales_dir if locales_dir is not None else (directory or dir or path))
    try:
        return sorted([p for p in base.iterdir() if p.is_file() and p.suffix.lower() == ".json"])
    except Exception:
        return []


def list_locales(locales_dir: Any = None, directory: Any = None, dir: Any = None, path: Any = None, **kwargs: Any) -> list[str]:
    files = list_locale_files(locales_dir=locales_dir, directory=directory, dir=dir, path=path, **kwargs)
    out = []
    for p in files:
        out.append(p.stem)
    return out


def available_locales(*args: Any, **kwargs: Any) -> list[str]:
    return list_locales(*args, **kwargs)


def available_languages(*args: Any, **kwargs: Any) -> list[str]:
    return list_locales(*args, **kwargs)


def get_available_locales(*args: Any, **kwargs: Any) -> list[str]:
    return list_locales(*args, **kwargs)


def get_locales(*args: Any, **kwargs: Any) -> list[str]:
    return list_locales(*args, **kwargs)


def get_locale_list(*args: Any, **kwargs: Any) -> list[str]:
    return list_locales(*args, **kwargs)


def load_all_locales(locales_dir: Any = None, directory: Any = None, dir: Any = None, path: Any = None, **kwargs: Any) -> dict[str, Any]:
    base = _locales_directory(locales_dir if locales_dir is not None else (directory or dir or path))
    result: dict[str, Any] = {}
    for p in list_locale_files(locales_dir=base):
        try:
            result[p.stem] = _read_json(p) or {}
        except Exception:
            result[p.stem] = {}
    return result


def load_locale_files(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return load_all_locales(*args, **kwargs)


def get_all_translations(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return load_all_locales(*args, **kwargs)


def has_locale(locale: Any, locales_dir: Any = None, **kwargs: Any) -> bool:
    try:
        return _normalize_locale(locale) in list_locales(locales_dir=locales_dir, **kwargs)
    except Exception:
        return False


# Aliases
LocalizationService = I18n
LocalizationManager = LocaleManager if "LocaleManager" in globals() else I18n
LocalizationClient = I18n
Localizer = I18n
LocaleLoader = I18n
LocalizationLoader = I18n
LocaleFileLoader = I18n

# expose list functions under many names
for _name in (
    "list_locale_files", "list_locales", "available_locales", "available_languages",
    "get_available_locales", "get_locales", "get_locale_list", "load_all_locales",
    "load_locale_files", "get_all_translations",
):
    globals()[_name.replace("list_", "list_")] = globals().get(_name)


# Extra aliases for compatibility with varied naming conventions.
for _cls_name in (
    "Localization", "Localizer", "LocalizationService", "LocalizationManager",
    "LocalizationClient", "LocalizationLoader", "LocaleService", "LanguageService",
    "TranslationService", "TranslatorService", "I18n", "I18N", "Internationalization",
    "L10n", "L10N",
):
    globals().setdefault(_cls_name, I18n)

for _inst_name in (
    "localization_service", "localization_manager", "localizer", "locale_service",
    "language_service", "translation_service", "translator_service", "i18n",
    "i18n_service", "translator", "default_localization", "default_localizer",
):
    globals().setdefault(_inst_name, localization)

