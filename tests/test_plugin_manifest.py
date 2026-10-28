import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.plugin_manifest import router as plugin_manifest_router  # noqa: F401
from app.services import plugin_manifest as pm  # noqa: F401
from app.services.plugin_manifest import (  # noqa: F401
    ManifestError,
    builtin_examples,
    discover_manifests,
    example_barcode_manifest,
    example_demo_manifest,
    example_gain_manifest,
    get_manifest,
    load_manifest,
    load_manifest_from_dir,
    manifest_config_bindings,
    normalize_supports,
    parse_manifest,
    supports_interfaces,
    supports_list,
    validate_errors,
    validate_manifest,
)


def _good_manifest():
    return {
        "name": "Barcode",
        "version": "1.2.2.0",
        "authors": "DeveloperInABox",
        "description": "Barcode scanner",
        "target_framework": "net9.0",
        "supports": "video",
        "custom_events": ["Barcode Recognized"],
        "config_schema": {
            "header": "Barcodes",
            "sections": [
                {
                    "header": "General",
                    "displayType": "Form",
                    "items": [
                        {"text": "Interval", "type": "Int32", "min": 100, "max": 20000, "bindto": "Interval"},
                        {"text": "Intensive", "type": "Boolean", "bindto": "Intensive"},
                    ],
                }
            ],
        },
    }


def test_plugin_manifest_import():
    import app.api.plugin_manifest  # noqa: F401
    import app.services.plugin_manifest  # noqa: F401
    from app.api.plugin_manifest import router  # noqa: F401
    from app.services.plugin_manifest import parse_manifest, validate_manifest  # noqa: F401


def test_plugin_manifest_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/plugin_manifests" in paths
    assert "/api/plugin_manifests/{name}" in paths
    assert "/api/plugin_manifests/validate" in paths
    assert "/api/plugin_manifests/parse" in paths


def test_parse_normalizes_supports_variants():
    assert normalize_supports("video") == "video"
    assert normalize_supports("audio") == "audio"
    assert normalize_supports("audio,video") == "video,audio"
    assert normalize_supports(["video", "audio"]) == "video,audio"
    assert normalize_supports({"video": True}) == "video"
    assert normalize_supports("ICamera") == "video"
    assert normalize_supports("IMicrophone") == "audio"
    assert normalize_supports("bogus") is None
    assert supports_list("video,audio") == ["video", "audio"]
    assert "ICamera" in supports_interfaces("video")
    assert "IMicrophone" in supports_interfaces("audio")


def test_builtin_examples_mirror_agentdvr_plugins():
    barcode = example_barcode_manifest()
    assert barcode["name"] == "Barcode"
    assert barcode["version"] == "1.2.2.0"
    assert barcode["supports"] == "video"
    assert "Barcode Recognized" in barcode["custom_events"]
    assert "Interval" in barcode["config_bindings"]

    demo = example_demo_manifest()
    assert demo["name"] == "Demo"
    assert demo["supports"] == "video,audio"
    assert "Box Bounce" in demo["custom_events"]
    assert {c["name"] for c in demo["commands"]} >= {"reset", "mirror", "volume", "alert", "status"}
    assert "Example_Trip_Wires" in demo["config_bindings"]

    gain = example_gain_manifest()
    assert gain["name"] == "Gain"
    assert gain["supports"] == "audio"
    assert "band1" in gain["config_bindings"]

    assert set(builtin_examples().keys()) == {"barcode", "demo", "gain"}
    for manifest in builtin_examples().values():
        assert validate_errors(manifest) == []


def test_validate_accepts_good_and_rejects_bad():
    assert validate_errors(_good_manifest()) == []
    parsed = validate_manifest(_good_manifest())
    assert parsed["name"] == "Barcode"
    assert parsed["supports_list"] == ["video"]
    assert parsed["assembly_name"] == "Barcode"

    missing = {}
    assert validate_errors(missing)
    try:
        validate_manifest(missing)
        raise AssertionError("expected ManifestError")
    except ManifestError:
        pass

    bad_version = dict(_good_manifest(), version="not-a-version")
    assert validate_errors(bad_version)

    bad_supports = dict(_good_manifest(), supports="smellovision")
    assert validate_errors(bad_supports)

    bad_item = _good_manifest()
    bad_item["config_schema"]["sections"][0]["items"][0]["type"] = "Nope"
    assert validate_errors(bad_item)

    missing_bindto = _good_manifest()
    del missing_bindto["config_schema"]["sections"][0]["items"][1]["bindto"]
    assert validate_errors(missing_bindto)

    bad_button = _good_manifest()
    bad_button["config_schema"]["sections"][0]["items"].append(
        {"text": "Run", "type": "ButtonFA", "action": "plugincommand"}
    )
    assert validate_errors(bad_button)


def test_manifest_config_bindings_splits_comma_bindto():
    manifest = parse_manifest(
        {
            "name": "Demo",
            "version": "1.3.5.0",
            "supports": "video,audio",
            "config_schema": {
                "sections": [
                    {
                        "header": "S",
                        "items": [{"text": "Range", "type": "Slider", "bindto": "Example_Range_Min,Example_Range_Max"}],
                    }
                ]
            },
        }
    )
    assert manifest_config_bindings(manifest) == ["Example_Range_Min", "Example_Range_Max"]


def test_load_and_discover_manifests(tmp_path):
    plugin_dir = tmp_path / "Barcode"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(json.dumps(_good_manifest()), encoding="utf-8")
    loaded = load_manifest(plugin_dir / "plugin.json")
    assert loaded["name"] == "Barcode"

    from_dir = load_manifest_from_dir(plugin_dir)
    assert from_dir is not None and from_dir["name"] == "Barcode"

    items = discover_manifests(tmp_path)
    assert [m["name"] for m in items] == ["Barcode"]
    assert get_manifest("barcode", tmp_path)["version"] == "1.2.2.0"
    assert get_manifest("missing", tmp_path) is None


def test_plugin_manifest_api(tmp_path):
    a = FastAPI()
    a.include_router(plugin_manifest_router)
    client = TestClient(a)

    r = client.post("/api/plugin_manifests/validate", json=_good_manifest())
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is True

    bad = client.post("/api/plugin_manifests/validate", json={"name": "Bad!"})
    assert bad.status_code == 200
    assert bad.json()["valid"] is False
    assert bad.json()["errors"]

    p = client.post("/api/plugin_manifests/parse", json=_good_manifest())
    assert p.status_code == 200
    assert p.json()["name"] == "Barcode"

    ex = client.get("/api/plugin_manifests/examples")
    assert ex.status_code == 200
    assert set(ex.json().keys()) == {"barcode", "demo", "gain"}

    demo = client.get("/api/plugin_manifests/examples/demo")
    assert demo.status_code == 200
    assert demo.json()["supports"] == "video,audio"

    named = client.get("/api/plugin_manifests/demo")
    assert named.status_code == 200
    assert named.json()["name"] == "Demo"

    missing = client.get("/api/plugin_manifests/nope")
    assert missing.status_code == 404

    # discovered manifests from a real directory
    (tmp_path / "Gain").mkdir()
    (tmp_path / "Gain" / "plugin.json").write_text(
        json.dumps(dict(_good_manifest(), name="Gain", supports="audio")), encoding="utf-8"
    )
    listed = client.get("/api/plugin_manifests", params={"plugins_dir": str(tmp_path)})
    assert listed.status_code == 200
    assert [m["name"] for m in listed.json()] == ["Gain"]
