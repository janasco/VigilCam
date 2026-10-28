# iSpy / Agent DVR Feature Parity

This document compares the feature set advertised by iSpy / Agent DVR with the
current VigilCam implementation. It is a planning and tracking aid, not a
release note. Statuses describe the current codebase in `/opt/repo/vigilcam`.

## Status legend

- **Implemented** - Functionality exists end-to-end enough for normal use (API
  and/or UI plus backing service).
- **Partial** - Some implementation exists, but it is limited, stubbed, not wired
  into the UI, or lacks important iSpy capabilities.
- **Missing** - No meaningful implementation found.

## Scope note

iSpy and Agent DVR are Windows-first, browser-based surveillance products with
a very large UI. VigilCam is currently a Linux/Docker-first FastAPI + Bootstrap
application with a single-page dashboard. Many "implemented" rows below are
backend-first and still need UI polish or production hardening.

---

## 1. Platform and deployment

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| Browser-based local UI | Full local web UI, no cloud login required | Partial | `app/templates/index.html`, `app/templates/setup.html`; basic dashboard only | Expand dashboard into multi-tab settings and live views |
| Runs as a service / background process | Windows service, Docker, Linux, macOS, Raspberry Pi | Partial | Linux/Docker supported; `install.sh`, `Dockerfile`, `docker-compose.yml` | Add Windows/macOS service installers and RPi guidance |
| Remote access without port forwarding | iSpyConnect relay, mobile network access | Partial | `app/api/relay.py`, `app/services/relay.py`, `RelaySettings` | Implement persistent outbound tunnel and pairing flow |
| Unlimited cameras and microphones | Unlimited local cameras/mics | Partial | Camera model has `audio_enabled`; no microphone device model | Add microphone/audio source support |
| ZeroConf / network discovery | Agent DVR ZeroConf discovery | Partial | `app/core/discovery.py`, `start_broadcast` | Advertise `_vigilcam._tcp` and document discovery |
| Backup / restore configuration | Copy, backup and restore configurations | Missing | No backup/restore API found | Add config export/import and scheduled backups |
| Profiles / quick system presets | Create profiles for system-wide settings | Missing | No profile model/API | Add profile presets for arming and recording modes |

## 2. Camera and device support

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| USB webcams | Local USB camera capture | Partial | `Camera.kind = usb`; ingestion expects RTSP URL, no V4L2 capture | Add V4L2/USB frame capture path |
| IP / RTSP cameras | RTSP/H.264/H.265 ingestion | Partial | `StreamCopyIngestor` uses FFmpeg RTSP TCP | Add HTTP/MJPEG and RTSP over UDP/ONVIF URLs |
| HTTP / MJPEG cameras | HTTP camera sources | Missing | No MJPEG/HTTP ingestor | Add HTTP source adapter |
| ONVIF cameras | ONVIF discovery, profiles, PTZ, events | Partial | `app/api/onvif*.py`, `app/services/onvif*.py`, `app/drivers/onvif` | Complete WS-Discovery and capability parsing |
| Vendor-specific adapters | Dahua, Hikvision, etc. | Partial | `app/drivers/dahua`, `hikvision`, `cpplus`, `secureye`, `xiongmai` | Unify capability matrix and tests |
| Device database / URL presets | Largest camera URL preset database | Partial | `app/api/source_presets.py`, `app/services/source_presets.py` | Import/expand iSpy camera database |
| Desktop recording | Record desktop/screen | Missing | No desktop source | Add screen capture source if needed |
| Kinect / Ximea / special cameras | Vendor-specific special devices | Missing | Not implemented | Defer unless requested |
| Files as camera source | Play/record from files | Missing | No file source type | Add file input adapter |
| Microphones / audio sources | Independent audio capture | Missing | `audio_enabled` only | Add audio ingestion and storage |
| Synthetic test streams | Test streams for development | Partial | Not explicit; can use URLs | Add synthetic stream generator for tests |

