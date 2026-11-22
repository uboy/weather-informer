#!/usr/bin/env python3
"""
Pull-сигналинг WebRTC (D4: сервер сам подключается к планшету) на caching_server.py:
- relay: POST /api/cam/webrtc/offer релеит offer на планшет (fake tablet HTTP
  server) и возвращает answer+ICE одним ответом + viewer id
- tablet_token не уходит дашборду: он подставляется сервером в запрос к планшету
- auth: без Basic -> 401, неверный пароль -> 401
- tablet 404 (старый APK) и 403 disabled пробрасываются как есть;
  недоступный планшет -> 502 tablet unreachable
- close: POST /api/cam/webrtc/close доходит до планшета; unknown room -> 400

Каждый негативный кейс проверяет свой статус, а не «какую-то ошибку».
"""

import os
import sys
import json
import base64
import http.client
import threading
import tempfile
import shutil
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)

import caching_server

DASH_USER = "dash-test"
DASH_PASS = "pw-test-123"
TABLET_TOKEN = "tt-pull-secret"
ROOM = "gostinaya"

ANSWER = {"type": "answer", "sdp": "v=0-answer-sdp",
          "candidates": [{"candidate": "candidate:1", "sdpMid": "0", "sdpMLineIndex": 0}]}
OFFER_BODY = {"type": "offer", "sdp": "v=0-offer-sdp", "audio": True}


def basic_header(user, pwd):
    raw = ("%s:%s" % (user, pwd)).encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


class FakeTablet(BaseHTTPRequestHandler):
    """Планшет-заглушка: пишет запросы в class-level журнал, отвечает по mode."""
    mode = "ok"  # ok | not_found | disabled | slow
    requests = []
    server = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        FakeTablet.requests.append({"path": self.path, "body": body.decode("utf-8")})
        if self.path.startswith("/api/cam/webrtc/offer"):
            if FakeTablet.mode == "not_found":
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"File not found")
                return
            if FakeTablet.mode == "disabled":
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"disabled"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(ANSWER).encode("utf-8"))
            return
        if self.path.startswith("/api/cam/webrtc/close"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"closed"}')
            return
        self.send_response(404)
        self.end_headers()


