# VisionSync — Camera Configuration Module

Part of the **Sentivise.AI CCTV Intelligence Platform**. This is the
**Camera Management** module: the single source of truth for Nx Witness
camera identity and streams, described in Section 3.3 of the project
handoff doc.

> Setting this up on a new device or a new project? See **[DEPLOY.md](DEPLOY.md)**
> for the full clone → configure → run walkthrough.

## What this module owns

- The **one** Nx Witness Server credential the entire platform needs (never
  per-camera credentials — Nx already manages those internally).
- **Dynamic camera discovery** via Nx's REST API (`GET /rest/v1/devices`) —
  camera IDs, names, MAC/physicalId, online status. Never a hardcoded list.
- Building Nx's RTSP relay URL for a given camera ID, so nothing downstream
  needs a raw camera IP or per-camera password.
- A live MJPEG preview stream for any camera by ID.
- Persisting **which camera each consuming module currently points at**:
  `DWELL_CAMERA_ID` / `POS_CAMERA_ID` for the POS-Dwell review panels (the
  floor — always present, never removable), plus any number of additional
  project-specific camera views added on top of that floor.
- **VisionSync**, the browser UI operators use to pick/preview cameras and
  add/remove extra camera views per project.
- Starting/stopping its own process (`/api/system/shutdown`, wired to the
  UI's Terminate button) — this only stops VisionSync itself, not any other
  module.

## What this module deliberately does NOT own

Pulled out of the original monolith on purpose, per the platform's module
boundaries — do not add these back here:

- ROI polygon drawing, dwell-time state, or occupancy counting →
  **ROI & Dwell module**.
- Face-Rec engine parameters (match threshold, cooldown, face TTL) and the
  Face Recognition engine's own start/stop/shutdown control →
  **Face Recognition module** (a separate service on its own port — see
  `FaceRecProject/server.py`).
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

On Windows, `Start_VisionSync_System.bat` (silent, `pythonw`) and
`Start_Debug_VisionSync_System.bat` (visible console window, for
troubleshooting) do the above plus open the UI automatically. On
Linux/macOS, `./start_visionsync.sh` (background) and
`./start_visionsync.sh --debug` (foreground, visible output) are the
equivalents. The Terminate System button in the UI calls
`/api/system/shutdown` and closes the tab — it only stops VisionSync's own
process.

## Internal API contract

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/cameras` | List all Nx cameras: `{ "<id>": {id, name, mac, vendor, model, status, online} }`. Add `?force=1` to bypass the 5-minute cache. |
| GET | `/api/cameras/<id>` | Single-camera lookup. |
| GET | `/api/stream?camera_id=<id>` | MJPEG live preview of that camera — `camera_id` is required. |
| GET / POST | `/api/settings` | Get/set the floor camera picks: `DWELL_CAMERA_ID`, `POS_CAMERA_ID`. The response also includes `EXTRA_CAMERAS` (read-only here — manage those through `/api/camera-slots` below). |
| GET / POST | `/api/camera-slots` | GET lists additional camera views beyond the floor: `[{id, label, camera_id}, ...]`. POST adds a new one (auto-labelled `Camera 3`, `Camera 4`, ...) and returns it. |
| PATCH / DELETE | `/api/camera-slots/<id>` | PATCH sets `{camera_id}` and/or `{label}` on one added slot. DELETE removes it. There's no route to delete `DWELL_CAMERA_ID`/`POS_CAMERA_ID` — the floor of 2 is structural, not just a UI rule. |
| POST | `/api/system/shutdown` | Stops this service's own process. Only affects VisionSync — other modules keep running. |
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
