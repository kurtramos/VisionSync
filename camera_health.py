"""
VisionSync — Camera Health & Notifications
by Sentivise.AI / VisionFlow

Background monitor that turns Nx Witness's raw device/archive/event data into
the two things operators actually want to see on the VisionSync page:

  1. A per-camera HEALTH picture — status, a 0–100 health score with the
     reasons behind it, streams (configured vs. actual fps/bitrate), recording
     schedule vs. what was actually archived in the last 24h, uptime and
     disconnect history, firmware/IP/MAC/capabilities, and every other device
     parameter Nx exposes (credentials stripped).
  2. A NOTIFICATIONS feed — camera disconnected / back online / login
     failed / unstable connection / added / removed, Nx server reachability,
     storage going offline, plus health-relevant entries from Nx's own event
     log (network issues, IP conflicts, storage and server failures, ...).

Disconnect/reconnect alerts come from this module's own status-transition
tracking (a /rest/v1/devices poll every HEALTH_POLL_SECONDS), NOT from Nx's
event log — so they work on every Nx server generation, whether or not its
event-log API is reachable. The Nx event log is layered on top as a bonus
source when the server exposes one.

Stays inside the Camera Management module's boundary: it only ever reports
on camera identity/status/recording, never ROI, dwell, or Face-Rec logic.
Everything Nx-facing goes through the `nx_request` callable handed in by
camera_service.py, so this file never holds the Nx credential itself.
"""

import json
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from urllib.parse import urlparse

import requests

ONLINE_STATUSES = ("Online", "Recording")

# What each non-online Nx device status means to an operator. Nx reports
# these verbatim in /rest/v*/devices' "status" field.
STATUS_PROBLEMS = {
    "Offline": ("critical", "Camera disconnected", "Nx can't reach this camera on the network."),
    "Unauthorized": ("critical", "Camera login failed", "Nx can reach the camera but its credentials are being rejected — the camera's password may have changed."),
    "Incompatible": ("critical", "Camera incompatible", "Nx reports this camera as incompatible with the server."),
    "MismatchedCertificate": ("critical", "Camera certificate mismatch", "The camera's TLS certificate no longer matches the one Nx trusted."),
    "NotDefined": ("warning", "Camera status unknown", "Nx hasn't determined this camera's status yet."),
}

# Base score a camera gets when it isn't online at all (see _camera_health).
_DOWN_SCORES = {"Offline": 0, "Unauthorized": 5}

# Endpoint spellings shift across Nx server generations (6.x rejects most of
# the legacy /api/* routes outright with 403 "Disabled insecure deprecated
# API"; 5.x lacks the newer /rest/v3+ routes) — every capability below is a
# chain tried in order, modern first. The first spelling that works is
# remembered and tried first from then on.
_UNSUPPORTED_RETRY_SECONDS = 600

# Nx event-log types worth surfacing as notifications. Matched as substrings
# of the event type with case/punctuation stripped, so "cameraDisconnectEvent"
# (legacy), "nx.events.deviceDisconnected" (v4) etc. all match. Anything not
# listed here (motion, generic/soft-trigger, analytics, I/O input) is noise
# for a camera-health feed and is skipped on purpose.
_NX_EVENT_TYPES = [
    (("cameradisconnect", "devicedisconnect"), "critical", "Camera disconnected (Nx event)"),
    (("cameraipconflict", "deviceipconflict", "ipconflict"), "critical", "IP address conflict"),
    (("networkissue",), "warning", "Network issue"),
    (("storagefailure", "storageissue"), "critical", "Storage problem"),
    (("serverfailure",), "critical", "Nx server failure"),
    (("serverconflict",), "warning", "Nx server conflict"),
    (("servercertificateerror",), "warning", "Nx server certificate error"),
    (("serverstart",), "info", "Nx server started"),
    (("licenseissue",), "warning", "Nx license issue"),
    (("backupfinished",), "info", "Archive backup finished"),
    (("poeoverbudget",), "warning", "PoE power over budget"),
    (("fanerror",), "warning", "Hardware fan error"),
    (("plugindiagnostic",), "warning", "Analytics plugin diagnostic"),
]

# Nx event types that repeat every few seconds while a condition persists —
# folded into one feed entry per resource per hour (see _ingest_nx_event).
_AGGREGATED_NX_TYPES = {"ipconflict", "networkissue", "plugindiagnostic"}

_NX_REASON_TEXT = {
    "networknoframe": "No video frames received from the camera",
    "networkconnectionclosed": "Camera connection closed unexpectedly",
    "networkrtppacketloss": "RTP packet loss on the video stream",
    "networknoresponsefromdevice": "Camera stopped responding",
    "networkmulticastaddressconflict": "Multicast address conflict",
    "networkmulticastaddressisinvalid": "Invalid multicast address",
    "networkbadcameratime": "Camera clock is wrong",
    "networkcameratimebackjump": "Camera clock jumped backwards",
    "storageioerror": "Storage I/O error",
    "storagetooslow": "Storage is too slow to keep up with recording",
    "storagefull": "Storage is full",
    "systemstoragefull": "System storage is full",
}

_SENSITIVE_KEY_RE = re.compile(r"password|passwd|credential|secret|token|streamurl", re.IGNORECASE)
_URL_CREDS_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]*@", re.IGNORECASE)

# Well-known FFmpeg AVCodecID values Nx stores in its "mediaStreams" device
# parameter (both the older and newer enum numbering, which shifted by one).
_CODECS = {7: "MJPEG", 8: "MJPEG", 12: "MPEG-4", 13: "MPEG-4", 27: "H.264", 28: "H.264", 173: "H.265", 174: "H.265"}

# 1-minute detail: shorter archive gaps than this don't count as gaps.
_GAP_THRESHOLD_MS = 60_000
_DAY_MS = 86_400_000


def _now_ms():
    return int(time.time() * 1000)


def _clean_id(value):
    return (str(value or "")).strip().strip("{}")