class TestCamPull(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp()
        cls.config_path = os.path.join(cls.test_dir, "config.json")
        cls.orig_config_file = caching_server.CONFIG_FILE
        caching_server.CONFIG_FILE = cls.config_path
        cls.tablet = ThreadingHTTPServer(("127.0.0.1", 0), FakeTablet)
        cls.tablet_port = cls.tablet.server_address[1]
        threading.Thread(target=cls.tablet.serve_forever, daemon=True).start()
        cls.write_cam_config()
        cls.server = caching_server.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), caching_server.WeatherHTTPHandler
        )
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
            cls.tablet.shutdown()
            cls.tablet.server_close()
        except Exception:
            pass
        caching_server.CONFIG_FILE = cls.orig_config_file
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    @classmethod
    def write_cam_config(cls):
        cfg = {
            "port": 0,
            "city_name": "Тест",
            "lat": 56.31,
            "lon": 44.00,
            "cameras": {
                "enabled": True,
                "dashboard_user": DASH_USER,
                "dashboard_password": DASH_PASS,
                "viewer_token": "vt-test",
                "rooms": {
                    ROOM: {"tablet_token": TABLET_TOKEN, "host": "127.0.0.1",
                           "port": cls.tablet_port},
                },
            },
        }
        with open(cls.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

    def post(self, path, body, headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def offer(self, headers, body=None, room=ROOM):
        payload = json.dumps(body if body is not None else OFFER_BODY).encode("utf-8")
        return self.post("/api/cam/webrtc/offer?room=%s" % room, payload, headers)

    # ===== Позитив: релей и токен =====

    def test_offer_relay_returns_answer_viewer_and_token_stays_server_side(self):
        FakeTablet.mode = "ok"
        FakeTablet.requests = []
        status, data = self.offer(basic_header(DASH_USER, DASH_PASS))
        self.assertEqual(status, 200, "positive case must be 200")
        got = json.loads(data.decode("utf-8"))
        self.assertEqual(got.get("type"), "answer")
        self.assertEqual(got.get("sdp"), ANSWER["sdp"])
        self.assertEqual(got.get("candidates"), ANSWER["candidates"])
        self.assertTrue(str(got.get("viewer", "")).startswith("pull-"),
                        "server must assign pull- viewer id")
        self.assertEqual(len(FakeTablet.requests), 1, "exactly one tablet call")
        req = FakeTablet.requests[0]
        self.assertIn("token=%s" % TABLET_TOKEN, req["path"],
                      "tablet_token must go to tablet, not to dashboard")
        self.assertIn("viewer=pull-", req["path"])
        self.assertIn("audio=1", req["path"])
        sent = json.loads(req["body"])
        self.assertEqual(sent.get("type"), "offer")
        self.assertEqual(sent.get("sdp"), OFFER_BODY["sdp"])

    def test_offer_audio_absent_means_audio_0(self):
        FakeTablet.mode = "ok"
        FakeTablet.requests = []
        body = dict(OFFER_BODY)
        del body["audio"]
        status, data = self.offer(basic_header(DASH_USER, DASH_PASS), body=body)
        self.assertEqual(status, 200)
        self.assertIn("audio=0", FakeTablet.requests[0]["path"])

    # ===== Негативы, каждый по своей причине =====

    def test_offer_no_auth_401(self):
        status, _ = self.offer({})
        self.assertEqual(status, 401, "missing Basic must be 401")

    def test_offer_bad_password_401(self):
        status, _ = self.offer(basic_header(DASH_USER, "wrong-pw"))
        self.assertEqual(status, 401, "wrong password must be 401")

    def test_offer_unknown_room_400(self):
        status, _ = self.offer(basic_header(DASH_USER, DASH_PASS), room="netu")
        self.assertEqual(status, 400, "unknown room must be 400")

    def test_offer_bad_body_400_no_sdp(self):
        status, _ = self.offer(basic_header(DASH_USER, DASH_PASS), body={"type": "offer"})
        self.assertEqual(status, 400, "offer without sdp must be 400")

    def test_offer_bad_json_400(self):
        status, _ = self.post("/api/cam/webrtc/offer?room=%s" % ROOM,
                              b"{broken", basic_header(DASH_USER, DASH_PASS))
        self.assertEqual(status, 400, "broken JSON must be 400")

    def test_offer_tablet_404_passthrough_old_apk(self):
        FakeTablet.mode = "not_found"
        status, _ = self.offer(basic_header(DASH_USER, DASH_PASS))
        self.assertEqual(status, 404, "old APK (404) must pass through for dashboard fallback")
        FakeTablet.mode = "ok"

    def test_offer_tablet_disabled_403_passthrough(self):
        FakeTablet.mode = "disabled"
        status, data = self.offer(basic_header(DASH_USER, DASH_PASS))
        self.assertEqual(status, 403, "tablet 403 disabled must pass through")
        self.assertIn(b"disabled", data)
        FakeTablet.mode = "ok"

    def test_offer_tablet_unreachable_502(self):
        # комната с неправильным портом: соединение не установится
        cfg_path = self.config_path
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["cameras"]["rooms"]["dead"] = {"tablet_token": "tt-dead",
                                           "host": "127.0.0.1", "port": 1}
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        status, data = self.offer(basic_header(DASH_USER, DASH_PASS), room="dead")
        self.assertEqual(status, 502, "unreachable tablet must be 502")
        self.assertIn(b"tablet unreachable", data)
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["cameras"]["rooms"].pop("dead", None)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

    # ===== Close =====

    def test_close_reaches_tablet_and_returns_closed(self):
        FakeTablet.requests = []
        payload = json.dumps({"viewer": "pull-abc"}).encode("utf-8")
        status, data = self.post("/api/cam/webrtc/close?room=%s" % ROOM, payload,
                                 basic_header(DASH_USER, DASH_PASS))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data.decode("utf-8")).get("status"), "closed")
        paths = [r["path"] for r in FakeTablet.requests]
        self.assertTrue(any("/api/cam/webrtc/close" in p and "viewer=pull-abc" in p
                            for p in paths), "close must reach tablet with viewer id")

    def test_close_unknown_room_400(self):
        status, _ = self.post("/api/cam/webrtc/close?room=netu", b'{"viewer":"v"}',
                              basic_header(DASH_USER, DASH_PASS))
        self.assertEqual(status, 400, "unknown room must be 400")

    def test_close_no_auth_401(self):
        status, _ = self.post("/api/cam/webrtc/close?room=%s" % ROOM, b'{"viewer":"v"}', {})
        self.assertEqual(status, 401, "missing Basic must be 401")


if __name__ == "__main__":
    unittest.main(verbosity=2)
