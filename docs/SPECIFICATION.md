# VigilCam Platform Specification

Version: 0.2 draft
Status: Engineering specification
Owner: janasco

## 1. Product Vision

VigilCam is an open-source, installable, AI-driven NVR and surveillance platform
for USB cameras, IP network cameras, RTSP/HTTP streams, and ONVIF-compatible
devices. It is intended as a modern alternative to ZoneMinder, Frigate, OpenNVR,
Shinobi, Moonfire NVR, and iSpy, with emphasis on easy installation, mixed
camera support, local-first AI analytics, a modern web interface, and predictable
operations.

## 2. Supported Camera Types

- USB webcams (V4L2/UVC)
- RTSP/H.264/H.265 cameras
- HTTP/MJPEG cameras
- ONVIF Profile S/T/G devices
- Vendor-specific cameras through adapters
- Synthetic test streams

## 3. Key Product Requirements

- ONVIF, IP, and USB camera support
- Low-latency live viewing
- Continuous and event-triggered recording
- AI event detection and rules
- Remote voice-driven camera questions via multimodal VLM
- Motion-gated processing to reduce CPU usage
- Zero-transcode recording
- ROI zoning and customizable thresholds
- Provider-agnostic AI backends
- Built-in MCP server for external AI clients
- Standing natural-language rules with expiration
- Outbound privacy-preserving relay instead of port forwarding

## 4. Architecture Summary

Logical components:

- Web UI
- Backend API
- Media gateway / ingestion
- Recorder
- Motion and AI workers
- Storage
- Message and scheduling services
- Integration adapters

Deployment modes:

- `single`: modular monolith for small sites
- `compose`: containerized services for standard installs

## 5. Camera Driver Contract

All cameras use a driver interface returning structured capabilities. Drivers do
not implement factory reset or unsafe IP changes. Unsupported capabilities are
reported as data, not exceptions.

## 6. Data Model

Core entities include users, roles, cameras, streams, recordings, events,
zones, rules, alerts, models, audit logs, integrations, and standing rules.

## 7. AI Pipeline

- Frame sampling
- Motion gating
- Object detection
- Tracking
- Specialized models
- Rule engine
- Event publishing
- Evidence capture
- Embedding generation

## 8. Installation

- Linux native wizard-style installation
- Docker Compose installation
- Generated secrets
- Database migrations
- Storage directories
- Health checks

## 9. Security

- Generated credentials
- Encrypted camera secrets
- RBAC and API keys
- TLS and air-gapped mode
- Audit logging
- Privacy-preserving AI context retrieval

## 10. Roadmap

Phase 0: Foundation and scaffold
Phase 1: Ingestion, recording, playback, health
Phase 2: AI events, zones, rules, alerts
Phase 3: Advanced analytics, search, integrations
Phase 4: Scale and enterprise features

## 11. iSpy-Inspired Setup Flow

1. Environment check
2. Network probe and auto-discovery
3. Add device wizard
4. Baseline AI assignment

## 12. Port and Discovery

- Default port: 11947
- mDNS service: `_vigilcam._tcp`
- Local hostname: `http://vigilcam.local:11947`
- `config/initialized.lock` controls first-launch routing.

## 13. Technology Stack

- Backend: Go or FastAPI
- Frontend: Svelte or React + Vite
- Video: FFmpeg
- AI: ONNX Runtime
- Database: SQLite + Redis

## 14. Multimodal AI Pipeline

Voice/audio → intent routing → camera snapshot → VLM → text-to-speech response.

Supports local Ollama/llama.cpp and cloud providers through an OpenAI-compatible
configuration.

## 15. Performance Architecture

- OpenCV motion gating
- Multiprocessed frame handling
- Frame dropping for realtime behavior
- Zero-transcode H.264 recording
- Shared-memory image transfer
- Smart object-based retention
- Hardware decode/accelerators as deployment options
- RTSP re-streaming through MediaMTX
- Dynamic combined camera grid

## 16. ROI Zoning and Recording Modes

- Paint-to-exclude ignored areas
- Threshold percentages
- Modes: always, motion, manual, scheduled
- Granular time profiles

## 17. Alert and Action Framework

- Local actions: snapshots, GPIO, chimes
- Remote actions: MQTT, webhooks, Telegram/Discord
- Decoupled event bus

## 18. MCP Server

Built-in stdio MCP server exposing camera inventory, alert queries, snapshot
references, and system state controls to AI clients while keeping footage local.

## 19. Standing Rules

Natural-language rules are parsed into structured constraints and stored with
expiration. The event loop evaluates detections against active rules.

## 20. Outbound Relay

Persistent outbound TLS tunnel avoids dynamic DNS, port forwarding, and
insecure exposure. Cloud relay is blind and data is end-to-end encrypted.

## 21. Reference Inspiration

ZoneMinder, Frigate, OpenNVR, Shinobi, Moonfire NVR, and iSpy inform the design.
EOF
wc -l /opt/repo/vigilcam/docs/SPECIFICATION.md