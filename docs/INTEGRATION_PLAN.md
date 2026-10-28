# VigilCam Integration Plan

This document tracks the multi-platform integration effort. Reference
repositories remain in `/opt/others`; VigilCam links them under
`app/reference` for development.

## Source mapping

- ZoneMinder -> V4L/USB capture, Linux packaging, ONVIF workflows
- Frigate -> camera pipelines, motion detection, detectors, events UI
- OpenNVR -> camera drivers, setup wizard, audit, compose installers
- Shinobi -> plugin and web UI patterns
- Moonfire NVR -> segmented storage and retention efficiency
- iSpy -> onboarding and device discovery UX

## Integration waves

Wave 1: Camera driver registry and ONVIF baseline
Wave 2: FFmpeg ingestion and recording retention
Wave 3: Motion gating and multiprocessing
Wave 4: AI detector adapters
Wave 5: Web setup wizard and rules UI
Wave 6: MCP and standing rules
Wave 7: Outbound relay and remote access

## Immediate next task

Adapt OpenNVR camera drivers into `app/drivers` with VigilCam database models.
