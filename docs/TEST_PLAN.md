# VigilCam Functional Test Plan

## Scope

This matrix tracks the functional coverage expected for the VigilCam
scaffold across backend API, storage, drivers, and frontend wiring. Each
row maps a user-facing capability to a minimal verification.

## Test Matrix

| ID | Area | Scenario | Endpoint / Function | Expected Result | Type |
|---|---|---|---|---|---|
| FT-001 | Config | Load settings with defaults | `Settings()` | `RETENTION_MAX_AGE_DAYS` and `RETENTION_MAX_TOTAL_BYTES` present | Unit |
| FT-002 | Retention | Age-based pruning selection | `RetentionManager.run(dry_run=True)` | Old MP4 files selected, not deleted on dry run | Unit |
| FT-003 | Retention | Total-size pruning selection | `RetentionManager.run(dry_run=True)` | Oldest files selected until under byte limit | Unit |
| FT-004 | Retention | Reads settings by default | `RetentionManager()` | `max_age_days`/`max_total_bytes` from settings | Unit |
| FT-005 | Retention | Recording manager status | `/api/recordings/status` | Retention policy reflected in status | API |
| FT-006 | Retention | Run retention API | `POST /api/recordings/retention/run` | Returns deleted candidates and `storage_root` | API |
| FT-007 | Cameras | Create camera | `POST /api/cameras` | 201 with persisted camera | API |
| FT-008 | Cameras | Get missing camera | `GET /api/cameras/{id}` | 404 | API |
| FT-009 | Cameras | Update camera | `PATCH /api/cameras/{id}` | Fields updated | API |
| FT-010 | Cameras | Delete camera | `DELETE /api/cameras/{id}` | 204 | API |
| FT-011 | Detection | Simulate detection | `POST /api/cameras/{id}/detect` | Detections and events recorded | API |
| FT-012 | Drivers | Select driver by manufacturer | `GET /api/drivers/select` | Returns driver class name | API |
| FT-013 | Drivers | Reprobe camera capabilities | `POST /api/cameras/{id}/reprobe` | Capability row updated in DB; probe result returned | API |
| FT-014 | Drivers | Reprobe missing camera | `POST /api/cameras/{id}/reprobe` | 404 | API |
| FT-015 | Frontend | Reprobe button | `runReprobe(id)` | Fetches reprobe endpoint | UI |
| FT-016 | Import | Import app | `import app.main` | No import errors | Smoke |
| FT-017 | Health | Health endpoint | `GET /health` | `status: ok` | API |

## Execution

1. Activate the project virtualenv: `.venv/bin/activate`
2. Run unit tests: `.venv/bin/pytest tests/`
3. Start the app and smoke-test endpoints with `curl` or a browser.
4. Exercise an unreachable camera to confirm reprobe reports `error`/`unreachable` instead of crashing.
