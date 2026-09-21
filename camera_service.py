"""
VisionSync — Camera Configuration Service
by Sentivise.AI / VisionFlow

This is the "Camera Management" module described in the CCTV Intelligence
Platform architecture (Section 3.3 of the project handoff): the single
source of truth for Nx Witness camera identity and streams.

Responsibilities (and ONLY these — everything else stays out of this
service on purpose):
  - Hold the one Nx Witness Server credential (never per-camera credentials).
  - Log in / refresh auth against Nx Witness.
  - Dynamically discover cameras via Nx's REST API (GET /rest/v1/devices) —
    never hardcode a camera list.
  - Build Nx's RTSP relay URL for a given camera ID, so no other module
    needs to know a raw camera IP/RTSP path.
  - Serve a live MJPEG preview of any camera by ID.
  - Persist which camera each *consuming* module currently points at
    (DWELL_CAMERA_ID / POS_CAMERA_ID for the POS-Dwell review panels, plus
    any number of additional project-specific camera slots — minimum 2,
    the two above) so other modules resolve "camera X" to a live stream
    through this service's API instead of talking to Nx directly.

Explicitly OUT of scope for this module (left in their own modules):
  - ROI polygon definition/overlay and dwell-time logic (ROI/Dwell module).
  - Face-Rec engine params (match threshold, cooldown, TTL) and engine
    start/stop/shutdown control (Face Recognition module).

Other modules should talk to THIS service's HTTP API rather than to Nx
Witness directly — see README.md for the internal API contract.
"""

import os
import signal
import threading
import time
import uuid
import hmac
import secrets
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, Response, send_from_directory
from requests.auth import HTTPBasicAuth, HTTPDigestAuth
import urllib3
import cv2
import json

load_dotenv()

app = Flask(__name__)
# No CORS: index.html now calls its API via a relative path (API = "/api"),
# so every real request is already same-origin. The previous wildcard grant
# would let any third-party page a LAN user visits script calls against this
# API — the one holding the platform's single Nx Witness credential.

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- ACCESS TOKEN (same bootstrap as zone-director/scene-intelligence) ---
# This module is also called server-to-server (Scene Intelligence's
# camera_sync_loop polls /api/settings, /api/camera-slots, /api/cameras, and
# /api/internal/resolve-camera — the one that hands back the Nx credential
# embedded in an RTSP URL), not just from this module's own browser UI. That
# caller authenticates with this same token via the VISIONSYNC_TOKEN env var
# on its own side, not by reading this file off disk — keeps the two modules
# talking only over HTTP, per this platform's module-boundary principle.
AUTH_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_config.json")
# See load_auth_token(): tokens generated from here on expire after this
# many days rather than living forever. Tokens that predate this (no
# issued_at recorded) are treated as non-expiring, not retroactively
# invalidated by this update. Note this also affects Scene Intelligence's
# VISIONSYNC_TOKEN — when this rotates, that env var needs the new value too.
AUTH_TOKEN_TTL_DAYS = 30

def ensure_auth_token_exists():
    if os.path.exists(AUTH_CONFIG_FILE):
        return
    token = secrets.token_urlsafe(32)
    fd = os.open(AUTH_CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"token": token, "issued_at": time.time()}, f)
    print("=" * 70)
    print("[i] First run: generated this box's access token.")
    print(f"[i] {AUTH_CONFIG_FILE}")
    print(f"[i] Token: {token}")
    print(f"[i] Expires in {AUTH_TOKEN_TTL_DAYS} days — delete this file and")
    print("[i] restart the service to mint a new one when it does.")
    print("[i] Paste this into the VisionSync UI when it prompts for an access")
    print("[i] token, and set it as VISIONSYNC_TOKEN for any other module (e.g.")
    print("[i] Scene Intelligence) that calls this service's API directly.")
    print("=" * 70)

def load_auth_token():
    """Returns "" (same as missing/unreadable) once the token is past
    AUTH_TOKEN_TTL_DAYS old, so every caller gets the same fail-closed
    behavior without needing its own expiry check."""
    try:
        with open(AUTH_CONFIG_FILE, "r") as f:
            data = json.load(f)
        issued_at = data.get("issued_at")
        if issued_at and (time.time() - issued_at) > AUTH_TOKEN_TTL_DAYS * 86400:
            return ""
        return (data.get("token") or "").strip()
    except Exception as e:
        print(f"[!] Could not read {AUTH_CONFIG_FILE}: {e}")
        return ""

