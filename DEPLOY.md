# VisionSync — Deployment Guide

What to do after `git clone`, on a fresh device or a new project, to get
VisionSync running and pointed at the right Nx Witness server.

---

## 1. Prerequisites

- **Python 3.9+** on the target machine (Windows, since the `Start_*.bat`
  scripts are Windows batch files — the service itself runs fine on
  Linux/macOS too, you'd just launch it with `python camera_service.py`
  directly instead).
- Network access from this machine to the Nx Witness server (cloud relay
  URL, or LAN IP) **and** to its RTSP relay port.
- The Nx Witness Server credential (username/password) for the site this
  install is for. This is the **one** credential the whole platform needs —
  never individual camera passwords.

## 2. Clone and install dependencies

```bash
git clone https://github.com/kurtramos/VisionSync.git
cd VisionSync
pip install -r requirements.txt
```

## 3. Configure `.env`

`.env` is gitignored on purpose — it never comes from the repo, you create
it locally on every machine this is deployed to.

```bash
cp .env.example .env
```

Then edit `.env` and fill in the real values for **this** site:

| Variable | What it is |
|---|---|
| `NX_SERVER_URL` | The Nx Witness Server's API base URL. Cloud relay: `https://<system-id>.relay.vmsproxy.com`. LAN/local server: `http://<host>:<port>`. |
| `NX_USERNAME` / `NX_PASSWORD` | The one Nx Witness Server login for this site. |
| `NX_RTSP_HOST` / `NX_RTSP_PORT` | Nx's own RTSP relay host/port — **not** a camera's IP. This is usually the Nx Server's LAN IP and its RTSP port (default `7001`). |
| `PORT` | Which port `camera_service.py` listens on. Defaults to `5010` — only change this if something else on the box already uses it. |

**Where to find these for a new site:** open Nx Witness Desktop Client →
Server Settings for the values above, or ask whoever administers that
site's Nx server. Don't guess or reuse another site's `.env`.

## 4. First run — verify it can actually reach Nx

```bash
python camera_service.py
```

Watch the console for errors, then in another terminal (or a browser):

```bash
curl http://127.0.0.1:5010/api/health
# {"status":"ok","nx_reachable":true}   <- nx_reachable must be true
```

If `nx_reachable` is `false`:
- Double-check `NX_SERVER_URL`, `NX_USERNAME`, `NX_PASSWORD` in `.env`.
- Confirm this machine can actually reach that URL (`curl`/browser to
  `NX_SERVER_URL` directly, VPN if the Nx server is only reachable that way).
- Check the Nx account isn't locked out from too many failed logins.

Then confirm cameras resolve:

```bash
curl http://127.0.0.1:5010/api/cameras
```

Should return a JSON object keyed by camera ID. An empty `{}` with
`nx_reachable: true` usually means the credential logged in fine but has no
camera permissions on that server — check the account's role in Nx.

Stop this test run (`Ctrl+C`) once confirmed.

## 5. Running it for real

**Windows, day-to-day use:**
- Double-click `Start_VisionSync_System.bat` — runs the service invisibly
  (`pythonw`, no console window) and opens the config page automatically.
- Use `Start_Debug_VisionSync_System.bat` instead if something's wrong and
  you need to see the service's console output/errors.

**Linux/macOS, or running as a background service:**
```bash
python camera_service.py
```
Wrap this in whatever your platform uses to keep a process alive and
restart it on crash/reboot — systemd unit, pm2, supervisor, a Docker
container, etc. There's no bundled equivalent to the `.bat` scripts for
non-Windows yet.

**Auto-start on Windows boot/login** (optional): place a shortcut to
`Start_VisionSync_System.bat` in
`shell:startup` (Win+R → type `shell:startup` → Enter), or wire it up as a
Scheduled Task set to run at log-on if you need it running before anyone
logs in.

## 6. Using VisionSync from another project

Other modules/projects should talk to VisionSync's HTTP API — never to Nx
Witness directly, and never construct RTSP URLs themselves. See
`README.md` for the full endpoint list. In short:

1. `GET http://<this-box>:5010/api/settings` — which camera IDs are
   currently assigned to `DWELL_CAMERA_ID` / `POS_CAMERA_ID`, plus any
   `EXTRA_CAMERAS` slots.
2. `GET http://<this-box>:5010/api/cameras` — full camera list with
   online/offline status, to build your own picker or health check.
3. `GET http://<this-box>:5010/api/stream?camera_id=<id>` — an MJPEG
   `<img src="...">` you can drop straight into any web page.
4. Need more camera views than the built-in two? `POST
   /api/camera-slots` from your own code, or just use VisionSync's own
   **+ Add Camera** button in the UI — either way, other projects can then
   read those extra slots back from `/api/settings`.

If the consuming project runs on a **different machine** than VisionSync,
make sure port `5010` (or whatever `PORT` you set) is reachable from it —
firewall rule, same LAN/VPN, etc. CORS is already open (`origins: "*"`) so
browser-side cross-origin requests aren't the blocker; network reachability
is.

## 7. Known limitations / troubleshooting

- **Live view doesn't show, or seems to hang when switching cameras:** this
  was a real bug (Flask's dev server defaulting to single-threaded, unable
  to hold more than one open MJPEG stream at a time) — fixed by running
  with `threaded=True`. If you still see it after pulling the latest code,
  confirm you're actually running the updated `camera_service.py` (check
  for `threaded=True` in its `app.run(...)` call near the bottom of the
  file) and that you restarted the service after updating.
- **Flask's built-in server is not a production server** (it says so on
  startup). It's fine for a single branch box serving a handful of camera
  views. If a deployment needs to serve many concurrent MJPEG streams
  reliably, put a real WSGI server in front (e.g. `waitress` on Windows,
  `gunicorn --worker-class gthread` on Linux) instead of running
  `camera_service.py` directly.
- **Port already in use:** another process (maybe a previous
  `camera_service.py` that didn't shut down cleanly) is holding port 5010.
  Windows: `netstat -ano | findstr :5010` to find the PID, then
  `taskkill /PID <pid> /F` — or just use the UI's Terminate System button
  next time instead of closing the console window directly.
- **`camera_settings.json` is gitignored on purpose** — it's per-machine
  state (which cameras are picked on *this* box), not something to commit
  or copy between sites.
- **Never commit `.env`.** If Nx credentials ever do end up in git history,
  rotate that Nx account's password immediately — treat it as compromised.