def _num(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _json_param(value):
    """Nx stores structured device parameters (mediaStreams, bitrateInfos,
    ...) as JSON-encoded strings inside the parameters map."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return None


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _humanize(camel):
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", str(camel or "")).replace("_", " ").strip()
    return words[:1].upper() + words[1:].lower() if words else ""


def _strip_url_creds(text):
    return _URL_CREDS_RE.sub(r"\1***:***@", text) if isinstance(text, str) else text


def _sanitize(obj, depth=0):
    """Deep-copies Nx data for display with anything credential-shaped
    removed — raw device parameters are shown in the UI, and some Nx
    versions put camera passwords / credentialed stream URLs in there."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        return {k: _sanitize(v, depth + 1) for k, v in obj.items() if not _SENSITIVE_KEY_RE.search(str(k))}
    if isinstance(obj, list):
        return [_sanitize(v, depth + 1) for v in obj[:200]]
    if isinstance(obj, str):
        value = _strip_url_creds(obj)
        return value if len(value) <= 600 else value[:600] + "…"
    return obj


def _host_of(url):
    if not url:
        return None
    url = str(url)
    if "://" not in url:
        url = "http://" + url
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def _walk_dicts(obj, predicate, depth=0):
    """Collects every dict inside an arbitrarily nested Nx response that
    satisfies predicate — response envelopes differ by server generation
    ({reply: [...]}, [{guid, periods: [...]}], a bare list, ...)."""
    found = []
    if depth > 5:
        return found
    if isinstance(obj, dict):
        if predicate(obj):
            found.append(obj)
            return found
        for v in obj.values():
            found.extend(_walk_dicts(v, predicate, depth + 1))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_walk_dicts(v, predicate, depth + 1))
    return found


def _to_ms(value):
    """Nx timestamps arrive as epoch ms, epoch µs (legacy eventTimestampUsec),
    numeric strings, or ISO-8601 depending on endpoint/version."""
    n = _num(value)
    if n is not None:
        if n > 1e14:      # microseconds
            return int(n / 1000)
        if n > 1e11:      # milliseconds
            return int(n)
        if n > 1e8:       # seconds
            return int(n * 1000)
        return None
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _merge_periods(periods):
    periods = sorted(p for p in periods if p[1] > p[0])
    merged = []
    for start, end in periods:
        if merged and start <= merged[-1][1] + 1000:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _stream_key(encoder_index):
    key = str(encoder_index if encoder_index is not None else "").strip().lower()
    if key in ("0", "primary", "primarystream"):
        return "primary"
    if key in ("1", "secondary", "secondarystream"):
        return "secondary"
    return key or "primary"


def parse_streams(params):
    """Merges Nx's mediaStreams (what the camera offers: resolution, codec)
    with bitrateInfos (what Nx actually measured: fps, bitrate) into one
    list — primary first."""
    streams = {}
    media = _json_param(params.get("mediaStreams")) or {}
    for s in (media.get("streams") if isinstance(media, dict) else media) or []:
        # encoderIndex -1 / resolution "*" is Nx's on-demand transcoding
        # pseudo-stream, not something the camera itself produces.
        if not isinstance(s, dict) or str(s.get("encoderIndex")) == "-1" or s.get("resolution") == "*":
            continue
        key = _stream_key(s.get("encoderIndex"))
        codec = s.get("codec")
        streams.setdefault(key, {"name": key})
        streams[key].update({
            "resolution": s.get("resolution"),
            "codec": _CODECS.get(int(codec), f"codec {codec}") if _num(codec) is not None else codec,
        })
    bitrates = _json_param(params.get("bitrateInfos")) or {}
    for b in (bitrates.get("streams") if isinstance(bitrates, dict) else bitrates) or []:
        if not isinstance(b, dict):
            continue
        key = _stream_key(b.get("encoderIndex"))
        streams.setdefault(key, {"name": key})
        entry = streams[key]
        entry["fps"] = _num(b.get("fps"))
        entry["actual_fps"] = _num(b.get("actualFps"))
        entry["bitrate_mbps"] = _num(b.get("actualBitrate"))
        entry["suggested_bitrate_mbps"] = _num(b.get("suggestedBitrate"))
        entry["gop"] = _num(b.get("averageGopSize"))
        entry["configured"] = b.get("isConfigured")
        if not entry.get("resolution"):
            entry["resolution"] = b.get("resolution")
    order = {"primary": 0, "secondary": 1}
    return sorted(streams.values(), key=lambda s: order.get(s["name"], 9))


def _recording_type(value):
    # Nx's REST API omits fields left at their default, and a schedule
    # task's default recordingType is "always" (continuous).
    if value is None:
        return "continuous"
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return {0: "continuous", 1: "motion", 2: "never", 3: "motion_lq"}.get(int(value), "never")
    v = _norm(value)
    if "always" in v:
        return "continuous"
    if "lowquality" in v:
        return "motion_lq"
    if "motion" in v or "metadata" in v:
        return "motion"
    return "never"


def summarize_schedule(schedule):
    schedule = schedule or {}
    enabled = _truthy(schedule.get("isEnabled"))
    secs = {"continuous": 0, "motion": 0, "motion_lq": 0}
    fps_values = []
    for task in schedule.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        start, end = _num(task.get("startTime"), 0), _num(task.get("endTime"), 0)
        duration = max(0.0, min(end, 86400) - max(start, 0))
        rtype = _recording_type(task.get("recordingType"))
        if rtype in secs:
            secs[rtype] += duration
        fps = _num(task.get("fps"))
        if fps:
            fps_values.append(fps)
    week = 7 * 86400
    continuous_frac = secs["continuous"] / week
    motion_frac = (secs["motion"] + secs["motion_lq"]) / week
    if not enabled:
        mode = "Recording off"
    elif continuous_frac >= 0.99:
        mode = "Continuous 24/7"
    elif continuous_frac > 0 and motion_frac > 0:
        mode = "Continuous + motion"
    elif continuous_frac > 0:
        mode = "Continuous (scheduled hours)"
    elif motion_frac > 0:
        mode = "Motion-triggered"
    else:
        mode = "No recording scheduled"

    # Negative archive limits mean "auto" in Nx (the magnitude is just the
    # auto default), so only positive values are real operator settings.
    def _days(key_s, key_d):
        s = _num(schedule.get(key_s))
        if s is not None:
            return round(s / 86400, 1) if s > 0 else None
        d = _num(schedule.get(key_d))
        return d if d and d > 0 else None

    return {
        "enabled": enabled,
        "mode": mode,
        "continuous_fraction": round(continuous_frac, 3),
        "motion_fraction": round(motion_frac, 3),
        "scheduled_fps": max(fps_values) if fps_values else None,
        "min_archive_days": _days("minArchivePeriodS", "minArchiveDays"),
        "max_archive_days": _days("maxArchivePeriodS", "maxArchiveDays"),
    }


