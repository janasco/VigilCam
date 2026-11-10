# VigilCam

**AI-driven NVR for USB, IP (RTSP/HTTP) and ONVIF cameras.**  
An open, installable alternative to ZoneMinder, Frigate, OpenNVR, Shinobi and
Moonfire NVR — with a focus on easy installation, a modern web UI and an
extensible AI detection pipeline.

## Features (scaffold)

- Installable on a fresh machine with one command (`./install.sh` then `./start.sh`)
- Supports **USB**, **IP (RTSP/HTTP)** and **ONVIF** camera definitions
- Web UI for camera management, manual AI detection and an event log
- Mock AI detection pipeline shaped like a real detector (ready to swap for
  ONNX/TensorRT/OpenVINO/YOLO)
- SQLite storage for cameras, events and recording metadata
- ONVIF probe endpoint (stub; ready for `python-onvif-zeep`)
- Dockerfile + docker-compose for containerised installs

## Core Camera Support

- **USB Cameras (V4L2 / UVC)**: Native Linux `/dev/video*` enumeration and frame capture with resolution/FPS negotiation.
- **IP Cameras (RTSP / HTTP / MJPEG)**: Zero-transcode RTSP H.264/H.265 stream-copy ingestion, sub-stream support for low-overhead live view and motion analysis, and HTTP/MJPEG snapshot & stream adapters.
- **ONVIF Discovery & Events**:
  - **WS-Discovery & Network Probe**: Multicast WS-Discovery and subnet scanning to automatically locate ONVIF-compatible cameras on the LAN.
  - **Profile & Capability Probing**: Automatic inspection of ONVIF media profiles, stream URIs, PTZ capabilities, and vendor drivers (Hikvision, Dahua, CP Plus, Secureye, Xiongmai, Generic ONVIF).
  - **PullPoint Event Subscriptions**: Real-time subscription and mapping of camera-side ONVIF motion and tamper events.
- **Camera Source Presets**: Built-in URL template database covering common camera brands and stream paths for rapid onboarding.

## UI & UX Features

- **5-Step Setup Wizard**: Guided first-run onboarding (`/setup`) covering:
  1. Environment & Dependency Checks
  2. Network Probe & Auto-Discovery (ONVIF / USB / LAN scan)
  3. Add Camera Device Wizard
  4. Baseline AI & Motion Configuration
  5. Storage & Retention Setup  
  Includes clickable step-pill navigation for jumping back to completed steps.
- **Dark / Light Theme Toggle**: Persistent theme switcher (`localStorage` key `vigilcam-theme`) synchronized seamlessly across both the Setup Wizard and the main Dashboard.
- **Fullscreen Live Wall**: Dedicated multi-camera monitor wall with camera group filtering, configurable grid layouts, and automatic camera page cycling (`auto-cycle`).
- **Keyboard Shortcuts**: Press `?` anywhere outside an input field to open the Keyboard Shortcuts reference modal:
  - `1` – `8`: Switch directly between the 8 primary navigation tabs (`Dashboard`, `Cameras`, `Events`, `People`, `Review`, `Recordings`, `Automate`, `System`)
  - `W`: Toggle Fullscreen Live Wall
  - `D`: Trigger manual AI detection on the selected camera
  - `R`: Refresh active dashboard data and camera feeds
  - `j` / `k`: Cycle next / previous item in active event or camera lists
- **Custom Confirmation Modals**: Non-blocking, accessible confirmation dialogs replacing native browser prompts for destructive or sensitive actions.

## Platform Capabilities (8-Tab Workspace)

### 1. Dashboard (`1`)
- **Live Camera Grid & Wall**: Real-time camera cards with status badges, group filters, layout switcher, and Fullscreen Live Wall mode.
- **Quick Actions & Telemetry**: One-click detection triggers, system health summary, active recording indicators, and recent alert stream.

### 2. Cameras (`2`)
- **Camera Inventory & Onboarding**: Full CRUD for **USB**, **IP (RTSP/HTTP)**, and **ONVIF** cameras, vendor driver selection, capability reprobing, and source presets.
- **Motion Tuner & ROI Zoning**: Interactive polygon/grid Regions of Interest (ROI), paint-to-exclude masks, sensitivity/threshold tuning, and motion-gated processing.
- **Line-Crossing Tripwires**: Configurable directional tripwires with entry/exit object counting and occupancy analytics.
- **PTZ Control, Auto-Tracking & Schedules**: Manual Pan/Tilt/Zoom controls, preset management, AI-driven **PTZ Auto-Tracking**, **PTZ Patrol Tours**, and **Solar (Sunrise/Sunset) Schedules**.

### 3. Events (`3`)
- **AI Event Log & Filtering**: Filterable event feed by camera, object label, confidence threshold, tag, and time window.
- **License Plate Recognition (LPR / ANPR)**: Automatic license plate detection, OCR extraction, watchlist matching, and plate history lookup.
- **Semantic & Tag Search**: Event tagging, snapshot inspection, and natural-language / embedding-backed event queries.

### 4. People (`4`)
- **Face Recognition & Enrollment**: Known-person gallery, face embedding enrollment, visitor recognition logs, and sighting timelines across cameras.

