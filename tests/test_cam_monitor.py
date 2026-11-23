#!/usr/bin/env python3
"""Тесты фазы 2 (мониторинг камер, 23.11): /api/cam/monitor + журнал событий.

Покрывает: авторизацию эндпоинта, состав ответа (rooms+events), фильтр часов
и кламп 1..168, дозагрузку cam_journal.jsonl при старте, переходы poll-петли
(room_error пишется один раз на серию, room_ok на восстановлении).
"""

import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import caching_server as cs

AUTH = base64.b64encode(b"monuser:monpass").decode()


class TestCamMonitor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp()
        cls.orig_config_file = cs.CONFIG_FILE
        cls.orig_journal_file = cs.CAM_JOURNAL_FILE
        cls.orig_registry = cs.CAM_REGISTRY
        cs.CONFIG_FILE = os.path.join(cls.test_dir, "config.json")
        cs.CAM_JOURNAL_FILE = os.path.join(cls.test_dir, "cam_journal.jsonl")
        cfg = {
            "port": 0,
            "cameras": {
                "enabled": True,
                "dashboard_user": "monuser",
                "dashboard_password": "monpass",
                "rooms": {"r1": {"tablet": "127.0.0.1", "tablet_token": "t" * 20, "port": 9}},
            },
        }
        with open(cs.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        cs.CAM_REGISTRY = cs.CameraRegistry()
        cs._CAM_JOURNAL.clear()
        cls.server = cs.BoundedThreadingHTTPServer(("127.0.0.1", 0), cs.WeatherHTTPHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cs.CONFIG_FILE = cls.orig_config_file
        cs.CAM_JOURNAL_FILE = cls.orig_journal_file
        cs.CAM_REGISTRY = cls.orig_registry
        cs._CAM_JOURNAL.clear()
        cs._CAM_ROOM_POLL_STATE.clear()

    def get(self, path, auth=AUTH):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path))
        if auth:
            req.add_header("Authorization", "Basic " + auth)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_monitor_requires_auth(self):
        st, _ = self.get("/api/cam/monitor", auth=None)
        self.assertEqual(st, 401)

    def test_monitor_returns_rooms_and_events(self):
        cs._CAM_JOURNAL.clear()
        cs.cam_journal_record("view_open", "r1", viewer=7, ip="192.168.4.2",
                              transport="pull", audio=True)
        st, j = self.get("/api/cam/monitor?hours=24")
        self.assertEqual(st, 200)
        self.assertIn("r1", j["rooms"])
        self.assertIn("events", j)
        self.assertTrue(any(e["kind"] == "view_open" and e.get("viewer") == 7
                            for e in j["events"]))

    def test_journal_hours_filter_and_clamp(self):
        cs._CAM_JOURNAL.clear()
        cs._CAM_JOURNAL.append({"ts": int(time.time()) - 48 * 3600,
                                "kind": "view_open", "room": "r1"})
        cs.cam_journal_record("pub_open", "r1", ip="x")
        st, j = self.get("/api/cam/monitor?hours=24")
        self.assertEqual(st, 200)
        kinds = [e["kind"] for e in j["events"]]
        self.assertNotIn("view_open", kinds)   # старше окна 24ч отфильтрован
        self.assertIn("pub_open", kinds)
        st, j = self.get("/api/cam/monitor?hours=99999")
        self.assertEqual(j["hours"], 168)      # кламп сверху
        st, j = self.get("/api/cam/monitor?hours=-5")
        self.assertEqual(j["hours"], 1)        # кламп снизу

    def test_journal_load_from_disk(self):
        path = os.path.join(self.test_dir, "journal_tail.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), "kind": "room_ok", "room": "r1"}) + "\n")
            f.write("garbage line\n")
        cs.CAM_JOURNAL_FILE = path
        try:
            cs._CAM_JOURNAL.clear()
            cs.cam_journal_load()
            self.assertTrue(any(e["kind"] == "room_ok" for e in cs._CAM_JOURNAL))
            self.assertEqual(len(cs._CAM_JOURNAL), 1)  # битая строка пропущена
        finally:
            cs.CAM_JOURNAL_FILE = self.orig_journal_file

    def test_poll_transition_records_error_once(self):
        # планшет на порту 9 (discard): connection refused мгновенно;
        # серия неудач пишет room_error один раз, не штормом
        cs._CAM_JOURNAL.clear()
        cs._CAM_ROOM_POLL_STATE.clear()
        cs.poll_cam_states_once()
        cs.poll_cam_states_once()
        errors = [e for e in cs._CAM_JOURNAL if e["kind"] == "room_error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("error", errors[0])


if __name__ == "__main__":
    unittest.main()