class CameraHealthMonitor:
    def __init__(self, nx_request, events_file, poll_seconds=15, footage_seconds=120,
                 event_log_seconds=30, servers_seconds=120, on_devices=None, max_events=1000):
        self._nx = nx_request
        self._events_file = events_file
        self.poll_seconds = max(5, poll_seconds)
        self.footage_seconds = max(30, footage_seconds)
        self.event_log_seconds = max(10, event_log_seconds)
        self.servers_seconds = max(30, servers_seconds)
        self._on_devices = on_devices

        self._lock = threading.RLock()
        self._persist_lock = threading.Lock()
        self._stop = threading.Event()

        self.started_at_ms = _now_ms()
        self.devices = {}          # camera id -> raw Nx device dict
        self.tracks = {}           # camera id -> status tracking (uptime, since)
        self.footage = {}          # camera id -> 24h archive summary
        self.servers = {}          # server id -> summary
        self.storages = []
        self.system = {}
        self.nx_reachable = None
        self.last_poll_ms = None
        self.last_error = None
        self._baseline_done = False

        self.sources = {}          # capability -> endpoint label that worked (or None)
        self._chain_state = {}
        self._storage_online = {}  # storage key -> bool (for offline alerts)

        self.events = deque(maxlen=max_events)
        self._seq = 0
        self._nx_seen = deque(maxlen=3000)
        self._nx_seen_set = set()
        self._nx_last_emit = {}    # (type, resource) -> ms, rate limit for chatty Nx events
        self._event_log_cursor_ms = _now_ms() - _DAY_MS
        self._flap_alerted = {}    # camera id -> ms of last "unstable" alert
        self._dirty_counts = False
        self._load_events()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self):
        threading.Thread(target=self._fast_loop, name="nx-health-devices", daemon=True).start()
        threading.Thread(target=self._slow_loop, name="nx-health-archive", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _fast_loop(self):
        while not self._stop.is_set():
            try:
                self._poll_devices()
            except Exception as e:
                print(f"[!] Health monitor device poll failed: {e}")
            self._stop.wait(self.poll_seconds)

    def _slow_loop(self):
        # Give the first device poll a head start so there's a camera list.
        self._stop.wait(3)
        # [next due time, interval, job]
        jobs = [[0, self.servers_seconds, self._poll_servers],
                [0, self.event_log_seconds, self._poll_event_log],
                [0, self.footage_seconds, self._poll_footage]]
        while not self._stop.is_set():
            if self.nx_reachable:
                for job in jobs:
                    if time.time() >= job[0]:
                        try:
                            job[2]()
                        except Exception as e:
                            print(f"[!] Health monitor {job[2].__name__} failed: {e}")
                        job[0] = time.time() + job[1]
            self._stop.wait(5)

    # ------------------------------------------------------------------ #
    # Nx endpoint chains
    # ------------------------------------------------------------------ #
    def _chain(self, key, attempts, parse, mark_unsupported=True, timeout=8):
        """Tries each (label, path, params) in order, returning
        (label, parsed) for the first spelling that answers 200 with a body
        `parse` accepts (parse raising/returning None = try the next one).
        A 400 with params is retried once without them, since Nx's newer
        REST versions reject parameters they don't know. Network errors
        abort the whole chain (Nx itself is down — not an unsupported
        endpoint)."""
        state = self._chain_state.setdefault(key, {"label": None, "unsupported_until": 0})
        if mark_unsupported and state["unsupported_until"] > time.time():
            return None, None
        if state["label"]:
            attempts = sorted(attempts, key=lambda a: a[0] != state["label"])
        for label, path, params in attempts:
            for p in ([params, None] if params else [None]):
                try:
                    res = self._nx("GET", path, params=p, timeout=timeout)
                except requests.RequestException:
                    return None, None
                if res.status_code == 400 and p:
                    continue
                if res.status_code != 200:
                    break
                try:
                    parsed = parse(res)
                except Exception:
                    parsed = None
                if parsed is None:
                    break
                state["label"] = label
                self.sources[key] = label
                return label, parsed
        if mark_unsupported:
            state["label"] = None
            state["unsupported_until"] = time.time() + _UNSUPPORTED_RETRY_SECONDS
            self.sources[key] = None
        return None, None

    # ------------------------------------------------------------------ #
    # device status polling → status-transition notifications
    # ------------------------------------------------------------------ #
    def _poll_devices(self):
        try:
            res = self._nx("GET", "/rest/v1/devices", timeout=15)
            res.raise_for_status()
            devices = res.json()
            if isinstance(devices, dict):
                devices = devices.get("reply") or devices.get("devices") or []
            if not isinstance(devices, list):
                raise ValueError("unexpected /rest/v1/devices response shape")
        except Exception as e:
            self._set_reachable(False, str(e))
            return
        self._set_reachable(True)
        self.sources["devices"] = "/rest/v1/devices"

        now = time.time()
        now_ms = int(now * 1000)
        fresh = {}
        for d in devices:
            if not isinstance(d, dict):
                continue
            cid = _clean_id(d.get("id"))
            if cid:
                fresh[cid] = d

        with self._lock:
            previous = self.devices
            baseline = self._baseline_done
            self.devices = fresh
            self.last_poll_ms = now_ms

        if baseline:
            for cid in fresh.keys() - previous.keys():
                d = fresh[cid]
                self._emit("info", "camera-added", "New camera discovered",
                           f"{d.get('vendor') or ''} {d.get('model') or ''} appeared on Nx ({d.get('status') or 'unknown status'}).".strip(),
                           camera_id=cid, camera_name=d.get("name"))
            for cid in previous.keys() - fresh.keys():
                self._emit("warning", "camera-removed", "Camera removed from Nx",
                           "This camera is no longer listed on the Nx server.",
                           camera_id=cid, camera_name=previous[cid].get("name"))
                with self._lock:
                    self.tracks.pop(cid, None)

        for cid, d in fresh.items():
            self._track_status(cid, d.get("name"), d.get("status") or "NotDefined", now, now_ms, baseline)

        with self._lock:
            self._baseline_done = True

        if self._on_devices:
            try:
                self._on_devices(devices)
            except Exception as e:
                print(f"[!] Health monitor on_devices callback failed: {e}")

    def _track_status(self, cid, name, status, now, now_ms, baseline):
        with self._lock:
            t = self.tracks.get(cid)
            if t is None:
                self.tracks[cid] = {"status": status, "since_ms": now_ms, "online_s": 0.0, "tracked_s": 0.0, "last_tick": now}
                first_sight = True
            else:
                first_sight = False
                # Cap the tick so time this monitor couldn't see (Nx
                # unreachable, service paused) never counts as up OR down.
                dt = min(now - t["last_tick"], self.poll_seconds * 3)
                if t["status"] in ONLINE_STATUSES:
                    t["online_s"] += dt
                t["tracked_s"] += dt
                t["last_tick"] = now
                prev, prev_since = t["status"], t["since_ms"]
                if status != prev:
                    t["status"], t["since_ms"] = status, now_ms

        if first_sight:
            # Camera already down when monitoring starts (or when it first
            # appears): alert once — unless the persisted history already
            # says it went down and never came back, so a service restart
            # doesn't re-announce the same dead camera every time.
            if status not in ONLINE_STATUSES and self._last_status_event(cid) not in ("camera-offline", "camera-problem"):
                severity, title, detail = STATUS_PROBLEMS.get(status, ("warning", "Camera unavailable", f"Nx reports status '{status}'."))
                when = "when VisionSync started monitoring" if not baseline else "when it was added"
                self._emit(severity, "camera-offline" if status == "Offline" else "camera-problem", title,
                           f"{detail} Already {status.lower()} {when}.", camera_id=cid, camera_name=name, status=status,
                           already_down=True)
            return

        if status == prev:
            return
        was_up, is_up = prev in ONLINE_STATUSES, status in ONLINE_STATUSES
        held_for = _fmt_duration(now_ms - prev_since)
        if was_up and not is_up:
            severity, title, detail = STATUS_PROBLEMS.get(status, ("critical", "Camera unavailable", f"Nx reports status '{status}'."))
            self._emit(severity, "camera-offline" if status == "Offline" else "camera-problem", title,
                       f"{detail} It had been up for {held_for}.", camera_id=cid, camera_name=name, status=status)
            self._check_flapping(cid, name)
        elif not was_up and is_up:
            self._emit("success", "camera-online", "Camera back online",
                       f"Reconnected after {held_for} {prev.lower()}.", camera_id=cid, camera_name=name, status=status,
                       downtime_ms=now_ms - prev_since)
        elif not was_up and not is_up:
            severity, title, detail = STATUS_PROBLEMS.get(status, ("warning", "Camera status changed", f"Nx reports status '{status}'."))
            self._emit(severity, "camera-offline" if status == "Offline" else "camera-problem", title,
                       f"{detail} (was {prev}).", camera_id=cid, camera_name=name, status=status)
        # Online <-> Recording flips constantly on motion-triggered schedules;
        # deliberately not a notification. Archive coverage covers "is it
        # actually recording" instead (see _camera_health).

    def _check_flapping(self, cid, name):
        now_ms = _now_ms()
        drops = self._disconnect_times(cid, now_ms - 3_600_000)
        last = self._flap_alerted.get(cid, 0)
        if len(drops) >= 3 and now_ms - last > 3_600_000:
            self._flap_alerted[cid] = now_ms
            self._emit("warning", "camera-unstable", "Unstable camera connection",
                       f"Dropped {len(drops)} times in the last hour — check its cabling, PoE switch port, or network path.",
                       camera_id=cid, camera_name=name)

    def _set_reachable(self, ok, error=None):
        # One failed poll isn't an outage — the Nx cloud relay regularly
        # times out on a cold first request. Only 2 in a row flip the state.
        self._consecutive_failures = 0 if ok else getattr(self, "_consecutive_failures", 0) + 1
        if not ok and self._consecutive_failures < 2:
            with self._lock:
                self.last_error = error
            return
        with self._lock:
            was = self.nx_reachable
            self.nx_reachable = ok
            self.last_error = None if ok else error
        if ok and was is False:
            self._emit("success", "nx-reachable", "Nx server reachable again", "VisionSync is receiving camera data from Nx Witness again.")
        elif not ok and was is not False:
            reason = "connection refused or timed out" if "Connection" in str(error) or "timed out" in str(error) else str(error)[:140]
            self._emit("critical", "nx-unreachable", "Nx server unreachable",
                       f"VisionSync can't reach Nx Witness ({reason}) — every camera's status is unknown until it's back.")

    # ------------------------------------------------------------------ #
    # recording archive (last 24h)
    # ------------------------------------------------------------------ #
    def _poll_footage(self):
        with self._lock:
            camera_ids = list(self.devices.keys())
        for cid in camera_ids:
            if self._stop.is_set():
                return
            self._fetch_footage(cid)

    def _fetch_footage(self, cid):
        end = _now_ms()
        start = end - _DAY_MS
        modern = {"startTimeMs": start, "endTimeMs": end}
        label, periods = self._chain("footage", [
            ("/rest/v3/devices/{id}/footage", f"/rest/v3/devices/{cid}/footage", modern),
            ("/rest/v2/devices/{id}/footage", f"/rest/v2/devices/{cid}/footage", modern),
            ("/ec2/recordedTimePeriods", "/ec2/recordedTimePeriods",
             {"cameraId": cid, "startTime": start, "endTime": end, "detail": 1000, "periodsType": 0}),
        ], parse=_parse_periods, timeout=10)
        if periods is None:
            return
        clipped = []
        for p_start, p_dur in periods:
            p_end = end if p_dur < 0 else p_start + p_dur
            p_start, p_end = max(p_start, start), min(p_end, end)
            if p_end > p_start:
                clipped.append([p_start, p_end])
        merged = _merge_periods(clipped)
        recorded = sum(e - s for s, e in merged)
        gaps = sum(1 for a, b in zip(merged, merged[1:]) if b[0] - a[1] >= _GAP_THRESHOLD_MS)
        last_end = merged[-1][1] if merged else None
        summary = {
            "fetched_at_ms": end,
            "window_start_ms": start,
            "coverage": round(recorded / _DAY_MS, 4),
            "recorded_ms": recorded,
            "gaps": gaps,
            "last_recorded_ms": last_end,
            "recording_now": bool(last_end and end - last_end < 90_000),
            "periods": merged[-400:],
        }
        with self._lock:
            self.footage[cid] = summary

    # ------------------------------------------------------------------ #
    # Nx's own event log
    # ------------------------------------------------------------------ #
    def _poll_event_log(self):
        end = _now_ms()
        start = self._event_log_cursor_ms
        modern = {"startTimeMs": start, "endTimeMs": end}
        label, entries = self._chain("event_log", [
            ("/rest/v4/events/log", "/rest/v4/events/log", modern),
            ("/rest/v3/events/log", "/rest/v3/events/log", modern),
            ("/api/getEvents", "/api/getEvents", {"from": start, "to": end}),
        ], parse=_parse_event_list, timeout=10)
        if entries is None:
            return
        self._event_log_cursor_ms = end - 60_000  # overlap; de-duped below
        parsed = [e for e in (_parse_nx_event(raw) for raw in entries) if e and e["ts"] >= start - 60_000]
        parsed.sort(key=lambda e: e["ts"])
        self._dirty_counts = False
        for e in parsed[-2000:]:
            self._ingest_nx_event(e)
        if self._dirty_counts:
            with self._lock:
                snapshot, seq = list(self.events), self._seq
            self._persist(snapshot, seq)

    def _ingest_nx_event(self, e):
        key = f"{e['type_key']}|{e['resource']}|{e['ts'] // 1000}"
        if key in self._nx_seen_set:
            return
        self._remember_nx_key(key)

        with self._lock:
            device = self.devices.get(e["resource"])
            server = self.servers.get(e["resource"])
            if not device and e.get("ip"):
                device_id = self._device_for_ip(e["ip"], e.get("macs") or [])
                device = self.devices.get(device_id) if device_id else None
                if device:
                    e["resource"] = device_id
        camera_id = e["resource"] if device else None
        camera_name = (device or {}).get("name") or (None if server else e.get("resource_name"))

        if e["type_key"] == "disconnect" and camera_id:
            # This monitor's own status tracking usually caught it already.
            if any(abs(t - e["ts"]) < 120_000 for t in self._disconnect_times(camera_id, e["ts"] - 120_000, own_only=True)):
                return

        # Repeats of the same type on the same resource within the hour fold
        # into the entry already in the feed as a ×N count — an unresolved IP
        # conflict alone makes Nx log one every ~30s.
        # Disconnects are never folded: each one is a real drop that feeds
        # the camera's disconnect count and "unstable" detection.
        rl_key = (e["type_key"], e["resource"])
        last = self._nx_last_emit.get(rl_key) if e["type_key"] in _AGGREGATED_NX_TYPES else None
        if last and e["ts"] - last["ts"] < 3_600_000:
            with self._lock:
                last["count"] = last.get("count", 1) + 1
                last["last_ts"] = max(last.get("last_ts", last["ts"]), e["ts"])
                # Re-surfaces the entry to ?since= pollers without a new
                # notification (seq, and therefore unread state, is unchanged).
                self._seq += 1
                last["updated_seq"] = self._seq
            self._dirty_counts = True
            return

        message = e["message"]
        if not message and e["type_key"] == "disconnect":
            message = "Nx Witness logged that it lost the connection to this camera."
        if server and not camera_id:
            message = f"{server.get('name')}: {message}" if message else server.get("name")
        self._nx_last_emit[rl_key] = self._emit(
            e["severity"], f"nx-{e['type_key']}", e["title"], message or "",
            camera_id=camera_id, camera_name=camera_name, source="nx", ts=e["ts"],
            nx_type=e["raw_type"], nx_key=key, ip=e.get("ip"))

    def _device_for_ip(self, ip, macs):
        """Several Nx device entries can share one IP (duplicates, manual
        adds) — prefer the one whose MAC matches, then one that's online."""
        norm = lambda m: re.sub(r"[^0-9a-f]", "", str(m or "").lower())
        wanted = {norm(m) for m in macs if norm(m).strip("0")}
        matches = [(cid, d) for cid, d in self.devices.items() if _host_of(d.get("url")) == ip]
        for cid, d in matches:
            if norm(d.get("mac") or d.get("physicalId")) in wanted:
                return cid
        online = [cid for cid, d in matches if d.get("status") in ONLINE_STATUSES]
        return (online or [cid for cid, _ in matches] or [None])[0]

    def _remember_nx_key(self, key):
        if len(self._nx_seen) == self._nx_seen.maxlen:
            self._nx_seen_set.discard(self._nx_seen[0])
        self._nx_seen.append(key)
        self._nx_seen_set.add(key)

    # ------------------------------------------------------------------ #
    # servers & storage
    # ------------------------------------------------------------------ #
    def _poll_servers(self):
        _, info = self._chain("system", [
            ("/rest/v1/system/info", "/rest/v1/system/info", None),
        ], parse=lambda r: r.json() if isinstance(r.json(), dict) else None)
        if info:
            with self._lock:
                self.system = {"name": info.get("name"), "version": info.get("version")}

        _, servers = self._chain("servers", [
            ("/rest/v1/servers", "/rest/v1/servers", None),
        ], parse=lambda r: r.json() if isinstance(r.json(), list) else None)
        if servers is None:
            return

        fresh = {}
        for s in servers:
            sid = _clean_id(s.get("id"))
            if not sid:
                continue
            os_info = s.get("osInfo")
            if isinstance(os_info, dict):
                os_info = " ".join(str(v) for v in (os_info.get("platform"), os_info.get("variant"), os_info.get("variantVersion")) if v)
            fresh[sid] = {
                "id": sid,
                "name": s.get("name"),
                "version": s.get("version"),
                "status": s.get("status"),
                "online": s.get("status") in ("Online", None),
                "os": os_info,
                "host": _host_of(s.get("url")),
            }
        with self._lock:
            previous = self.servers
            self.servers = fresh
        for sid, s in fresh.items():
            before = previous.get(sid)
            if before and before["online"] and not s["online"]:
                self._emit("critical", "server-offline", "Nx server offline", f"{s['name']} reports status '{s['status']}'. Cameras it hosts may stop recording.")
            elif before and not before["online"] and s["online"]:
                self._emit("success", "server-online", "Nx server back online", f"{s['name']} is online again.")

        storages = []
        for sid, s in fresh.items():
            if not s["online"]:
                continue
            label, items = self._chain("storages", [
                ("/rest/v1/servers/{id}/storages/*/status", f"/rest/v1/servers/{sid}/storages/*/status", None),
                ("/rest/v1/servers/{id}/storages", f"/rest/v1/servers/{sid}/storages", None),
                ("/api/storageSpace", "/api/storageSpace", None),
            ], parse=_parse_storages)
            for st in items or []:
                st["server_id"], st["server_name"] = sid, s["name"]
                storages.append(st)
            if label == "/api/storageSpace":
                break  # legacy route only ever describes the server we're talking to
        with self._lock:
            self.storages = storages
        for st in storages:
            key = (st["server_id"], st["url"])
            was = self._storage_online.get(key)
            self._storage_online[key] = st["online"]
            if st["online"] is False and was is not False:
                self._emit("critical", "storage-offline", "Recording storage offline",
                           f"{st['url']} on {st['server_name']} is offline — cameras writing to it can't record.")
            elif st["online"] and was is False:
                self._emit("success", "storage-online", "Recording storage back online", f"{st['url']} on {st['server_name']} is online again.")

    # ------------------------------------------------------------------ #
    # thumbnails (Nx-rendered snapshot; camera_service falls back to RTSP)
    # ------------------------------------------------------------------ #
    def fetch_thumbnail(self, cid, height=270):
        width = int(height * 16 / 9)

        def _parse_image(res):
            ctype = res.headers.get("Content-Type", "")
            return (res.content, ctype) if ctype.startswith("image/") and res.content else None

        _, image = self._chain("thumbnails", [
            ("/rest/v3/devices/{id}/image", f"/rest/v3/devices/{cid}/image", {"size": f"{width}x{height}"}),
            ("/rest/v1/devices/{id}/image", f"/rest/v1/devices/{cid}/image", {"size": f"{width}x{height}"}),
            ("/ec2/cameraThumbnail", "/ec2/cameraThumbnail",
             {"cameraId": cid, "time": "LATEST", "height": height, "imageFormat": "jpg"}),
        ], parse=_parse_image, mark_unsupported=False, timeout=6)
        return image

    # ------------------------------------------------------------------ #
    # events store
    # ------------------------------------------------------------------ #
    def _emit(self, severity, type_, title, message="", camera_id=None, camera_name=None,
              source="visionsync", ts=None, **extra):
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts": ts or _now_ms(),
                "severity": severity,
                "type": type_,
                "title": title,
                "message": message,
                "camera_id": camera_id,
                "camera_name": camera_name,
                "source": source,
            }
            event.update({k: v for k, v in extra.items() if v is not None})
            self.events.append(event)
            snapshot = list(self.events)
            seq = self._seq
        try:
            print(f"[{severity.upper()}] {title}{' — ' + camera_name if camera_name else ''}: {message}")
        except Exception:
            pass  # console can't encode a camera name — never lose the event over it
        self._persist(snapshot, seq)
        return event

    def _persist(self, events, seq):
        with self._persist_lock:
            try:
                tmp = self._events_file + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"seq": seq, "events": events}, f)
                os.replace(tmp, self._events_file)
            except Exception as e:
                print(f"[!] Could not persist camera events: {e}")

    def _load_events(self):
        if not os.path.exists(self._events_file):
            return
        try:
            with open(self._events_file, "r") as f:
                data = json.load(f)
            for e in data.get("events", [])[-self.events.maxlen:]:
                self.events.append(e)
            self._seq = max([int(data.get("seq") or 0)] + [int(e.get("seq") or 0) for e in self.events])
            # So the event-log overlap window never re-announces entries
            # that were already ingested before a restart.
            for e in self.events:
                if e.get("nx_key"):
                    self._remember_nx_key(e["nx_key"])
            nx_times = [e["ts"] for e in self.events if e.get("source") == "nx"]
            if nx_times:
                self._event_log_cursor_ms = max(self._event_log_cursor_ms, max(nx_times) - 60_000)
        except Exception as e:
            print(f"[!] Could not load saved camera events ({e}) — starting with an empty history.")

    def _last_status_event(self, cid):
        with self._lock:
            for e in reversed(self.events):
                if e.get("camera_id") == cid and e.get("source") == "visionsync" and e.get("type") in (
                        "camera-offline", "camera-problem", "camera-online", "camera-removed"):
                    return e["type"]
        return None

    def _disconnect_times(self, cid, since_ms, own_only=False):
        """Real observed drops only — an "already offline when monitoring
        started" alert isn't a disconnect this monitor saw happen."""
        with self._lock:
            return [e["ts"] for e in self.events
                    if e.get("camera_id") == cid and e["ts"] >= since_ms and not e.get("already_down")
                    and (e.get("type") == "camera-offline" or (not own_only and e.get("type") == "nx-disconnect"))]

    def get_events(self, since_seq=0, limit=200, camera_id=None):
        with self._lock:
            events = [e for e in self.events
                      if max(e["seq"], e.get("updated_seq", 0)) > since_seq and (not camera_id or e.get("camera_id") == camera_id)]
            latest = self._seq
        return {"events": events[-limit:], "latest_seq": latest}

    # ------------------------------------------------------------------ #
    # health read models
    # ------------------------------------------------------------------ #
    def _camera_health(self, cid, now_ms):
        d = self.devices[cid]
        params = d.get("parameters") if isinstance(d.get("parameters"), dict) else {}
        options = d.get("options") if isinstance(d.get("options"), dict) else {}
        status = d.get("status") or "NotDefined"
        online = status in ONLINE_STATUSES
        track = self.tracks.get(cid) or {}
        footage = self.footage.get(cid)
        schedule = summarize_schedule(d.get("schedule"))
        streams = parse_streams(params)
        server = self.servers.get(_clean_id(d.get("serverId"))) or {}

        issues = []
        score = 100.0

        def issue(severity, text, penalty=0):
            nonlocal score
            issues.append({"severity": severity, "text": text})
            score -= penalty

        since = track.get("since_ms")
        if not online:
            severity, title, detail = STATUS_PROBLEMS.get(status, ("warning", "Unavailable", f"Nx reports status '{status}'."))
            score = _DOWN_SCORES.get(status, 15)
            issue(severity, f"{title}{' for ' + _fmt_duration(now_ms - since) if since else ''} — {detail}")
        else:
            if not schedule["enabled"]:
                issue("warning", "Recording is off — Nx isn't archiving this camera.", 15)
            elif footage:
                cov = footage["coverage"]
                if schedule["continuous_fraction"] >= 0.95:
                    if cov < 0.5:
                        issue("critical", f"Only {cov * 100:.1f}% of the last 24h was recorded (schedule is continuous).", 35)
                    elif cov < 0.9:
                        issue("warning", f"{cov * 100:.1f}% of the last 24h recorded, {footage['gaps']} gap(s) (schedule is continuous).", 20)
                    elif cov < 0.98:
                        issue("warning", f"{footage['gaps']} recording gap(s) in the last 24h ({cov * 100:.1f}% coverage).", 8)
                    if not footage["recording_now"]:
                        issue("warning", "Not writing to the archive right now, although the schedule is continuous.", 15)
                elif footage["recorded_ms"] == 0:
                    issue("warning", "No recordings at all in the last 24h (motion-triggered schedule).", 10)

            primary = streams[0] if streams else None
            if primary and primary.get("fps") and primary.get("actual_fps") is not None and primary["fps"] > 0:
                ratio = primary["actual_fps"] / primary["fps"]
                if ratio < 0.7:
                    issue("warning", f"Primary stream running at {primary['actual_fps']:.1f} fps (configured {primary['fps']:.0f}).", 15)

        drops_24h = len(self._disconnect_times(cid, now_ms - _DAY_MS))
        if drops_24h:
            issue("warning" if drops_24h < 3 else "critical",
                  f"Disconnected {drops_24h}× in the last 24h.", min(25, drops_24h * 5) if online else 0)

        ip = _host_of(d.get("url"))
        conflicts = sum(e.get("count", 1) for e in self.events
                        if e.get("camera_id") == cid and e.get("type") == "nx-ipconflict"
                        and e.get("last_ts", e["ts"]) >= now_ms - _DAY_MS)
        if conflicts:
            issue("critical" if online else "warning",
                  f"IP address conflict on {ip or 'this camera'} — Nx logged it {conflicts}× in the last 24h.", 15 if online else 0)
        twins = [o.get("name") or oid for oid, o in self.devices.items() if oid != cid and ip and _host_of(o.get("url")) == ip]
        if twins:
            issue("warning", f"{len(twins)} other Nx device {'entry uses' if len(twins) == 1 else 'entries use'} the same IP "
                             f"({', '.join(twins[:3])}) — likely a duplicate; remove the stale one in the Nx Desktop client.")

        tracked = track.get("tracked_s") or 0
        uptime = (track.get("online_s", 0) / tracked) if tracked >= 60 else None
        if online and uptime is not None and tracked >= 600 and uptime < 0.99:
            issue("warning", f"Uptime {uptime * 100:.1f}% while monitored.", 10)

        score = int(max(0, min(100, round(score))))
        if not online:
            grade = "offline" if status == "Offline" else "critical"
        elif score >= 90:
            grade = "healthy"
        elif score >= 70:
            grade = "fair"
        elif score >= 40:
            grade = "degraded"
        else:
            grade = "critical"

        ptz = _num(params.get("ptzCapabilities"), 0) or 0
        return {
            "id": cid,
            "name": d.get("name"),
            "status": status,
            "online": online,
            "status_since_ms": since,
            "score": score,
            "grade": grade,
            "issues": issues,
            "vendor": d.get("vendor"),
            "model": d.get("model"),
            "firmware": params.get("firmware") or d.get("firmware"),
            "ip": _host_of(d.get("url")),
            "mac": d.get("mac") or d.get("physicalId"),
            "group": (d.get("group") or {}).get("name") if isinstance(d.get("group"), dict) else None,
            "server_name": server.get("name"),
            "streams": streams,
            "recording": {
                **schedule,
                "coverage_24h": footage["coverage"] if footage else None,
                "gaps_24h": footage["gaps"] if footage else None,
                "recording_now": footage["recording_now"] if footage else (status == "Recording"),
                "last_recorded_ms": footage["last_recorded_ms"] if footage else None,
            },
            "uptime_pct": round(uptime * 100, 2) if uptime is not None else None,
            "monitored_s": int(tracked),
            "disconnects_24h": drops_24h,
            "capabilities": {
                "audio": _truthy(params.get("isAudioSupported")) or _truthy(options.get("isAudioEnabled")),
                "ptz": ptz > 0,
                "dual_stream": _truthy(params.get("hasDualStreaming")) or _truthy(params.get("hasDualStreaming2")) or len(streams) > 1,
                "io": _truthy(params.get("ioConfigCapability")) or bool(_json_param(params.get("ioSettings"))),
                "license_used": d.get("isLicenseUsed"),
            },
        }

    def fleet_snapshot(self):
        now_ms = _now_ms()
        with self._lock:
            cameras = [self._camera_health(cid, now_ms) for cid in self.devices]
            servers = list(self.servers.values())
            storages = [dict(s) for s in self.storages]
            day_events = [e for e in self.events if e["ts"] >= now_ms - _DAY_MS]
            monitor = {
                "started_at_ms": self.started_at_ms,
                "last_poll_ms": self.last_poll_ms,
                "poll_seconds": self.poll_seconds,
                "footage_seconds": self.footage_seconds,
                "nx_reachable": self.nx_reachable,
                "last_error": self.last_error,
                # Absent = not tried yet; null = this Nx server doesn't offer it.
                "sources": {k: self.sources[k] for k in ("devices", "footage", "event_log", "servers", "storages", "thumbnails") if k in self.sources},
            }
            system = dict(self.system)
        scores = [c["score"] for c in cameras]
        summary = {
            "total": len(cameras),
            "online": sum(1 for c in cameras if c["online"]),
            "offline": sum(1 for c in cameras if not c["online"]),
            "recording": sum(1 for c in cameras if c["recording"]["recording_now"]),
            "needs_attention": sum(1 for c in cameras if c["issues"]),
            "avg_score": round(sum(scores) / len(scores)) if scores else None,
            "critical_24h": sum(1 for e in day_events if e["severity"] == "critical"),
            "warnings_24h": sum(1 for e in day_events if e["severity"] == "warning"),
        }
        if not system.get("version") and servers:
            system["version"] = servers[0].get("version")
        return {"generated_at_ms": now_ms, "monitor": monitor, "system": system, "summary": summary,
                "cameras": cameras, "servers": servers, "storages": storages}

    def camera_details(self, cid):
        now_ms = _now_ms()
        with self._lock:
            d = self.devices.get(cid)
            if not d:
                return None
            health = self._camera_health(cid, now_ms)
            footage = self.footage.get(cid)
            events = [e for e in self.events if e.get("camera_id") == cid][-60:]
        schedule = d.get("schedule") if isinstance(d.get("schedule"), dict) else {}
        health.update({
            "identity": {
                "id": cid,
                "physical_id": d.get("physicalId"),
                "logical_id": d.get("logicalId"),
                "type_id": _clean_id(d.get("typeId")) or None,
                "server_id": _clean_id(d.get("serverId")) or None,
                "url": _strip_url_creds(d.get("url")),
            },
            "timeline": {
                "window_start_ms": footage["window_start_ms"] if footage else now_ms - _DAY_MS,
                "window_end_ms": footage["fetched_at_ms"] if footage else now_ms,
                "periods": footage["periods"] if footage else None,
                "disconnects": [e["ts"] for e in events if e.get("type") in ("camera-offline", "nx-disconnect")],
            },
            "schedule_tasks": _sanitize(schedule.get("tasks") or []),
            "events": list(reversed(events)),
            "options": _sanitize(d.get("options") or {}),
            "parameters": _sanitize({k: (_json_param(v) or v) for k, v in (d.get("parameters") or {}).items()}),
            "raw": _sanitize({k: v for k, v in d.items() if k not in ("parameters", "options", "schedule", "credentials")}),
        })
        return health