ensure_auth_token_exists()

def _request_is_authorized() -> bool:
    expected = load_auth_token()
    if not expected:
        return False
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer ") and hmac.compare_digest(header[len("Bearer "):].strip(), expected):
        return True
    provided = request.args.get("token", "")
    if provided and hmac.compare_digest(provided, expected):
        return True
    return False

@app.before_request
def require_auth():
    # "/" and static assets (html/js/css/images, see serve_static_page's own
    # extension allowlist) stay public so the page can load and show its own
    # token prompt. Everything under /api is gated, including /api/stream —
    # accepts a `?token=` query param too since the live-preview <img> tags
    # can't send an Authorization header.
    if request.path.startswith("/api/") and not _request_is_authorized():
        return jsonify({"error": "Unauthorized. This box's access token is missing or incorrect."}), 401

@app.route("/api/auth/check", methods=["GET"])
def auth_check():
    return jsonify({"ok": True})

# --- NX WITNESS CONNECTION (the ONE credential this whole platform needs) ---
# All of these come from the environment (.env, gitignored) — never hardcode
# a server URL or credential in code. See .env.example for the shape.
NX_SERVER_URL = os.environ["NX_SERVER_URL"].rstrip("/")
NX_USERNAME = os.environ["NX_USERNAME"]
NX_PASSWORD = os.environ["NX_PASSWORD"]

# Nx Witness's own RTSP relay host/port — video is proxied through Nx using
# the Camera ID (GUID) as the RTSP path, so this service (and anything
# downstream of it) never needs a camera's raw RTSP URL or password.
NX_RTSP_HOST = os.environ["NX_RTSP_HOST"]
NX_RTSP_PORT = int(os.environ.get("NX_RTSP_PORT", "7001"))

# Where camera-selection settings persist across restarts.
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera_settings.json")

DEFAULT_SETTINGS = {
    # Which Nx camera each consuming module currently points at. Other
    # modules read these via GET /api/settings instead of hardcoding a
    # camera ID of their own. These two are the floor — always present,
    # never removable via the /api/camera-slots endpoints below.
    "DWELL_CAMERA_ID": "",   # POS-Dwell "Customer Dwell POV" panel
    "POS_CAMERA_ID": "",     # POS-Dwell "POS Camera Feed" panel
    # Additional, project-specific camera views beyond the floor of 2 above —
    # added/removed freely via /api/camera-slots. Each entry:
    # {"id": "<8-hex>", "label": "Camera 3", "camera_id": "<nx camera id>"}
    "EXTRA_CAMERAS": [],
}

SETTINGS = dict(DEFAULT_SETTINGS)


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)
            SETTINGS.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        except Exception as e:
            print(f"[!] Could not load saved camera settings ({e}) — using defaults.")


def save_settings():
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(SETTINGS, f, indent=2)
    except Exception as e:
        print(f"[!] Could not persist camera settings: {e}")


load_settings()


def build_rtsp_url(camera_id: str) -> str:
    """Nx's RTSP relay URL for a camera — the only way any module should
    ever get a playable stream URL for a camera ID."""
    safe_pass = quote(NX_PASSWORD, safe="")
    return f"rtsp://{NX_USERNAME}:{safe_pass}@{NX_RTSP_HOST}:{NX_RTSP_PORT}/{camera_id}"


def _nx_request(method, path, **kwargs):
    """Calls the Nx Witness REST API, trying Digest auth first and falling
    back to Basic — some Nx deployments (e.g. behind the cloud relay) only
    accept one or the other. Centralized here so no other function
    duplicates the fallback dance."""
    url = f"{NX_SERVER_URL}{path}"
    kwargs.setdefault("timeout", 5)
    kwargs.setdefault("verify", False)
    res = requests.request(method, url, auth=HTTPDigestAuth(NX_USERNAME, NX_PASSWORD), **kwargs)
    if res.status_code == 401:
        res = requests.request(method, url, auth=HTTPBasicAuth(NX_USERNAME, NX_PASSWORD), **kwargs)
    return res


