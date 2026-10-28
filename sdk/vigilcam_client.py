"""VigilCam Python client SDK (sync, httpx-based).

Modeled on the ispy ``agent-py`` wrapper structure (``agent/a.py`` +
``agent/device.py`` + ``agent/exceptions.py``):

* ``VigilCamClient`` mirrors ``Agent`` -- server-URL building, a shared
  HTTP session, a ``_request`` helper with reconnect-interval short
  circuiting (``_conFailed`` / ``disconnected`` / ``reconnect_interval``),
  ``close()``, ``is_available``, and resource accessors.
* ``VigilCamCamera`` mirrors ``Device`` -- thin wrapper around a raw
  camera dict with convenience actions that delegate to the client.
* ``VigilCamError`` / ``VigilCamConnectionError`` mirror ``AgentError`` /
  ``AgentConnectionError``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

_LOGGER = logging.getLogger(__name__)


class VigilCamError(Exception):
    """Generic VigilCam SDK exception (mirrors AgentError)."""


class VigilCamConnectionError(VigilCamError):
    """VigilCam connection exception (mirrors AgentConnectionError)."""


class VigilCamClient:
    """The VigilCam API client. Create one of these to begin."""

    DEFAULT_TIMEOUT = 10.0

    def __init__(
        self,
        server_host: str,
        api_key: Optional[str] = None,
        session: Optional[httpx.Client] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Create a VigilCam API client.

        ``server_host`` may be a bare host (``http://host:port/``) with or
        without a trailing slash. An optional ``api_key`` is sent as an
        ``X-API-Key`` header when provided. Pass an existing
        :class:`httpx.Client` (e.g. one backed by ``httpx.MockTransport``
        in tests) via ``session``; otherwise a client is created and owned
        by this instance.
        """
        self._server_url = VigilCamClient._build_server_url(server_host)
        self._close_session = session is None
        self._session = session or httpx.Client(timeout=timeout)
        self._api_key = api_key
        self._timeout = timeout
        self._conFailed = False
        self.disconnected = datetime.now()
        self.reconnect_interval = 10
        self.cameras_cache: list[VigilCamCamera] = []
        self._raw_result: dict = {}

    # -- lifecycle ------------------------------------------------------
    def close(self) -> None:
        if self._session is not None and self._close_session:
            self._session.close()

    def __enter__(self) -> "VigilCamClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- low-level request helper (mirrors Agent._agent_request) --------
    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": "PythonVigilCam/",
            "Accept": "application/json, text/plain, */*",
        }
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    def _request(
        self,
        method: str,
        api_url: str,
        *,
        params: Optional[dict] = None,
        json: Any = None,
        timeout: Optional[float] = None,
    ) -> Any:
        if self._conFailed:
            if (datetime.now() - self.disconnected).total_seconds() < self.reconnect_interval:
                return None
        try:
            response = self._session.request(
                method.upper(),
                urljoin(self._server_url, api_url.lstrip("/")),
                params=params,
                json=json,
                headers=self._headers(),
                timeout=self._timeout if timeout is None else timeout,
            )
            response.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
            self.disconnected = datetime.now()
            self._conFailed = True
            raise VigilCamConnectionError(
                "Error occurred while communicating with VigilCam server"
            ) from exc
        except httpx.HTTPStatusError as exc:
            self.disconnected = datetime.now()
            self._conFailed = True
            try:
                detail: Any = exc.response.json()
            except Exception:  # noqa: BLE001
                detail = exc.response.text
            raise VigilCamError(
                f"VigilCam server returned status {exc.response.status_code}: {detail}"
            ) from exc

        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            # Snapshot bytes / placeholder PNG etc. -- return raw bytes.
            self._conFailed = False
            return response.content
        try:
            data = response.json()
        except ValueError as exc:
            self.disconnected = datetime.now()
            self._conFailed = True
            raise VigilCamError("Unexpected response from the VigilCam server") from exc
        if isinstance(data, dict) and data.get("error") is not None:
            raise VigilCamError(
                "Error from the VigilCam server",
                # NOTE: VigilCamError takes a single message arg; keep detail
                # in the message string for debuggability.
                # (agent-py passes a dict second arg; stdlib Exception
                # ignores extra args for str(), so format it instead.)
            )
        self._conFailed = False
        return data

    def get_state(self, api_url: str, **kwargs: Any) -> Any:
        """Perform a GET request on the given VigilCam API URL."""
        return self._request("GET", api_url, **kwargs)

    # -- cameras (mirrors Agent.get_devices) -----------------------------
    def get_cameras(self) -> list["VigilCamCamera"]:
        """Fetch the camera list and wrap each row in a VigilCamCamera."""
        raw = self._request("GET", "api/cameras")
        if raw is None:
            return []
        rows = raw if isinstance(raw, list) else raw.get("cameras", raw)
        self.cameras_cache = [VigilCamCamera(self, item) for item in rows]
        return self.cameras_cache

    # Alias matching agent-py's get_devices naming for familiarity.
    def list_cameras(self) -> list["VigilCamCamera"]:
        return self.get_cameras()

    def get_camera(self, camera_id: int) -> "VigilCamCamera":
        raw = self._request("GET", f"api/cameras/{int(camera_id)}")
        return VigilCamCamera(self, raw)

    def add_camera(
        self,
        name: str,
        source: str,
        kind: str = "ip",
        **kwargs: Any,
    ) -> "VigilCamCamera":
        """Create a camera (mirrors Device provisioning on Agent)."""
        payload = {"name": name, "source": source, "kind": kind, **kwargs}
        raw = self._request("POST", "api/cameras", json=payload)
        return VigilCamCamera(self, raw)

    # Back-compat alias: some callers say create_camera.
    def create_camera(self, *args: Any, **kwargs: Any) -> "VigilCamCamera":
        return self.add_camera(*args, **kwargs)

    def update_camera(self, camera_id: int, patch: dict) -> "VigilCamCamera":
        raw = self._request("PATCH", f"api/cameras/{int(camera_id)}", json=patch)
        return VigilCamCamera(self, raw)

    def delete_camera(self, camera_id: int) -> bool:
        self._request("DELETE", f"api/cameras/{int(camera_id)}")
        return True

    # -- events -----------------------------------------------------------
    def list_events(
        self,
        camera_id: Optional[int] = None,
        limit: int = 100,
        label: Optional[str] = None,
        **filters: Any,
    ) -> list[dict]:
        """List events, optionally filtered by camera/label."""
        params: dict[str, Any] = {"limit": limit}
        if camera_id is not None:
            params["camera_id"] = int(camera_id)
        if label is not None:
            params["label"] = label
        params.update(filters)
        raw = self._request("GET", "api/events", params=params)
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        return raw.get("events", raw)

    # Alias mirroring agent-py's Device.get_events phrasing.
    def get_events(self, *args: Any, **kwargs: Any) -> list[dict]:
        return self.list_events(*args, **kwargs)

    # -- recordings --------------------------------------------------------
    def recordings_status(self) -> dict:
        """Return the recorder status dict (active recordings, etc.)."""
        raw = self._request("GET", "api/recordings/status")
        return raw if isinstance(raw, dict) else {"status": raw}

    def list_recordings(self, camera_id: Optional[int] = None) -> list[dict]:
        params = {"camera_id": int(camera_id)} if camera_id is not None else None
        raw = self._request("GET", "api/recordings", params=params)
        if raw is None:
            return []
        return raw if isinstance(raw, list) else raw.get("recordings", raw)

    def start_recording(self, camera_id: int, main_url: str, sub_url: Optional[str] = None) -> dict:
        payload: dict[str, Any] = {"camera_id": int(camera_id), "main_url": main_url}
        if sub_url is not None:
            payload["sub_url"] = sub_url
        return self._request("POST", "api/recordings/start", json=payload)

    def stop_recording(self, camera_name: str) -> dict:
        return self._request("POST", "api/recordings/stop", json={"camera_name": camera_name})

    # -- snapshots ----------------------------------------------------------
    def snapshot(self, camera_id: Optional[int] = None, source: Optional[str] = None, **kwargs: Any) -> dict:
        """Capture a snapshot now for a camera (or raw source URL)."""
        payload: dict[str, Any] = dict(kwargs)
        if camera_id is not None:
            payload["camera_id"] = int(camera_id)
        if source is not None:
            payload["source"] = source
        return self._request("POST", "api/snapshot_now/capture", json=payload)

    def snapshot_status(self) -> dict:
        raw = self._request("GET", "api/snapshot_now/status")
        return raw if isinstance(raw, dict) else {"status": raw}

    # -- rules ---------------------------------------------------------------
    def list_rules(self) -> list[dict]:
        """List active standing rules."""
        raw = self._request("GET", "api/rules/standing")
        if raw is None:
            return []
        return raw if isinstance(raw, list) else raw.get("rules", raw)

    def create_rule(self, sentence: str, camera_id: Optional[int] = None) -> dict:
        payload: dict[str, Any] = {"sentence": sentence}
        if camera_id is not None:
            payload["camera_id"] = int(camera_id)
        return self._request("POST", "api/rules/standing", json=payload)

    # Alias with agent-py style verb.
    def add_rule(self, *args: Any, **kwargs: Any) -> dict:
        return self.create_rule(*args, **kwargs)

    def expire_rules(self) -> dict:
        raw = self._request("POST", "api/rules/standing/expire", json={})
        return raw if isinstance(raw, dict) else {"expired": raw}

    # -- server info (mirrors Agent.update / name / version) ----------------
    def update(self) -> dict:
        state = self.get_state("api/system/status")
        if state is not None:
            self._raw_result = state if isinstance(state, dict) else {"status": state}
        return self._raw_result

    @property
    def is_available(self) -> bool:
        """Indicate whether this VigilCam service is currently reachable."""
        return not self._conFailed

    @property
    def raw_result(self) -> dict:
        return self._raw_result

    @staticmethod
    def _build_server_url(server_host: str) -> str:
        """Build the server URL, ensuring a trailing slash."""
        if not server_host.endswith("/"):
            return f"{server_host}/"
        return server_host