# ---------------------------------------------------------------------- #
# response parsers (shared across endpoint generations)
# ---------------------------------------------------------------------- #
def _legacy_ok(body):
    """Legacy /api and /ec2 routes wrap errors as 200 {error: "N", errorString}."""
    return not (isinstance(body, dict) and str(body.get("error", "0")) not in ("0", "", "None"))


def _parse_periods(res):
    body = res.json()
    if not _legacy_ok(body):
        return None
    if body in ([], {}):
        return []
    items = _walk_dicts(body, lambda o: "startTimeMs" in o)
    if not items and not (isinstance(body, list) or "reply" in (body if isinstance(body, dict) else {})):
        return None
    periods = []
    for p in items:
        # REST v3+ omits durationMs entirely on the chunk that's still being
        # recorded (legacy routes send -1) — both mean "ongoing".
        start, dur = _num(p.get("startTimeMs")), _num(p.get("durationMs"), -1)
        if start is not None:
            periods.append((int(start), int(dur)))
    return periods


def _parse_event_list(res):
    body = res.json()
    if not _legacy_ok(body):
        return None
    if isinstance(body, dict):
        body = body.get("reply", body.get("events", body.get("data")))
    return body if isinstance(body, list) else None


def _parse_storages(res):
    body = res.json()
    if not _legacy_ok(body):
        return None
    items = _walk_dicts(body, lambda o: isinstance(o, dict) and ("url" in o or "path" in o) and (
        "totalSpace" in o or "freeSpace" in o or "isOnline" in o or "spaceLimitB" in o or "status" in o))
    if not items:
        return None
    out = []
    for s in items:
        online = s.get("isOnline")
        if online is None and s.get("status"):
            online = str(s.get("status")).lower() not in ("offline", "failed", "notmounted")
        out.append({
            "url": _strip_url_creds(s.get("url") or s.get("path") or s.get("name")),
            "type": s.get("storageType") or s.get("type"),
            "total_bytes": _num(s.get("totalSpace") or s.get("totalSpaceB")),
            "free_bytes": _num(s.get("freeSpace") or s.get("freeSpaceB")),
            "reserved_bytes": _num(s.get("reservedSpace") or s.get("spaceLimitB")),
            "online": None if online is None else _truthy(online),
            "used_for_writing": _truthy(s.get("isUsedForWriting")) if s.get("isUsedForWriting") is not None else None,
            "backup": _truthy(s.get("isBackup")) if s.get("isBackup") is not None else None,
        })
    return out


