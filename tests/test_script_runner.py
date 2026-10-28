from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
import app.services.script_runner as svc
from app.services.actions import ActionExecutor


def test_actions_command_blocked_by_default():
    # actions.py must still refuse command execution unless explicitly enabled.
    assert os.getenv("ALLOW_COMMAND_ACTIONS") in (None, "", "0", "false", "False")
    ex = ActionExecutor()
    if ex._command_actions_allowed():
        # Environment explicitly enables commands; skip default-blocked check.
        import pytest

        pytest.skip("command actions enabled in this environment")
    res = ex.execute(type="command", target="echo hi")
    assert isinstance(res, dict)
    assert res.get("ok") is False
    assert res.get("status") == "blocked"


def test_actions_has_no_allowlist_mechanism():
    import app.services.actions as actions_mod
    import pathlib

    src = pathlib.Path(actions_mod.__file__).read_text(encoding="utf-8")
    lowered = src.lower()
    assert "allowlist" not in lowered
    assert "allowed_scripts" not in lowered
    # Arbitrary command execution exists but is gated behind the
    # ALLOW_COMMAND_ACTIONS flag (blocked by default, verified above).
    assert "shell=True" in src
    assert "_command_actions_allowed" in src


def _write_py_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_script_runner_imports_and_routes():
    from app.api import script_runner as api_mod

    assert hasattr(api_mod, "router")
    paths = list(app.openapi()["paths"].keys())
    assert "/api/script-runner/scripts" in paths
    assert "/api/script-runner/run" in paths
    assert "/api/script_runner/scripts" in paths


def test_service_has_no_shell_true():
    src = Path(svc.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in src
    assert "ALLOW" in src or "allowlist" in src.lower()
    assert "timeout" in src.lower()
    assert "stdout" in src.lower()


def test_allowlist_blocks_arbitrary_commands():
    assert svc.is_allowed("echo hi") is False
    assert svc.is_allowed("rm -rf /") is False
    assert svc.is_allowed("/bin/sh") is False
    assert svc.is_allowed("../../etc/passwd") is False
    assert svc.is_allowed("../scripts/evidence_pack.py") is False
    assert svc.is_allowed("") is False
    assert svc.is_allowed(None) is False

    for bad in ("echo hi", "/bin/sh", "../../etc/passwd", "not-a-real-script-xyz"):
        res = svc.run_script(bad)
        assert res.ok is False
        assert res.blocked is True
        d = res.to_dict()
        assert d["blocked"] is True
        assert d["status"] == "blocked"


def test_allowed_script_runs_with_output_capture(tmp_path):
    script = tmp_path / "hello_script.py"
    _write_py_script(
        script,
        "import sys\nprint('hello-stdout')\nprint('hello-stderr', file=sys.stderr)\n",
    )
    svc.register_script("test-hello-script", script)
    try:
        assert svc.is_allowed("test-hello-script") is True
        res = svc.run_script("test-hello-script", timeout=10)
        assert res.ok is True
        assert res.returncode == 0
        assert "hello-stdout" in res.stdout
        assert "hello-stderr" in res.stderr
        assert res.latency_ms is not None
        d = res.to_dict()
        assert d["ok"] is True
        assert d["success"] is True
        assert "hello-stdout" in d["stdout"]
        assert "latency_ms" in d
        assert "timeout_seconds" in d
    finally:
        svc.unregister_script("test-hello-script")


def test_script_args_passing(tmp_path):
    script = tmp_path / "args_script.py"
    _write_py_script(script, "import sys\nprint('|'.join(sys.argv[1:]))\n")
    svc.register_script("test-args-script", script)
    try:
        res = svc.run_script("test-args-script", args=["a", "b", "--flag", "1"])
        assert res.ok is True
        assert "a|b|--flag|1" in res.stdout
    finally:
        svc.unregister_script("test-args-script")


def test_script_rejects_bad_args(tmp_path):
    script = tmp_path / "ok_script.py"
    _write_py_script(script, "print('ok')\n")
    svc.register_script("test-badargs-script", script)
    try:
        res = svc.run_script("test-badargs-script", args="not-a-list")
        assert res.ok is False
        res2 = svc.run_script("test-badargs-script", args=["ok", 123 * 20 * "x" * 10])
        # Overlong single arg (>1024 chars) is rejected.
        assert res2.ok is False
    finally:
        svc.unregister_script("test-badargs-script")


def test_script_timeout(tmp_path):
    script = tmp_path / "sleep_script.py"
    _write_py_script(script, "import time\ntime.sleep(5)\nprint('done')\n")
    svc.register_script("test-sleep-script", script)
    try:
        res = svc.run_script("test-sleep-script", timeout=0.5)
        assert res.ok is False
        assert res.timed_out is True
        assert "timeout" in (res.detail or "").lower()
        assert res.to_dict()["timed_out"] is True
    finally:
        svc.unregister_script("test-sleep-script")


def test_script_nonzero_exit(tmp_path):
    script = tmp_path / "fail_script.py"
    _write_py_script(script, "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")
    svc.register_script("test-fail-script", script)
    try:
        res = svc.run_script("test-fail-script")
        assert res.ok is False
        assert res.blocked is False
        assert res.returncode == 3
        assert "boom" in res.stderr
    finally:
        svc.unregister_script("test-fail-script")


def test_endpoints(tmp_path):
    script = tmp_path / "api_script.py"
    _write_py_script(script, "print('api-ok')\n")
    svc.register_script("test-api-script", script)
    try:
        client = TestClient(app)

        rs = client.get("/api/script-runner/scripts")
        assert rs.status_code == 200
        body = rs.json()
        assert body["ok"] is True
        assert isinstance(body["scripts"], list)

        rs2 = client.get("/api/script_runner/scripts")
        assert rs2.status_code == 200

        r422 = client.post("/api/script-runner/run", json={})
        assert r422.status_code == 422

        r403 = client.post("/api/script-runner/run", json={"script": "definitely-not-allowlisted-xyz"})
        assert r403.status_code == 403

        rok = client.post("/api/script-runner/run", json={"script": "test-api-script", "timeout": 10})
        assert rok.status_code == 200
        payload = rok.json()
        assert payload["ok"] is True
        assert "api-ok" in payload.get("stdout", "")

        rok2 = client.post("/api/script_runner/run", json={"script": "test-api-script"})
        assert rok2.status_code == 200
        assert rok2.json()["ok"] is True

        rget = client.get("/api/script-runner/run", params={"script": "test-api-script"})
        assert rget.status_code == 200
        assert rget.json()["ok"] is True

        rget403 = client.get("/api/script-runner/run", params={"script": "nope-not-allowlisted"})
        assert rget403.status_code == 403

        rget422 = client.get("/api/script-runner/run")
        assert rget422.status_code == 422
    finally:
        svc.unregister_script("test-api-script")