class VigilCamCamera:
    """Represents a single VigilCam camera (mirrors agent.py ``Device``)."""

    def __init__(self, client: VigilCamClient, raw_result: dict) -> None:
        self._client = client
        self._raw_result = dict(raw_result or {})
        self._oid = int(self._raw_result.get("id", 0))

    @property
    def id(self) -> int:
        return self._oid

    @property
    def client(self) -> VigilCamClient:
        return self._client

    @property
    def name(self) -> str:
        return str(self._raw_result.get("name", ""))

    @property
    def source(self) -> str:
        return str(self._raw_result.get("source", ""))

    @property
    def kind(self) -> str:
        return str(self._raw_result.get("kind", ""))

    @property
    def enabled(self) -> bool:
        return bool(self._raw_result.get("enabled", True))

    @property
    def recording_enabled(self) -> bool:
        return bool(self._raw_result.get("recording_enabled", False))

    @property
    def raw_result(self) -> dict:
        return self._raw_result

    @property
    def snapshot_url(self) -> str:
        return f"api/snapshot_now/capture?camera_id={self._oid}"

    def update(self) -> "VigilCamCamera":
        """Refresh this camera from the server."""
        fresh = self._client.get_camera(self._oid)
        self._raw_result = fresh.raw_result
        return self

    def delete(self) -> bool:
        return self._client.delete_camera(self._oid)

    def snapshot(self, **kwargs: Any) -> dict:
        """Capture a snapshot for this camera."""
        return self._client.snapshot(camera_id=self._oid, **kwargs)

    def get_events(self, limit: int = 100, **filters: Any) -> list[dict]:
        """List events for this camera (mirrors Device.get_events)."""
        return self._client.list_events(camera_id=self._oid, limit=limit, **filters)

    def start_recording(self, main_url: str, sub_url: Optional[str] = None) -> dict:
        return self._client.start_recording(self._oid, main_url, sub_url=sub_url)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"VigilCamCamera(id={self._oid!r}, name={self.name!r})"
