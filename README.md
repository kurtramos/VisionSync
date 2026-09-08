# VisionSync — Camera Configuration Module

Part of the **Sentivise.AI CCTV Intelligence Platform**. This is the
**Camera Management** module: the single source of truth for Nx Witness
camera identity and streams, described in Section 3.3 of the project
handoff doc.

## What this module owns

- The **one** Nx Witness Server credential the entire platform needs (never
  per-camera credentials — Nx already manages those internally).
- **Dynamic camera discovery** via Nx's REST API (`GET /rest/v1/devices`) —
  camera IDs, names, MAC/physicalId, online status. Never a hardcoded list.
- Building Nx's RTSP relay URL for a given camera ID, so nothing downstream
  needs a raw camera IP or per-camera password.
- A live MJPEG preview stream for any camera by ID.
- Persisting **which camera each consuming module currently points at**
  (`ACTIVE_CAMERA_ID` for Face Recognition, `DWELL_CAMERA_ID` /
  `POS_CAMERA_ID` for the POS-Dwell review panels).
- **VisionSync**, the browser UI operators use to pick/preview cameras.

## What this module deliberately does NOT own

Pulled out of the original monolith on purpose, per the platform's module
boundaries — do not add these back here:

- ROI polygon drawing, dwell-time state, or occupancy counting →
  **ROI & Dwell module**.
- Face-Rec engine parameters (match threshold, cooldown, face TTL) and
  engine start/stop/shutdown control → **Face Recognition module**.
- Bookmarking to Nx Witness, POS event tagging, employee registry →
  **Sentivise.AI Post-Processing module** / Face Recognition module.

Other modules should resolve "camera X" → a live stream/status by calling
**this service's API**, not by talking to Nx Witness directly.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in real Nx credentials (a real .env is
                        # already included here, pre-filled from the current
                        # FaceRecProject config, for a same-project handoff)
python camera_service.py
```

Defaults to `http://127.0.0.1:5010`. Open `/` for the VisionSync UI.

## Internal API contract

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/cameras` | List all Nx cameras: `{ "<id>": {id, name, mac, vendor, model, status, online} }`. Add `?force=1` to bypass the 5-minute cache. |
| GET | `/api/cameras/<id>` | Single-camera lookup. |
| GET | `/api/stream?camera_id=<id>` | MJPEG live preview of that camera (defaults to `ACTIVE_CAMERA_ID` if omitted). |
| GET / POST | `/api/settings` | Get/set which camera each module currently points at: `ACTIVE_CAMERA_ID`, `DWELL_CAMERA_ID`, `POS_CAMERA_ID`. |
| GET | `/api/health` | `{ status, nx_reachable }` — liveness + Nx connectivity check. |

Any other module (ROI/Dwell, Face Recognition, Local LLM, an N8N flow)
should:
1. Call `GET /api/settings` to find out which camera ID it should be
   watching.
2. Call `GET /api/cameras` (or `/api/cameras/<id>`) to check that camera is
   online, or to build its own camera picker.
3. Never construct an RTSP URL or hold an Nx credential itself — either
   consume `/api/stream`, or (if running as a Python process on the same
   box) import `build_rtsp_url()` from `camera_service.py`.

## Multi-server note

Only one Nx server is in scope today, but if/when a site needs more than
one, extend `NX_SERVER_URL`/credentials into a list of
`{server_url, credential}` pairs rather than adding a second hardcoded set
of env vars — keep this service the only place that shape lives.

## Origin

Extracted from `FaceRecProject/server.py` (the `/api/cameras`,
`/api/stream`, camera-related parts of `/api/settings`) and
`FaceRecProject/settings.html` (the camera pickers + live preview, with ROI
sliders, Face-Rec params, and engine controls stripped out) in the
`1RotaryFaceDetectionModel` repo — that repo remains the working demo;
this folder is the clean starting point for VisionSync as its own
repository.