def _first(*values):
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


def _parse_nx_event(raw):
    """Normalizes one Nx event-log record across generations (legacy
    {eventParams: {eventType, eventTimestampUsec, eventResourceId, ...}},
    v4 {eventData: {type, deviceId, ...}, timestampMs}, ...) into
    {ts, type_key, severity, title, message, resource, ...} — or None when
    it isn't a health-relevant type (see _NX_EVENT_TYPES)."""
    if not isinstance(raw, dict):
        return None
    ev = raw.get("eventParams") or raw.get("eventData") or raw.get("event") or raw
    if not isinstance(ev, dict):
        return None
    raw_type = _first(ev.get("eventType"), ev.get("type"), raw.get("eventType"), raw.get("type")) or ""
    t = _norm(raw_type)
    match = next(((keys, sev, title) for keys, sev, title in _NX_EVENT_TYPES if any(k in t for k in keys)), None)
    if not match:
        return None
    keys, severity, title = match
    ts = _to_ms(_first(ev.get("eventTimestampUsec"), ev.get("timestampUs"), ev.get("timestampMs"), ev.get("timestamp"),
                       raw.get("timestampMs"), raw.get("timestampUs"), raw.get("timestamp")))
    if not ts:
        return None
    metadata = ev.get("metadata") if isinstance(ev.get("metadata"), dict) else {}
    refs = list(metadata.get("cameraRefs") or []) + list(ev.get("deviceIds") or [])
    # Device ids before the server id; Nx pads unknown devices with the
    # all-zero UUID (e.g. both sides of an IP conflict), which isn't a resource.
    candidates = [ev.get("eventResourceId"), ev.get("deviceId"), ev.get("cameraId"), ev.get("sourceId"),
                  *refs, ev.get("serverId"), ev.get("sourceServerId")]
    resource = next((c for c in (_clean_id(x) for x in candidates) if c and c.strip("0-")), "")

    type_key = keys[0].replace("camera", "").replace("device", "") or keys[0]
    ip = ev.get("ipAddress") or (ev.get("caption") if type_key == "ipconflict" else None)
    macs = [m for m in (ev.get("macAddresses") or []) if m]
    reason = _first(ev.get("reasonCode"), ev.get("reason"))
    reason_text = _NX_REASON_TEXT.get(_norm(reason)) or (_humanize(reason) if reason and _norm(reason) not in ("none", "0") else None)
    if type_key == "ipconflict" and ip:
        message = f"{ip} is claimed by more than one device" + (f" (MACs {', '.join(macs)})" if macs else "") + \
                  " — often a duplicate or manually-added entry for the same camera in Nx."
    else:
        stream = (ev.get("info") or {}).get("stream") if isinstance(ev.get("info"), dict) else None
        message = " — ".join(str(x) for x in (reason_text, f"{stream} stream" if stream else None,
                                               _first(ev.get("caption")), _first(ev.get("description"))) if x)
    return {
        "ts": ts,
        "type_key": type_key,
        "ip": ip,
        "macs": macs,
        "raw_type": raw_type,
        "severity": severity,
        "title": title,
        "message": message,
        "resource": resource,
        "resource_name": _first(ev.get("resourceName"), ev.get("deviceName"), ev.get("sourceName")),
    }


def _fmt_duration(ms):
    s = max(0, int(ms / 1000))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s" if s and m < 10 else f"{m}m"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{h // 24}d {h % 24}h"