# --- CAMERA DISCOVERY (dynamic — never a hardcoded camera list) ---
# Short in-memory cache so bursty callers (multiple modules polling at once)
# don't each hit Nx individually — camera hardware rarely changes.
_camera_cache = {"data": None, "fetched_at": 0}
CAMERA_CACHE_TTL_SECONDS = 300


@app.route("/api/cameras", methods=["GET"])
def get_cameras():
    """Resolves Nx camera identifiers (Camera ID + MAC/physicalId) so every
    other module can ask "what cameras exist and are they online" without
    ever holding Nx credentials or a hardcoded camera list of its own.

    Response: { "<camera id>": { id, name, mac, vendor, model, status, online }, ... }
    """
    now = time.time()
    force_refresh = request.args.get("force", "").lower() in ("1", "true", "yes")
    if not force_refresh and _camera_cache["data"] and (now - _camera_cache["fetched_at"]) < CAMERA_CACHE_TTL_SECONDS:
        return jsonify(_camera_cache["data"]), 200

    try:
        res = _nx_request("GET", "/rest/v1/devices", params={"_with": "id,name,physicalId,vendor,model,status"})
        res.raise_for_status()

        devices = res.json()
        result = {}
        for d in devices:
            clean_id = (d.get("id") or "").strip("{}")
            if not clean_id:
                continue
            result[clean_id] = {
                "id": clean_id,
                "name": d.get("name"),
                "mac": d.get("physicalId"),
                "vendor": d.get("vendor"),
                "model": d.get("model"),
                "status": d.get("status"),  # e.g. "Online", "Recording", "Offline", "Unauthorized"
                # Nx reports an actively-streaming/recording camera as "Recording", not
                # "Online" — a straight `== "Online"` check misses those.
                "online": d.get("status") in ("Online", "Recording"),
            }

        _camera_cache["data"] = result
        _camera_cache["fetched_at"] = now
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/cameras/<camera_id>", methods=["GET"])
def get_camera(camera_id):
    """Single-camera lookup — convenience wrapper over GET /api/cameras."""
    cameras, status = get_cameras()
    if status != 200:
        return cameras, status
    data = cameras.get_json()
    clean_id = camera_id.strip("{}")
    if clean_id not in data:
        return jsonify({"status": "error", "message": "Camera not found"}), 404
    return jsonify(data[clean_id]), 200


@app.route("/api/internal/resolve-camera", methods=["GET"])
def resolve_camera():
    """Resolves a camera ID straight to a playable RTSP URL, for a module
    running as a Python process on the same box (e.g. the ROI/Dwell
    module's "visionsync" camera source) that can't just embed
    /api/stream's MJPEG. Never holds an Nx credential itself — this is the
    one place that does, per this module's ownership boundary.

    Response: { id, name, rtsp_url }
    """
    camera_id = request.args.get("camera_id", "").strip("{}")
    if not camera_id:
        return jsonify({"status": "error", "message": "camera_id is required"}), 400

    cameras, status = get_cameras()
    if status != 200:
        return cameras, status
    data = cameras.get_json()
    cam = data.get(camera_id)
    if not cam:
        return jsonify({"status": "error", "message": "Camera not found"}), 404

    return jsonify({
        "id": camera_id,
        "name": cam.get("name"),
        "rtsp_url": build_rtsp_url(camera_id),
    }), 200


# --- LIVE PREVIEW STREAM ---
def generate_frames(camera_id: str):
    """Streams one Nx camera as MJPEG. No ROI overlay here on purpose — ROI
    drawing/logic belongs to the ROI/Dwell module, not Camera Management."""
    cap = cv2.VideoCapture(build_rtsp_url(camera_id))
    try:
        while True:
            success, frame = cap.read()
            if not success:
                time.sleep(0.5)
                if not cap.isOpened():
                    cap.release()
                    cap = cv2.VideoCapture(build_rtsp_url(camera_id))
                continue

            frame = cv2.resize(frame, (640, 360))
            ret, buffer = cv2.imencode(".jpg", frame)
            frame_bytes = buffer.tobytes()

            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n")
    finally:
        cap.release()