## 3. Recording formats and modes

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| MP4 recording | MP4 output | Partial | FFmpeg segmented MP4 copy | Validate MP4 playback and metadata |
| MKV recording | MKV output | Missing | Only MP4 patterns | Add container option |
| VP8 recording | VP8 encoding | Missing | No VP8 pipeline | Add optional transcode profile |
| MP3 audio recording | MP3 output | Missing | No audio recording | Add audio recording |
| CPU/GPU encoding | Hardware encoders | Partial | `app/services/hwaccel.py` | Wire hwaccel into ingestion command |
| Raw stream / zero-CPU recording | Direct stream copy | Implemented | `-c:v copy` in `StreamCopyIngestor` | Keep as default for supported codecs |
| Flip / rotate / timestamp overlay playback | Playback transforms | Partial | `Camera.rotation` field exists | Implement overlay/transform at playback/export |
| Record on detect | Event-triggered recording | Partial | Recording modes exist; manual start/stop API | Connect motion/AI events to recording triggers |
| Record on alert | Alert-triggered recording | Partial | Actions/alerts exist; recording trigger not explicit | Add alert-to-recording action |
| Record on demand | Manual recording | Implemented | `/api/recordings/start`, `/stop` | Add UI controls and camera binding |
| Record by schedule | Scheduled recording | Partial | `recording_mode`, PTZ scheduler; no recording scheduler | Add recording schedule service |
| Record via API / external events | External trigger | Partial | Recordings API | Document and secure external triggers |
| Timelapse recording | Timelapse mode | Partial | `ExportJob.timelapse_factor` for export only | Add timelapse recording mode |
| Adaptive record mode | Adaptive quality/rate | Missing | No adaptive logic | Design adaptive recording policy |
| Dual streaming (low-res live + high-res record) | Separate sub-stream | Partial | `start_camera(..., sub_url=...)` | Expose sub-stream in camera/UI |
| Pre-event buffer | Ring buffer before event | Partial | `app/services/preroll.py` | Wire preroll to event recording |
| Post-event buffer | Continue after event | Partial | Segment-based; no explicit post buffer | Add post-event recording window |
| Recording status / process watchdog | Monitor FFmpeg processes | Partial | `RecordingManager.status()` polls processes | Add restart/watchdog and health alerts |

## 4. Motion detection

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| Simple motion detectors | Basic motion detection | Partial | `app/services/motion.py`, `motion_worker.py` | Verify detector runtime and UI |
| Configurable motion areas | Zones/areas | Partial | `app/services/motion_zoning.py`, motion zoning API | Add zone editor UI |
| ONVIF motion events | Consume camera motion events | Partial | `app/services/onvif_events.py` | Test event subscription and mapping |
| Trip wires | Line crossing | Missing | No tripwire model | Add tripwire zones and counting |
| Speed detection | Object speed | Missing | No speed tracking | Add tracking + speed calculation |
| Object tracking | Track objects across frames | Missing | No tracker implementation | Add lightweight tracker |
| Heat maps | Motion heatmaps | Missing | No heatmap generation | Add heatmap aggregation/export |
| Counting detectors | Count entries/exits | Missing | No counting logic | Add counting zones and counters |
| Sound detection | Detect audio activity | Missing | No audio pipeline | Add audio level monitoring |
| Sound recognition | Recognize sounds via Listen plugin | Missing | No audio recognition | Defer or integrate plugin |

## 5. Artificial intelligence

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| LLM providers (OpenAI, Claude, Gemini, Ollama, etc.) | AI assistant/LLM support | Partial | `app/api/ai_settings.py`, `app/services/ai.py` | Complete provider config and tests |
| Third-party recognizers (DeepStack, CodeProject.AI, PlateRecognizer) | External AI services | Missing | No adapter | Add provider adapters |
| Built-in object recognition | Local object detection | Partial | Mock/placeholder detection; AI pipeline scaffold | Integrate ONNX/YOLO detector |
| Facial recognition | Face detection/recognition | Missing | No face model | Add face detector + embeddings |
| LPR / ANPR | License plate recognition | Missing | No plate model | Add plate detector/OCR |
| Built-in audio recognition | Audio AI | Missing | No audio AI | Defer |
| Reinforcement feedback to reduce false positives | User feedback loop | Missing | No feedback model | Add alert feedback and tuning |
| AI event filtering | Filter alerts by AI results | Partial | Rules/events exist | Connect detector output to rules |
| AI tagging / search | Tag and search events | Partial | Event tags and embeddings service | Add semantic search UI |

