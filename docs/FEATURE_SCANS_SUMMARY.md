# VigilCam Platform and Reference Feature Scans

Consolidated highlights from deep scans of the reference repositories in
`/opt/others`. These are inputs for future VigilCam integration waves.

## ZoneMinder

- Pluggable camera abstraction: local, remote HTTP, FFmpeg, VNC, file, web source.
- V4L2 USB capture and multi-server clustering.
- Pre/post event ring buffers, JPEG and passthrough video writing, codec options.
- Multi-tier storage areas and DB/filesystem audit reconciliation.
- Polygon zones and linked-monitor trigger expressions.
- Event tags, filters, bulk export, timeline, maps, PTZ tracking.
- Hardware acceleration selections per monitor.
- JWT auth/API patterns and fine-grained permissions.
- AI datasets/models/classes/detections schema and YOLO training export.

## Frigate

- Zero-transcode recording with direct segment copy.
- Motion-gated detection and reviewed event categories.
- Deep detector plugin ecosystem and multi-detector backend.
- Separate sub-stream recording and multi-view.
- Embeddings semantic search and auto-generated clip export.
- go2rtc restreaming and MSE/WebRTC live view.
- Birdseye dynamic active camera grid.
- MQTT/WebSocket/Home Assistant integration.
- Free-space retention and media reconciler.
- PWA/server authenticated and unauthenticated media ports.

## OpenNVR

- Camera driver registry with vendor discovery and capability matrix.
- ONVIF digest/control endpoint resolver and service metadata reads.
- MediaMTX video plane, JWT/media signing, recording session/watchdog/reconciler.
- Tier-0 motion/candidate detection over NATS.
- Adapter SDK and KAI-C contract for external AI adapters.
- Model fingerprint/permission governance and local-only sovereignty.
- First-time setup tokens, secrets, MFA/RBAC/API tokens.
- App SDK, app catalog, camera assignments, app keys, entitlements, app egress.
- Evidence store with content-addressed best frames.

## Shinobi

- Multi-source FFmpeg input matrix and HTTP restreaming.
- Continuous/idle/record plus detector-trigger recording.
- HLS/MJPEG/mp4/FLV/ts and embed endpoints.
- Plugins for motion, OpenCV, OpenALPR, cloud AI services.
- Multi-tenant accounts/sub-accounts and map/layout UI.
- OTP/2FA and multi-server child nodes.
- PTZ via node-onvif and camera scheduling.
- Archival/retention and file bin.
- EJS-based monitor editor with masks and stream options.

## Moonfire NVR

- Pure RTSP passthrough H.264/H.265 recording.
- ~1 minute immutable segments with on-demand MP4 export.
- Hybrid SQLite metadata plus sample-file storage.
- MSE-friendly live segmentation over WebSockets.
- Crash-safe recording lifecycle and garbage-table durability.
- Per-stream byte retention and deletion planning.
- Signal records for motion/events.
- Session permission model and CSRF protections.

## iSpy

- Broad source abstraction: webcam, IP, ONVIF, RTSP, Ximea, Kinect, desktop.
- FFmpeg recording with hardware encoders and cloud upload.
- MJPEG remote feeds and iSpyConnect mobile integration.
- AForge motion/blob detection and no-motion/event triggers.
- PTZ scheduling and area selection.
- Extensive camera URL preset catalog and ONVIF plugins.
- Action framework: sound, command, URL, network, email, SMS, camera control.
- Plugin DLL extensibility and localization.
- MSI installer and companion monitor process.
EOF
echo done