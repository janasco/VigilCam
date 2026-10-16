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

## Get it running

### Linux / macOS (native)

```bash
git clone https://github.com/janasco/VigilCam.git
cd VigilCam
./install.sh
./start.sh
```

Open <http://localhost:8080>.

### Docker

```bash
git clone https://github.com/janasco/VigilCam.git
cd VigilCam
docker compose up -d --build
```

## API

- `GET /health`
- `GET /api/info`
- `GET/POST/PATCH/DELETE /api/cameras`
- `POST /api/cameras/{id}/detect`
- `POST /api/onvif/probe`

Interactive docs: <http://localhost:8080/docs>.

## Roadmap

- Real RTSP ingestion (OpenCV/GStreamer/FFmpeg)
- ONVIF WS-Discovery + profile parsing
- Recording (continuous + motion/triggered) with retention
- Hardware-accelerated recording (VAAPI/NVENC)
- Pluggable detectors (YOLO, MobileNet, etc.) with GPU/Coral support
- Zones, masks, alerts, MQTT/Home Assistant integration

## License

MIT.