## 6. Analytics

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| People/vehicle counting | Counting lines and zones | Missing | No analytics service | Build analytics module |
| Heatmaps and paths | Visual analytics | Missing | No heatmap/path data | Add aggregation and overlay |
| Occupancy counters | Multi-entrance occupancy | Missing | No occupancy model | Add zone occupancy tracking |
| Dashboards | Custom analytics dashboards | Missing | No dashboard builder | Add analytics dashboard |
| Queue / loitering / tailgating alerts | Advanced analytics alerts | Missing | Rules engine basic | Extend rules with analytics triggers |
| Daily/weekly reports | Scheduled reports | Missing | No report service | Add report generation/email |
| CSV export / API | Analytics data export | Partial | Export API exists for recordings | Add analytics export endpoints |

## 7. Alerts, actions, and integrations

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| Events and actions system | Advanced automations | Partial | `app/api/actions.py`, `app/services/actions.py`, `rules.py` | Unify actions/rules and add triggers |
| Sound actions | Play sound/chime | Missing | No audio action | Add local audio playback action |
| Command actions | Run command | Missing | No command action | Add secured command action |
| URL actions | HTTP URL trigger | Partial | Webhook queue exists | Add URL action with retry |
| Network actions | Network triggers | Missing | No network action | Add if needed |
| Email alerts | SMTP email | Partial | `app/api/email_alerts.py`, `app/services/email_alerts.py` | Complete templates and tests |
| SMS alerts | SMS notifications | Partial | `app/api/sms_alerts.py`, `app/services/sms_alerts.py` | Complete provider integration |
| Webhooks | Discord, Slack, Teams, ntfy, etc. | Partial | `app/api/webhook_queue.py`, `app/services/webhook_queue.py` | Add channel templates and retries |
| MQTT | MQTT pub/sub | Missing | No MQTT service | Add MQTT publisher/subscriber |
| SMTP | Email transport | Partial | Email alerts service | Verify SMTP delivery |
| FTP / SFTP upload | Upload images/recordings | Missing | No FTP/SFTP service | Add upload worker |
| Cloud uploads | Box, Google Drive, Dropbox, OneDrive | Partial | `app/api/cloud_upload.py`, `app/services/cloud_upload.py` | Implement provider connectors |
| Home Assistant integration | HA integration | Missing | No HA adapter | Add HA discovery/MQTT bridge |
| Alexa live view | Alexa skill | Missing | No Alexa integration | Defer |
| IFTTT | IFTTT triggers | Missing | No IFTTT integration | Defer |
| Telegram | Telegram alerts | Missing | SMS/webhooks only | Add Telegram bot notifier |
| Full API | Complete REST API | Partial | Many routers under `app/api` | Stabilize and document API |

## 8. PTZ, talk, and device control

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| PTZ (ONVIF, digital, IP) | Pan/tilt/zoom control | Partial | `app/api/ptz*.py`, `app/api/onvif_ptz.py`, `services/ptz*` | Consolidate PTZ APIs and UI |
| PTZ presets | Save/recall presets | Partial | PTZ preset endpoints | Test ONVIF preset tokens |
| PTZ scheduler | Scheduled PTZ moves | Partial | `app/services/ptz_scheduler.py`, `PTZSchedule` | Add cron validation and UI |
| PTZ object tracking | Track object with PTZ | Missing | No tracker | Add tracking-driven PTZ |
| Talk (local/device playback) | Two-way audio/talk | Partial | `app/api/talk.py`, `app/services/talk.py` | Implement device audio playback |
| Text to speech via actions | TTS announcements | Missing | No TTS service | Add TTS action provider |
| Digital PTZ / zoom | Software zoom/pan | Missing | No digital PTZ | Add client-side digital PTZ |