### 5. Review (`5`)
- **Alert Triage & Feedback Loop**: Review queue for triaging unverified detections, marking false positives/true positives to tune per-camera confidence thresholds, and synchronized multi-camera timeline review.

### 6. Recordings (`6`)
- **Segmented Storage & Playback**: Zero-transcode MP4 segments with pre-roll and post-event buffering, file locking, and retention policies (max age and max storage quota).
- **Lossless Clip Trim**: Fast, zero-re-encode clip trimming (`-c copy`) for extracting exact incident windows.
- **EDL, SRT & Compliance Exports**: Export recordings with Edit Decision Lists (EDL), burned-in or sidecar SRT subtitle telemetry, SHA-256 chain-of-custody manifests, and optional privacy face/plate blurring for compliance.
- **Cloud, FTP/SFTP & S3 Uploads**: Automated and on-demand archival of clips and snapshots to S3-compatible object storage, FTP/SFTP servers, and cloud storage providers.

### 7. Automate (`7`)
- **Standing Rules & Action Engine**: Natural-language standing rules with expiration plus structured trigger-action pipelines.
- **Web Push Notifications & Webhooks**: Browser Web Push (VAPID) alerts, outgoing webhook queues with retry logic, MQTT publishing, and SMS/Telegram/Discord notifications.
- **Email Digest Scheduler**: Configurable SMTP instant alerts and scheduled daily/weekly HTML email summaries with top event snapshots.

### 8. System (`8`)
- **Hardware Acceleration**: Auto-detection and configuration of GPU/VPU decode and encode acceleration (`VAAPI`, `NVENC`, `QSV`, `V4L2-M2M`, `RKMPP`).
- **AI Model Registry**: Manage local ONNX/YOLO/OpenVINO/TensorRT models and multimodal LLM/VLM providers (Ollama, llama.cpp, OpenAI-compatible endpoints) with fingerprint verification.
- **Multi-Server Node Dispatch**: Distribute camera ingestion and AI inference workloads across cluster child nodes with health monitoring and failover.
- **Dual-NIC Camera LAN Hardening**: Network isolation tools and firewall/routing diagnostics for isolating untrusted PoE camera VLANs/NICs from the corporate or home network.
- **Subaccounts & RBAC**: Role-Based Access Control (`admin`, `operator`, `viewer`), granular per-camera permissions, API key management, audit logging, and outbound relay configuration.

## Get it running

### Linux / macOS (native)

VigilCam is designed to install cleanly on a fresh machine using the included bootstrap and startup scripts:

```bash
git clone https://github.com/janasco/VigilCam.git
cd VigilCam
./install.sh
./start.sh
```

- `./install.sh` checks system prerequisites (Python 3, `venv`, `ffmpeg`, `v4l-utils`), creates `.venv`, installs Python dependencies, initializes storage directories and secrets, and runs database migrations.
- `./start.sh` activates the virtual environment and launches the VigilCam server on port `11947` (configurable via `VIGILCAM_PORT`).

Open <http://vigilcam.local:11947> or <http://localhost:11947> (first launch automatically routes to `/setup` until onboarding is completed).

### Docker

```bash
git clone https://github.com/janasco/VigilCam.git
cd VigilCam
docker compose up -d --build
```

For USB cameras under Docker, pass the video device through in `docker-compose.yml` (e.g. `devices: ["/dev/video0:/dev/video0"]`).

## API

- `GET /health`
- `GET /api/info`
- `GET/POST/PATCH/DELETE /api/cameras`
- `POST /api/cameras/{id}/detect`
- `POST /api/cameras/{id}/reprobe`
- `POST /api/onvif/probe`
- `GET/POST /api/recordings`, `POST /api/recordings/trim`, `POST /api/exports`
- `GET/POST /api/events`, `GET/POST /api/faces`, `GET/POST /api/plates`
- `GET/POST /api/rules`, `GET/POST /api/push`, `GET/POST /api/email-digest`
- `GET/POST /api/nodes`, `GET/POST /api/network/dual-nic`, `GET/POST /api/users`

Interactive docs: <http://localhost:11947/docs> (or <http://localhost:8080/docs> when running on port 8080).

## Documentation

Additional engineering and architecture documents are located in [`docs/`](docs/):

- [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) — Platform architecture, camera driver contract, AI pipeline, and security model
- [`docs/ISPY_FEATURE_PARITY.md`](docs/ISPY_FEATURE_PARITY.md) — Feature matrix and implementation status across camera, AI, recording, UI, and system capabilities
- [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md) — Functional verification matrix and test execution guide
- [`docs/INTEGRATION_PLAN.md`](docs/INTEGRATION_PLAN.md) — Multi-platform reference architecture and integration waves
- [`docs/FEATURE_SCANS_SUMMARY.md`](docs/FEATURE_SCANS_SUMMARY.md) — Reference NVR capability survey

## Roadmap

- Real RTSP ingestion (OpenCV/GStreamer/FFmpeg)
- ONVIF WS-Discovery + profile parsing
- Recording (continuous + motion/triggered) with retention
- Hardware-accelerated recording (VAAPI/NVENC)
- Pluggable detectors (YOLO, MobileNet, etc.) with GPU/Coral support
- Zones, masks, alerts, MQTT/Home Assistant integration

## License

MIT.
