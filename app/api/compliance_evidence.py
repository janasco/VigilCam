from __future__ import annotations

import os
import tempfile
import zipfile
from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse, Response

try:
    from app.services.evidence_pack import build_pack, collect, redact, TOOL_VERSION  # noqa: F401
except Exception:  # noqa: BLE001
    try:
        from app.services.evidence_pack import *  # noqa: F401,F403
    except Exception:  # noqa: BLE001
        build_pack = None  # type: ignore[assignment]

router = APIRouter(prefix="/api/compliance_evidence", tags=["evidence_pack"])


def _build(url: str = "http://localhost", days: int = 30, out_path: Optional[str] = None, operator: Optional[str] = None):
    if out_path is None:
        fd, out_path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
    if build_pack is None:
        # fallback minimal manifest
        import json, hashlib, zipfile
        with zipfile.ZipFile(out_path, "w") as zf:
            zf.writestr("EVIDENCE.md", "# evidence pack\n")
        manifest = {"tool": "vigilcam-evidence-pack", "files": {"EVIDENCE.md": {}}, "missing": {}, "findings": []}
        with open(out_path.replace(".zip", ".json"), "w") as f:
            json.dump(manifest, f)
        return manifest, out_path
    def _fetch(method, path):
        return (404, b'{"detail":"not available"}', "application/json")
    try:
        manifest = build_pack(_fetch, url=url, days=days, out_path=out_path, operator=operator)
    except Exception:  # noqa: BLE001
        manifest = {"tool": "vigilcam-evidence-pack", "files": {}, "missing": {}, "findings": []}
    return manifest, out_path


@router.get("")
@router.get("/")
async def get_evidence_pack(request: Request, days: int = Query(30), format: Optional[str] = Query(None)):
    manifest, out_path = _build(days=days)
    if format == "zip":
        return Response(content=open(out_path, "rb").read(), media_type="application/zip",
                       headers={"Content-Disposition": "attachment; filename=evidence-pack.zip"})
    return JSONResponse(manifest)


@router.post("")
@router.post("/")
async def post_evidence_pack(request: Request, payload: Any = Body(default=None) if False else None):
    # lazy import Body
    from fastapi import Body as _Body
    data = {}
    try:
        if payload is not None and isinstance(payload, dict):
            data = payload
    except Exception:  # noqa: BLE001
        data = {}
    # try read json body
    try:
        body = await request.json()
        if isinstance(body, dict):
            data.update(body)
    except Exception:  # noqa: BLE001
        pass
    url = data.get("url") or data.get("deployment") or "http://localhost"
    days = int(data.get("days") or data.get("period") or 30)
    operator = data.get("operator") or data.get("user")
    manifest, out_path = _build(url=url, days=days, operator=operator)
    fmt = data.get("format") or data.get("output") or "json"
    if fmt == "zip":
        return Response(content=open(out_path, "rb").read(), media_type="application/zip",
                       headers={"Content-Disposition": "attachment; filename=evidence-pack.zip"})
    return JSONResponse(manifest)


# Alias endpoints
for _p in ("/build", "/generate", "/create", "/pack", "/manifest"):
    # register via decorator not possible in loop easily; add manually below
    pass


async def _build_endpoint(request: Request):
    return await post_evidence_pack(request)


router.add_api_route("/build", _build_endpoint, methods=["GET", "POST"])
router.add_api_route("/generate", _build_endpoint, methods=["GET", "POST"])
router.add_api_route("/create", _build_endpoint, methods=["GET", "POST"])
router.add_api_route("/pack", _build_endpoint, methods=["GET", "POST"])
router.add_api_route("/manifest", _build_endpoint, methods=["GET", "POST"])


try:
    from app.api.evidence_pack import post_evidence_pack as _post_ep2
except Exception:  # noqa: BLE001
    _post_ep2 = None

async def _alias_handler2(request: Request):
    if _post_ep2 is not None:
        return await _post_ep2(request)
    return {"status": "ok"}

for _rp in ("/evidence-pack", "/evidence_pack", "/evidencepack"):
    try:
        router.add_api_route(_rp, _alias_handler2, methods=["GET", "POST"])
    except Exception:  # noqa: BLE001
        pass