## 9. Storage management

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| Multiple media locations / rules | Storage areas | Partial | `StorageArea`, `app/api/storage*.py`, `storage_quota.py` | Add storage area CRUD and usage |
| Max size limits | Cap storage size | Partial | Retention max bytes | Enforce per-camera/per-area limits |
| Max age limits | Delete old media | Implemented | `RetentionManager`, retention API/tests | Keep and document |
| Archival options | Archive recordings | Partial | Export exists; no archive tier | Add archive storage tier |
| Storage / disk space alerts | Low disk alerts | Partial | `system_metrics` service | Add disk threshold alerts |
| Automatic archival and archive delete | Auto archive/delete | Partial | Retention deletes; no archive | Implement archive lifecycle |
| Archived recordings accessible in UI | View archives | Partial | Recordings list | Add archive filters |
| Free-space retention | Delete based on free space | Partial | Retention max bytes | Add free-space policy |
| Media reconciler | Repair DB/filesystem mismatch | Partial | `segment_repair.py`, tests | Schedule reconciler and report |

## 10. Layouts, views, and navigation

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| Custom layouts / views | Up to 9 views | Partial | `Layout`, `app/api/layouts.py` | Add layout editor and rendering |
| Grid and rotating device list | Live view arrangements | Partial | `liveGrid` in UI | Add rotating list and presets |
| Special slots for alerted/motion devices | Dynamic slots | Missing | No dynamic slot logic | Add alert-driven layout slots |
| Timeline in layout | Historical timeline panel | Missing | No timeline API/UI | Add timeline component |
| Photos in layout | Snapshot panel | Partial | Snapshots service | Add snapshot gallery panel |
| Recordings in layout | Recordings panel | Partial | Recordings list | Embed recordings viewer |
| Floorplans in layout | Floorplan overlay | Partial | `app/api/floorplans.py`, `floorplan.py` | Add device placement on floorplan |
| Picture in picture | PiP live view | Missing | No PiP control | Add browser PiP support |
| VR mode | Immersive viewing | Missing | No VR support | Defer |

## 11. Timeline, media, and search

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| Timeline playback | Synchronized historical playback | Missing | No timeline API | Add timeline aggregation |
| TimeMachine sync playback | Multi-camera sync | Missing | No sync playback | Add synchronized playback |
| Fast / slow / seek / frame-by-frame | Playback controls | Missing | No player controls | Add media player controls |
| Zoomable recordings/photos UI | Zoom and pan | Missing | Basic list only | Add zoomable media viewer |
| Sortable / searchable recordings | Search and sort | Partial | Events filters; recordings list basic | Add recording filters/search |
| Bulk operations | Bulk delete/export/tag | Partial | Export job exists | Add bulk actions API/UI |
| Cutting recordings | Trim clips | Partial | Export endpoint only | Add clip trim tool |
| Archiving / deleting / uploading tools | Media management | Partial | Export/upload partial | Complete media management |
| Advanced search with AI/manual tags | Event search | Partial | `Event.tags`, filters by camera/time/tag/confidence | Add full-text and semantic search |
| Save filters | Saved views/filters | Missing | No saved filter model | Add saved filter presets |
| File locking | Protect recordings from deletion | Missing | No lock flag | Add recording lock field |

## 12. Live view and UI

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| Mobile-responsive UI | Works on mobile | Partial | Bootstrap responsive, basic | Test mobile and improve touch |
| Push notifications | Mobile push alerts | Missing | No push service | Add web push / mobile push |
| Multiple themes | 6 UI themes | Missing | Single `vigilcam.css` | Add theme switcher |
| Translated UI | 28 languages | Partial | `app/services/i18n.py`, `app/locales/en.json` only | Add translation files and loader |
| PTZ and talk in UI | Controls in live view | Partial | PTZ tab exists | Embed controls in live view |
| Full permissions system | Grant access to others | Partial | `User`, `Role`, `CameraPermission` | Complete RBAC and admin UI |
| Secure local login | Login required | Partial | `app/api/auth.py`, `app/services/auth.py` | Enforce auth on all endpoints |
| 2FA | Two-factor auth | Missing | No 2FA | Add TOTP and recovery codes |
| Inbuilt help / docs links | Contextual help | Missing | No help UI | Add help links |
| Context menus | Right-click actions | Missing | No context menus | Add UI context actions |

