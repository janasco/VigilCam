"""Group recording-mode bulk service.

Verification note (read-only check before writing this module):

- ``app/services/group_bulk.py`` + ``app/api/group_bulk.py`` operate by
  group only via ``bulk_set_group_cameras(db, group_ids, enabled, fields)``.
  ``fields`` is restricted to boolean flags::

      {"enabled", "recording_enabled", "detection_enabled",
       "detector_enabled", "alert_enabled"}

  There is no ``recording_mode`` (string profile: ``motion``/``always``/
  ``never``/``continuous``/...) setting anywhere in ``group_bulk``.
  ``RECORDING_FIELDS = ("recording_enabled",)`` is a boolean toggle, not
  the ``Camera.recording_mode`` string column
  (``app/models/entities.py``).

This module fills that gap: set ``Camera.recording_mode`` for all member
cameras of the given groups. The ``app/api/group_recmode.py`` router stays
thin.
"""

from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

# Canonical stored values mirror ``app/services/config_validate.py``
# ``_VALID_MODES`` so single-camera validation and group bulk stay aligned.
VALID_RECORDING_MODES = (
    "motion",
    "continuous",
    "always",
    "never",
    "none",
    "events",
    "detections",
    "alerts",
    "all",
)

# Tolerant input aliases mapped to canonical stored values.
_MODE_ALIASES = {
    "24/7": "continuous",
    "24x7": "continuous",
    "record": "continuous",
    "continuous_record": "continuous",
    "continuous-record": "continuous",
    "always_record": "always",
    "always-record": "always",
    "motion-only": "motion",
    "motion_only": "motion",
    "on-motion": "motion",
    "on_motion": "motion",
    "motion_detect": "motion",
    "smart": "motion",
    "event": "motion",
    "off": "never",
    "disabled": "never",
    "disable": "never",
    "paused": "never",
    "pause": "never",
    "stop": "never",
    "false": "never",
    "0": "never",
    "on": "continuous",
    "true": "continuous",
    "1": "continuous",
}


def normalize_recording_mode(value: Any) -> str:
    """Normalize a user-supplied mode to a canonical stored value.

    Raises ``ValueError`` when missing or unknown.
    """
    if value is None:
        raise ValueError("recording_mode is required")
    if isinstance(value, bool):
        raise ValueError(f"unknown recording_mode {value!r}")
    s = str(value).strip().lower()
    if not s:
        raise ValueError("recording_mode is required")
    # Direct canonical hit.
    if s in VALID_RECORDING_MODES:
        return s
    # Alias hit.
    if s in _MODE_ALIASES:
        return _MODE_ALIASES[s]
    # Be tolerant of separators/case variants, e.g. "Always Record".
    compact = s.replace(" ", "_").replace("-", "_")
    if compact in VALID_RECORDING_MODES:
        return compact
    if compact in _MODE_ALIASES:
        return _MODE_ALIASES[compact]
    raise ValueError(
        f"unknown recording_mode {value!r}, expected one of: "
        + ", ".join(VALID_RECORDING_MODES)
    )


async def bulk_set_group_recording_mode(
    db: AsyncSession,
    group_ids: Any,
    recording_mode: Any,
) -> Dict[str, Any]:
    """Set ``recording_mode`` on all member cameras of the given groups.

    Returns a summary dict with ``updated`` camera ids, per-group counts
    and ``missing`` group ids that did not resolve.
    """
    from app.services import group_bulk as _gb

    mode = normalize_recording_mode(recording_mode)

    groups = await _gb.resolve_groups(db, group_ids)
    wanted = _gb._coerce_int_list(  # noqa: SLF001
        group_ids
        if not isinstance(group_ids, dict)
        else (
            group_ids.get(
                "group_ids", group_ids.get("ids", group_ids.get("group_id", []))
            )
        )
    )
    resolved_ids = {g.id for g in groups}
    missing = [i for i in wanted if i not in resolved_ids]

    updated: List[int] = []
    per_group: Dict[str, int] = {}
    for group in groups:
        cams = await _gb.get_group_cameras(db, group.id)
        count = 0
        for cam in cams:
            if hasattr(cam, "recording_mode"):
                cam.recording_mode = mode
            count += 1
            if cam.id not in updated:
                updated.append(cam.id)
        per_group[str(group.id)] = count
    await db.commit()
    return {
        "ok": True,
        "recording_mode": mode,
        "mode": mode,
        "group_ids": sorted(resolved_ids),
        "missing_group_ids": missing,
        "updated_camera_ids": sorted(updated),
        "updated_count": len(updated),
        "per_group": per_group,
    }


# Convenience aliases for tolerant callers / tests.
bulk_set = bulk_set_group_recording_mode
bulk_set_recording_mode = bulk_set_group_recording_mode
set_group_recording_mode = bulk_set_group_recording_mode
set_recording_mode = bulk_set_group_recording_mode
bulk_set_group_recmode = bulk_set_group_recording_mode
set_group_recmode = bulk_set_group_recording_mode

__all__ = [
    "VALID_RECORDING_MODES",
    "normalize_recording_mode",
    "bulk_set_group_recording_mode",
    "bulk_set",
    "bulk_set_recording_mode",
    "set_group_recording_mode",
    "set_recording_mode",
    "bulk_set_group_recmode",
    "set_group_recmode",
]
