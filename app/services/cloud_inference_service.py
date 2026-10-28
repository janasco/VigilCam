from __future__ import annotations

import app.services.cloud_inference as _primary

# Re-export everything from primary module.
from app.services.cloud_inference import *  # noqa: F401,F403

# Ensure httpx attribute is available for patching via this module path.
httpx = _primary.httpx
# Alias module-level objects
cloud_inference = _primary.cloud_inference_service
service = _primary.cloud_inference_service
cloud_inference_service = _primary.cloud_inference_service

# Rebind common names explicitly
run_inference = _primary.run_inference
infer = _primary.infer
clear = _primary.clear
CloudInferenceService = _primary.CloudInferenceService

def __getattr__(name: str):
    return getattr(_primary, name)


__all__ = getattr(_primary, "__all__", [])