## 13. Scheduling, rules, and profiles

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| Scheduler with sunrise/sunset | Time-based automation | Missing | No sunrise/sunset calc | Add scheduler with astro times |
| Events and actions automation | Rule engine | Partial | `StandingRule`, `app/services/rules.py` | Expand rule conditions/actions |
| Standing natural-language rules | NL rules with expiry | Implemented | `StandingRule`, rules API | Add NL parser/UI |
| Tasks and commands for manual use | Manual commands | Partial | Actions API | Add task runner UI |
| Recording schedules | Per-camera recording schedule | Missing | `recording_mode` only | Add schedule model/API |
| Camera profiles | Quick camera setting profiles | Missing | No camera profiles | Add profile presets |

## 14. Remote access, relay, and MCP

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| Remote web portal | Browser remote access | Partial | Relay settings/events | Build remote access portal |
| No port forwarding | Outbound tunnel | Partial | Relay service scaffold | Implement tunnel client |
| MCP server | AI assistant integration | Partial | `app/mcp_core_server.py` | Expose camera/alert/snapshot tools |
| Assistants can inspect live data | Query alerts/recordings | Partial | MCP server exists | Add read-only query tools |
| Assistants can arm/disarm/control | Control system | Partial | Rules/actions exist | Add safe control endpoints |
| Assistants can create notification rules | NL rules | Partial | Standing rules | Expose rule creation via MCP |

## 15. ONVIF and discovery

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| ONVIF probe | Device probe | Partial | `/api/onvif/probe` | Return full service metadata |
| ONVIF WS-Discovery | Auto-discover cameras | Partial | Discovery service | Implement multicast discovery |
| ONVIF profile parsing | Parse profiles/streams | Partial | `app/drivers/onvif/driver.py` | Parse media profiles and URLs |
| ONVIF PTZ | PTZ via ONVIF | Partial | `app/api/onvif_ptz.py` | Test move/preset/stop |
| ONVIF events | Event subscriptions | Partial | `app/services/onvif_events.py` | Implement pull-point subscription |
| ONVIF imaging | Brightness/contrast/etc. | Missing | No imaging service | Add imaging settings if supported |

## 16. Extensibility and plugins

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| Plugin system | DLL/plugins with source | Missing | No plugin loader | Design Python plugin API |
| Gain plugin | Video gain | Missing | Not implemented | Defer |
| Listen plugin | Sound recognition | Missing | Not implemented | Defer |
| LiveDelay plugin | Delayed live view | Missing | Not implemented | Defer |
| Weather overlays | Overlay weather | Missing | Not implemented | Defer |
| Adapter SDK | External AI adapters | Partial | Driver registry | Define adapter SDK |

## 17. Setup and onboarding

| Feature | iSpy / Agent DVR | VigilCam status | Evidence / notes | Next action |
|---|---|---|---|---|
| Environment check | First-run checks | Partial | `templates/setup.html` step 1 | Add dependency/service checks |
| Network probe / auto-discovery | Discover cameras | Partial | Setup probe form | Add subnet scan |
| Add device wizard | Guided camera add | Partial | Camera form + ONVIF probe | Build multi-step wizard |
| Baseline AI assignment | Assign detector per camera | Partial | AI settings exist | Add per-camera baseline model |
| Storage setup | Configure storage | Partial | Setup storage step | Add storage area wizard |

---

## Summary counts (approximate)

| Status | Count |
|---|---:|
| Implemented | 6 |
| Partial | 86 |
| Missing | 52 |

## Recommended next wave

1. Complete ONVIF discovery/profile parsing and camera onboarding wizard.
2. Make recording event-driven with pre/post buffers and schedules.
3. Implement real detector integration and connect to rules/actions.
4. Add motion zoning UI, tripwires, and counting analytics.
5. Build storage areas, cloud upload connectors, and media management UI.
6. Expand i18n, themes, permissions, and mobile responsiveness.
7. Implement outbound relay and MCP tool surface.
8. Add plugin/adapter SDK for external detectors and actions.