@app.route("/api/stream")
def video_stream():
    """?camera_id=<id> is required — every camera view (Dwell, POS, or any
    added camera slot) always passes its own camera_id explicitly."""
    camera_id = (request.args.get("camera_id") or "").strip("{}")
    if not camera_id:
        return jsonify({"status": "error", "message": "camera_id query param is required"}), 400
    return Response(generate_frames(camera_id), mimetype="multipart/x-mixed-replace; boundary=frame")


# --- CAMERA-SELECTION SETTINGS ---
# Which camera each consuming module currently points at. This is the ONLY
# settings surface this service owns — ROI values and Face-Rec engine
# params live in their own modules' settings, not here.
@app.route("/api/settings", methods=["GET", "POST"])
def manage_settings():
    if request.method == "POST":
        data = request.json or {}
        if "DWELL_CAMERA_ID" in data:
            SETTINGS["DWELL_CAMERA_ID"] = str(data["DWELL_CAMERA_ID"]).strip("{}")
        if "POS_CAMERA_ID" in data:
            SETTINGS["POS_CAMERA_ID"] = str(data["POS_CAMERA_ID"]).strip("{}")
        save_settings()
        return jsonify({"message": "Updated"}), 200
    return jsonify(SETTINGS), 200


# --- ADDITIONAL CAMERA SLOTS (beyond the floor of Dwell + POS) ---
# Lets a project add more live camera views than the two built-in ones,
# without ever going below that floor — there's no route to delete Dwell or
# POS, only entries created here.
@app.route("/api/camera-slots", methods=["GET", "POST"])
def camera_slots():
    if request.method == "POST":
        next_number = len(SETTINGS["EXTRA_CAMERAS"]) + 3  # 1=Dwell, 2=POS, extras start at 3
        slot = {"id": uuid.uuid4().hex[:8], "label": f"Camera {next_number}", "camera_id": ""}
        SETTINGS["EXTRA_CAMERAS"].append(slot)
        save_settings()
        return jsonify(slot), 201
    return jsonify(SETTINGS["EXTRA_CAMERAS"]), 200


@app.route("/api/camera-slots/<slot_id>", methods=["PATCH", "DELETE"])
def camera_slot(slot_id):
    slots = SETTINGS["EXTRA_CAMERAS"]
    idx = next((i for i, s in enumerate(slots) if s["id"] == slot_id), None)
    if idx is None:
        return jsonify({"status": "error", "message": "Camera slot not found"}), 404

    if request.method == "DELETE":
        slots.pop(idx)
        save_settings()
        return jsonify({"message": "Removed"}), 200

    data = request.json or {}
    if "camera_id" in data:
        slots[idx]["camera_id"] = str(data["camera_id"]).strip("{}")
    if "label" in data:
        slots[idx]["label"] = str(data["label"])[:60]
    save_settings()
    return jsonify(slots[idx]), 200


@app.route("/api/system/shutdown", methods=["POST"])
def shutdown_system():
    """Stops this service. The browser-side terminate button calls this then
    closes its own tab — the process exit is what actually frees the port."""
    def kill_server():
        time.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=kill_server, daemon=True).start()
    return jsonify({"message": "VisionSync shutting down..."}), 200


@app.route("/api/health", methods=["GET"])
def health():
    """Lightweight liveness/connectivity check other modules (or a
    orchestrator like N8N) can poll before assuming this service — and Nx
    itself — is reachable."""
    try:
        res = _nx_request("GET", "/rest/v1/devices", params={"_with": "id"}, timeout=3)
        nx_ok = res.status_code < 500
    except Exception:
        nx_ok = False
    return jsonify({"status": "ok", "nx_reachable": nx_ok}), 200


# --- STATIC UI (VisionSync front end) ---
_ALLOWED_STATIC_EXTENSIONS = {".html", ".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".ico"}


@app.route("/", defaults={"filename": "index.html"})
@app.route("/<path:filename>")
def serve_static_page(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_STATIC_EXTENSIONS:
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5010"))
    # threaded=True is required, not optional: every camera preview is a
    # long-lived MJPEG connection that never closes on its own. Without this,
    # Flask's dev server handles one request at a time, so with 2+ camera
    # views open at once, every request after the first (a newly switched
    # camera included) queues behind whichever stream(s) are already open
    # and never renders.
    #
    # host 0.0.0.0 (not the Flask default of 127.0.0.1) since DEPLOY.md
    # documents other modules/machines reaching this service over the LAN
    # on this port.
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
