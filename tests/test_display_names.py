"""Camera display names (PLATFORM_GUIDE.md step 6): set here, shown in the
camera list and read by the other modules. No camera server needed: the
camera list is faked, settings go to a temporary file.

    venv/bin/python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

for k, v in {"NX_SERVER_URL": "https://127.0.0.1:9", "NX_USERNAME": "test", "NX_PASSWORD": "test",
             "NX_RTSP_HOST": "127.0.0.1"}.items():
    os.environ[k] = v          # set before import: the real .env never fills them
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import camera_service as cs  # noqa: E402

CAM = "b713a654-3347-4beb-a773-f0187b8c3d05"
TOKEN = "test-token"


class DisplayNames(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cs.SETTINGS_FILE = os.path.join(self.tmp, "camera_settings.json")
        cs.SETTINGS.clear()
        cs.SETTINGS.update({**cs.DEFAULT_SETTINGS, "DISPLAY_NAMES": {}, "EXTRA_CAMERAS": []})
        cs.load_auth_token = lambda: TOKEN
        cs._camera_cache["data"] = cs._shape_devices([{"id": "{" + CAM + "}", "name": "IPC-HFW1230DT", "status": "Online"}])
        cs._camera_cache["fetched_at"] = cs.time.time()
        self.c = cs.app.test_client()
        self.h = {"Authorization": f"Bearer {TOKEN}"}

    def test_name_set_shown_and_cleared(self):
        self.assertEqual(self.c.get("/api/cameras", headers=self.h).json[CAM]["display_name"], "IPC-HFW1230DT")
        r = self.c.put(f"/api/cameras/{CAM}/display-name", json={"display_name": "  Vault   door "}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["display_name"], "Vault door")
        cams = self.c.get("/api/cameras", headers=self.h).json
        self.assertEqual(cams[CAM]["display_name"], "Vault door")
        self.assertEqual(cams[CAM]["name"], "IPC-HFW1230DT", "the Nx name stays as it is")
        self.assertEqual(self.c.get(f"/api/cameras/{CAM}", headers=self.h).json["display_name"], "Vault door")
        self.assertEqual(self.c.get("/api/display-names", headers=self.h).json, {CAM: "Vault door"})
        self.assertEqual(cs.json.load(open(cs.SETTINGS_FILE))["DISPLAY_NAMES"], {CAM: "Vault door"}, "saved to the settings file")
        self.c.put(f"/api/cameras/{CAM}/display-name", json={"display_name": ""}, headers=self.h)
        self.assertEqual(self.c.get("/api/display-names", headers=self.h).json, {})
        self.assertEqual(self.c.get("/api/cameras", headers=self.h).json[CAM]["display_name"], "IPC-HFW1230DT")

    def test_limits_and_access(self):
        r = self.c.put(f"/api/cameras/{CAM}/display-name", json={"display_name": "x" * 200}, headers=self.h)
        self.assertEqual(len(r.json["display_name"]), cs.DISPLAY_NAME_MAX)
        self.assertEqual(self.c.put("/api/cameras/not!an!id/display-name", json={"display_name": "a"}, headers=self.h).status_code, 400)
        self.assertEqual(self.c.get("/api/display-names").status_code, 401, "needs this box's token, like every /api route")
        self.assertEqual(self.c.put(f"/api/cameras/{CAM}/display-name", json={"display_name": "a"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
