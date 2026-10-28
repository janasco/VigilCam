import json
import tempfile
from pathlib import Path

from app.services import i18n


def test_default_locale():
    assert i18n.DEFAULT_LOCALE == "en"
    assert i18n.default_locale if hasattr(i18n, "default_locale") else True


def test_load_translations_default():
    data = i18n.load_translations()
    assert isinstance(data, dict)
    assert data["hello"] == "Hello"
    assert data["app"]["name"] == "VigilCam"


def test_load_translations_locale_arg():
    data = i18n.load_translations("en")
    assert data["hello"] == "Hello"


def test_translate_basic():
    assert i18n.translate("hello") == "Hello"
    assert i18n.t("hello") == "Hello"
    assert i18n.get_text("hello") == "Hello"
    assert i18n.get_translation("hello") == "Hello"
    assert i18n.tr("hello") == "Hello"
    assert i18n._("hello") == "Hello"


def test_translate_formatting():
    assert i18n.translate("greeting", name="World") == "Hello, World!"
    assert i18n.t("greeting", name="World") == "Hello, World!"


def test_translate_plural():
    assert i18n.translate("items", count=1) == "1 item"
    assert i18n.translate("items", count=3) == "3 items"


def test_missing_key_returns_key():
    assert i18n.translate("does.not.exist") == "does.not.exist"
    assert i18n.get_text("missing", default="fallback") == "fallback"


def test_set_locale():
    i18n.set_locale("en")
    assert i18n.get_current_locale() == "en"


def test_class_instance():
    tr = i18n.I18n()
    assert tr.t("hello") == "Hello"
    tr2 = i18n.Translator(locale="en")
    assert tr2.translate("hello") == "Hello"


def test_monkeypatch_dir(tmp_path, monkeypatch):
    (tmp_path / "en.json").write_text(json.dumps({"hello": "Patched"}))
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    assert i18n.load_translations()["hello"] == "Patched"
    # restore
    monkeypatch.setattr(i18n, "LOCALES_DIR", i18n.ORIGINAL_LOCALES_DIR)


def test_monkeypatch_config_dir(tmp_path, monkeypatch):
    (tmp_path / "en.json").write_text(json.dumps({"hello": "ConfigPatched"}))
    monkeypatch.setattr("app.core.config.LOCALES_DIR", tmp_path, raising=False)
    # reset global to original so config is consulted
    monkeypatch.setattr(i18n, "LOCALES_DIR", i18n.ORIGINAL_LOCALES_DIR)
    monkeypatch.setattr(i18n, "LOCALE_DIR", i18n.ORIGINAL_LOCALES_DIR)
    assert i18n.load_translations()["hello"] == "ConfigPatched"


def test_nested_lookup():
    assert i18n.translate("app.name") == "VigilCam"
    assert i18n.translate("camera.status") == "Status"


def test_locale_first_t():
    assert i18n.t("en", "hello") == "Hello"
