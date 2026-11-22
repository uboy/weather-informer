#!/usr/bin/env python3
"""
Integration tests for Phase 1 cameras signaling on caching_server.py:
- /ws/cam RFC6455 framing: unmasked client frame -> close 1002
- hello auth: bad tablet/viewer token -> 4001, unknown room -> 4002,
  cameras disabled -> 4005, second publisher -> 4003 publisher busy
- routing: publisher cam_state -> viewer, viewer offer -> publisher,
  offer while camera disabled -> viewer error camera_disabled
- dashboard HTTP Basic: no auth -> 401, bad password -> 401, good -> 200;
  viewer-token endpoint returns viewer_token only to authorized

Every negative case asserts its own close code / status, not just "failure".
"""

import os
import sys
import json
import time
import socket
import struct
import base64
import http.client
import threading
import tempfile
import shutil
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)

import caching_server

DASH_USER = "dash-test"
DASH_PASS = "pw-test-123"
VIEWER_TOKEN = "vt-test-456"
TABLET_TOKEN = "tt-abc-def"
ROOM = "gostinaya"


def basic_header(user, pwd):
    raw = ("%s:%s" % (user, pwd)).encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def ws_client_frame(opcode, payload, mask=True):
    """Client->server frame; RFC6455 requires masking, mask=False builds the violation."""
    b = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        b.append((0x80 if mask else 0) | n)
    elif n < 65536:
        b.append((0x80 if mask else 0) | 126)
        b += struct.pack(">H", n)
    else:
        b.append((0x80 if mask else 0) | 127)
        b += struct.pack(">Q", n)
    if mask:
        m = os.urandom(4)
        b += m
        b += bytes(payload[i] ^ m[i & 3] for i in range(n))
    else:
        b += payload
    return bytes(b)


def ws_read_frame(sock):
    """Server->client frame (no mask). Returns (opcode, payload). Pings auto-ponged."""
    while True:
        hdr = sock.recv(2)
        if len(hdr) < 2:
            raise ConnectionError("short header")
        opcode = hdr[0] & 0x0F
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", sock.recv(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", sock.recv(8))[0]
        payload = b""
        while len(payload) < length:
            chunk = sock.recv(length - len(payload))
            if not chunk:
                raise ConnectionError("short payload")
            payload += chunk
        if opcode == 0x9:  # ping -> pong
            sock.sendall(ws_client_frame(0xA, payload))
            continue
        return opcode, payload


def ws_connect(port, path="/ws/cam"):
    """Handshake; returns raw socket ready for frames (or raises on non-101)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (
        "GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
        % (path, port, key)
    )
    sock.sendall(req.encode("ascii"))
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("handshake eof")
        resp += chunk
    head, _, rest = resp.partition(b"\r\n\r\n")
    if b" 101 " not in head.split(b"\r\n")[0]:
        raise AssertionError("handshake not 101: %r" % head.split(b"\r\n")[0])
    if rest:
        # server must not send frames before hello; stash is unused in tests
        pass
    sock.settimeout(5)
    return sock


def ws_close_code(sock):
    """Read the next frame expecting CLOSE; return its code (or None if EOF)."""
    try:
        opcode, payload = ws_read_frame(sock)
    except (ConnectionError, socket.timeout, OSError):
        return None
    assert opcode == 0x8, "expected close frame, got opcode %r" % opcode
    if len(payload) >= 2:
        return struct.unpack(">H", payload[:2])[0]
    return 1005


def ws_hello(sock, hello_dict, mask=True):
    sock.sendall(ws_client_frame(0x1, json.dumps(hello_dict).encode("utf-8"), mask=mask))


def ws_recv_json(sock):
    opcode, payload = ws_read_frame(sock)
    assert opcode == 0x1, "expected text frame, got opcode %r" % opcode
    return json.loads(payload.decode("utf-8"))


class TestCamWs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp()
        cls.config_path = os.path.join(cls.test_dir, "config.json")
        cls.orig_config_file = caching_server.CONFIG_FILE
        caching_server.CONFIG_FILE = cls.config_path
        cls.write_cam_config(enabled=True)
        cls.server = caching_server.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), caching_server.WeatherHTTPHandler
        )
        cls.port = cls.server.server_address[1]
        t = threading.Thread(target=cls.server.serve_forever)
        t.daemon = True
        t.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:
            pass
        caching_server.CONFIG_FILE = cls.orig_config_file
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    @classmethod
    def write_cam_config(cls, enabled):
        cfg = {
            "port": 0,
            "city_name": "Тест",
            "lat": 56.31,
            "lon": 44.00,
            "cameras": {
                "enabled": enabled,
                "dashboard_user": DASH_USER,
                "dashboard_password": DASH_PASS,
                "viewer_token": VIEWER_TOKEN,
                "rooms": {
                    ROOM: {"tablet_token": TABLET_TOKEN, "host": "127.0.0.1", "port": 8080},
                    "kabinet": {"tablet_token": "tt-kab", "host": "127.0.0.1", "port": 8081},
                },
            },
        }
        with open(cls.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)


    # ---- tolerant handshake (\r\r\n legacy build) ----

    def test_cr_cr_lf_handshake_publisher_hello_ok(self):
        """Byte-dump 22.11.2026 (.13): handshake lines end \r\r\n; server must
        still 101 and answer hello_ok to a valid publisher hello."""
        sock = ws_connect_cr_cr_lf(self.port)
        try:
            ws_hello(sock, {"role": "publisher", "room": ROOM, "token": TABLET_TOKEN})
            resp = ws_recv_json(sock)
            self.assertEqual(resp.get("type"), "hello_ok",
                             "publisher with CRLF-CR handshake must get hello_ok, got %r" % resp)
            self.assertEqual(resp.get("room"), ROOM)
        finally:
            sock.close()

    def test_cr_cr_lf_handshake_viewer_gets_cam_state(self):
        """Viewer over CRLF-CR handshake reaches hello + cam_state snapshot path."""
        pub = ws_connect_cr_cr_lf(self.port)
        view = ws_connect_cr_cr_lf(self.port)
        try:
            ws_hello(pub, {"role": "publisher", "room": ROOM, "token": TABLET_TOKEN})
            self.assertEqual(ws_recv_json(pub).get("type"), "hello_ok")
            ws_hello(view, {"role": "viewer", "room": ROOM, "token": VIEWER_TOKEN})
            self.assertEqual(ws_recv_json(view).get("type"), "hello_ok")
            snap = ws_recv_json(view)
            self.assertEqual(snap.get("type"), "cam_state",
                             "viewer must get cam_state snapshot, got %r" % snap)
        finally:
            pub.close()
            view.close()

    # ---- framing ----

    def test_unmasked_client_frame_closed_1002(self):
        sock = ws_connect(self.port)
        try:
            # First frame unmasked (protocol violation, RFC6455 5.1)
            sock.sendall(ws_client_frame(0x1, b'{"type":"hello"}', mask=False))
            self.assertEqual(ws_close_code(sock), 1002,
                             "unmasked client frame must close with 1002")
        finally:
            sock.close()

    # ---- hello auth ----

    def test_bad_tablet_token_4001(self):
        sock = ws_connect(self.port)
        try:
            ws_hello(sock, {"type": "hello", "role": "publisher", "room": ROOM,
                            "token": "wrong-token"})
            self.assertEqual(ws_close_code(sock), 4001,
                             "wrong tablet token must close with 4001")
        finally:
            sock.close()

    def test_bad_viewer_token_4001(self):
        sock = ws_connect(self.port)
        try:
            ws_hello(sock, {"type": "hello", "role": "viewer", "room": ROOM,
                            "token": "nope"})
            self.assertEqual(ws_close_code(sock), 4001,
                             "wrong viewer token must close with 4001")
        finally:
            sock.close()

    def test_unknown_room_4002(self):
        sock = ws_connect(self.port)
        try:
            ws_hello(sock, {"type": "hello", "role": "viewer", "room": "no-such-room",
                            "token": VIEWER_TOKEN})
            self.assertEqual(ws_close_code(sock), 4002,
                             "unknown room must close with 4002")
        finally:
            sock.close()

    def test_cameras_disabled_4005(self):
        try:
            self.write_cam_config(enabled=False)
            sock = ws_connect(self.port)
            try:
                ws_hello(sock, {"type": "hello", "role": "viewer", "room": ROOM,
                                "token": VIEWER_TOKEN})
                self.assertEqual(ws_close_code(sock), 4005,
                                 "cameras disabled must close with 4005")
            finally:
                sock.close()
        finally:
            self.write_cam_config(enabled=True)

    def test_second_publisher_busy_4003_then_free(self):
        a = ws_connect(self.port)
        try:
            ws_hello(a, {"type": "hello", "role": "publisher", "room": ROOM,
                         "token": TABLET_TOKEN})
            msg = ws_recv_json(a)
            self.assertEqual(msg.get("type"), "hello_ok", "first publisher must pass")
            b = ws_connect(self.port)
            try:
                ws_hello(b, {"type": "hello", "role": "publisher", "room": ROOM,
                             "token": TABLET_TOKEN})
                self.assertEqual(ws_close_code(b), 4003,
                                 "second publisher same room must close with 4003")
            finally:
                b.close()
            # free the room, then a new publisher must be accepted again
            a.close()
            deadline = time.time() + 5
            while time.time() < deadline:
                c = ws_connect(self.port)
                try:
                    ws_hello(c, {"type": "hello", "role": "publisher", "room": ROOM,
                                 "token": TABLET_TOKEN})
                    opcode, payload = ws_read_frame(c)
                    if opcode == 0x8:
                        time.sleep(0.3)  # registry cleanup lag, retry
                        continue
                    self.assertEqual(json.loads(payload.decode("utf-8")).get("type"),
                                     "hello_ok", "publisher must be accepted after old left")
                    return
                finally:
                    c.close()
            self.fail("publisher not accepted within 5s after previous left")
        finally:
            a.close()

    # ---- routing ----

    def test_publisher_cam_state_reaches_viewer_and_offer_routed(self):
        pub = ws_connect(self.port)
        try:
            ws_hello(pub, {"type": "hello", "role": "publisher", "room": ROOM,
                           "token": TABLET_TOKEN})
            self.assertEqual(ws_recv_json(pub).get("type"), "hello_ok")
            viewer = ws_connect(self.port)
            try:
                ws_hello(viewer, {"type": "hello", "role": "viewer", "room": ROOM,
                                  "token": VIEWER_TOKEN})
                ok = ws_recv_json(viewer)
                self.assertEqual(ok.get("type"), "hello_ok")
                # immediately after hello_ok the server sends a cam_state snapshot
                snap = ws_recv_json(viewer)
                self.assertEqual(snap.get("type"), "cam_state")
                # publisher publishes new state -> viewer receives it
                ws_hello(pub, {"type": "cam_state", "enabled": True,
                               "state": "STREAMING", "viewers": 1})
                # (publisher->server messages are JSON too; ws_hello is a generic send)
                got = ws_recv_json(viewer)
                self.assertEqual(got.get("type"), "cam_state")
                self.assertEqual(got.get("state"), "STREAMING")
                # viewer offer -> publisher gets it routed with from=viewer id
                ws_hello(viewer, {"type": "offer", "payload": {"type": "offer", "sdp": "x"}})
                routed = ws_recv_json(pub)
                self.assertEqual(routed.get("type"), "offer")
                self.assertTrue(str(routed.get("from", "")).startswith("v"),
                                "routed offer must carry viewer id")
            finally:
                viewer.close()
        finally:
            pub.close()

    def test_offer_when_camera_disabled_rejected_for_viewer(self):
        pub = ws_connect(self.port)
        try:
            ws_hello(pub, {"type": "hello", "role": "publisher", "room": ROOM,
                           "token": TABLET_TOKEN})
            ws_recv_json(pub)
            viewer = ws_connect(self.port)
            try:
                ws_hello(viewer, {"type": "hello", "role": "viewer", "room": ROOM,
                                  "token": VIEWER_TOKEN})
                ws_recv_json(viewer)  # hello_ok
                ws_recv_json(viewer)  # cam_state snapshot
                # publisher reports disabled state -> viewer offer must be rejected
                ws_hello(pub, {"type": "cam_state", "enabled": False,
                               "state": "IDLE", "viewers": 1})
                ws_recv_json(viewer)  # cam_state(enabled=False) broadcast
                ws_hello(viewer, {"type": "offer", "payload": {"type": "offer", "sdp": "x"}})
                err = ws_recv_json(viewer)
                self.assertEqual(err.get("type"), "error")
                self.assertEqual(err.get("code"), "camera_disabled",
                                 "offer with disabled camera must be rejected as camera_disabled")
            finally:
                viewer.close()
        finally:
            pub.close()

    # ---- dashboard HTTP Basic ----

    def test_dashboard_requires_basic_auth(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/dashboard")
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 401, "dashboard without creds must be 401")
        self.assertEqual(json.loads(body.decode("utf-8")).get("error"), "unauthorized")
        www = resp.getheader("WWW-Authenticate") or ""
        self.assertIn("Basic", www, "401 must carry WWW-Authenticate: Basic")
        conn.close()

    def test_dashboard_bad_password_401(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/dashboard", headers=basic_header(DASH_USER, "wrong"))
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 401, "dashboard with wrong password must be 401")
        conn.close()

    def test_dashboard_good_auth_200_and_summary(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/dashboard", headers=basic_header(DASH_USER, DASH_PASS))
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200, "dashboard with good creds must be 200")
        self.assertIn(b"<!DOCTYPE html", body[:64])
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/cam/summary", headers=basic_header(DASH_USER, DASH_PASS))
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(resp.status, 200)
        self.assertIn("rooms", data)
        conn.close()

    def test_summary_includes_all_configured_rooms_without_publisher(self):
        """Summary обязан показывать все конфиг-комнаты, не только с паблишером."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/cam/summary", headers=basic_header(DASH_USER, DASH_PASS))
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(resp.status, 200)
        rooms = data.get("rooms") or {}
        self.assertIn(ROOM, rooms)
        self.assertIn("kabinet", rooms)
        # Реестр глобален и порядок тестов не фиксирован: строгий офлайн-контракт
        # проверяем на комнате, которую ни один тест не подключал (kabinet).
        entry = rooms["kabinet"]
        self.assertTrue(entry.get("configured"), "kabinet")
        self.assertFalse(entry.get("publisher_connected"), "kabinet")
        cs = entry.get("cam_state") or {}
        self.assertEqual(cs.get("state"), "OFFLINE", "kabinet")
        # офлайн = флаг неизвестен, а не "выключена"
        self.assertNotIn("enabled", cs)
        self.assertTrue(rooms[ROOM].get("configured"), ROOM)
        conn.close()

    def test_settings_cameras_rooms_no_token_leak(self):
        """GET /api/settings: cameras_rooms = room+tablet (для подсказки UI);
        tablet_token никогда не покидает конфиг (негатив по утечке секрета)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/settings")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(resp.status, 200)
        conn.close()
        rooms = data.get("cameras_rooms")
        self.assertIsInstance(rooms, list)
        self.assertTrue(rooms, "configured cam rooms must be listed")
        self.assertTrue(all(sorted(r.keys()) == ["room", "tablet"] for r in rooms),
                        "cameras_rooms entries must carry only room and tablet")
        self.assertNotIn("tablet_token", json.dumps(data),
                         "tablet_token must not leak via /api/settings")

    def test_cam_rooms_admin_auth_add_remove(self):
        """Админ комнат: 401 без пары, add пишет конфиг (токен показан раз),
        remove убирает только целевую комнату (остальные не теряются)."""
        # 1) без Basic - 401
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/cam/rooms",
                     body=json.dumps({"action": "add", "room": "kabinet2", "tablet": "127.0.0.2"}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 401, "rooms admin without creds must be 401")
        conn.close()
        # 2) add с Basic: сгенерированный tablet_token возвращается один раз
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/cam/rooms",
                     body=json.dumps({"action": "add", "room": "kabinet2", "tablet": "127.0.0.2"}),
                     headers=dict(basic_header(DASH_USER, DASH_PASS),
                                  **{"Content-Type": "application/json"}))
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(resp.status, 200)
        self.assertEqual(data.get("action"), "add")
        self.assertIn("kabinet2", data.get("rooms") or [])
        tok = data.get("tablet_token") or ""
        self.assertGreaterEqual(len(tok), 16, "generated tablet_token must be returned once")
        conn.close()
        # 3) конфиг persist: kabinet2 с токеном
        cfg = json.load(open(self.config_path, encoding="utf-8"))
        rooms = (cfg.get("cameras") or {}).get("rooms") or {}
        self.assertIn("kabinet2", rooms)
        self.assertGreaterEqual(len(rooms["kabinet2"].get("tablet_token") or ""), 16)
        # 4) remove: уходит только kabinet2
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/cam/rooms",
                     body=json.dumps({"action": "remove", "room": "kabinet2"}),
                     headers=dict(basic_header(DASH_USER, DASH_PASS),
                                  **{"Content-Type": "application/json"}))
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(resp.status, 200)
        self.assertNotIn("kabinet2", data.get("rooms") or [])
        conn.close()
        cfg = json.load(open(self.config_path, encoding="utf-8"))
        rooms = (cfg.get("cameras") or {}).get("rooms") or {}
        self.assertNotIn("kabinet2", rooms)
        self.assertIn(ROOM, rooms, "no-loss: other rooms must survive remove")

    def test_viewer_token_endpoint_auth(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/cam/viewer-token")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 401, "viewer-token without creds must be 401")
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/cam/viewer-token", headers=basic_header(DASH_USER, DASH_PASS))
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(resp.status, 200)
        self.assertEqual(data.get("viewer_token"), VIEWER_TOKEN,
                         "authorized dashboard must receive the viewer token")
        self.assertIn(ROOM, data.get("rooms", []))
        conn.close()




# ---- tolerant handshake: legacy kiosk builds send lines with \r\r\n ----


def ws_connect_cr_cr_lf(port, path="/ws/cam"):
    """Handshake exactly like the Digma .13 byte-dump (22.11.2026): every line,
    including the final blank one, terminated with \r\r\n instead of \r\n.
    Server must still answer 101 (lenient rfile reader)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = ("GET %s HTTP/1.1\r\r\nHost: 127.0.0.1:%d\r\r\n"
           "Upgrade: websocket\r\r\nConnection: Upgrade\r\r\n"
           "Sec-WebSocket-Key: %s\r\r\nSec-WebSocket-Version: 13\r\r\n\r\r\n"
           % (path, port, key))
    sock.sendall(req.encode("ascii"))
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("handshake eof")
        resp += chunk
    head = resp.split(b"\r\n\r\n", 1)[0]
    if b" 101 " not in head.split(b"\r\n")[0]:
        raise AssertionError("handshake not 101: %r" % head.split(b"\r\n")[0])
    sock.settimeout(5)
    return sock


class _ChunkyRaw:
    """Fake BufferedReader: yields buffered data in fixed-size pieces."""

    def __init__(self, data, piece=3):
        self._data = data
        self._piece = piece
        self.reads = 0
        self.closed = False

    def read1(self, n):
        self.reads += 1
        k = min(n, self._piece)
        out, self._data = self._data[:k], self._data[k:]
        return out

    def read(self, n=-1):
        self.reads += 1
        if n is None or n < 0:
            out, self._data = self._data, b""
            return out
        out, self._data = self._data[:n], self._data[n:]
        return out

    def close(self):
        self.closed = True


class TestLenientCRLFLineReader(unittest.TestCase):
    """_LenientCRLFLineReader: \r\r\n -> \r\n, no byte loss across reads."""

    def test_wellformed_crlf_passthrough(self):
        rd = caching_server._LenientCRLFLineReader(
            _ChunkyRaw(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"))
        self.assertEqual(rd.readline(), b"GET / HTTP/1.1\r\n")
        self.assertEqual(rd.readline(), b"Host: x\r\n")
        self.assertEqual(rd.readline(), b"\r\n")
        self.assertEqual(rd.readline(), b"")

    def test_cr_cr_lf_normalized_byte_per_byte(self):
        rd = caching_server._LenientCRLFLineReader(_ChunkyRaw(
            b"GET /ws/cam HTTP/1.1\r\r\nHost: 127.0.0.1:8099\r\r\n\r\r\n",
            piece=1))
        self.assertEqual(rd.readline(), b"GET /ws/cam HTTP/1.1\r\n")
        self.assertEqual(rd.readline(), b"Host: 127.0.0.1:8099\r\n")
        self.assertEqual(rd.readline(), b"\r\n")
        self.assertEqual(rd.readline(), b"")

    def test_limit_respected(self):
        rd = caching_server._LenientCRLFLineReader(_ChunkyRaw(b"abcdef\r\r\ngh\r\n"))
        self.assertEqual(rd.readline(4), b"abcd")
        self.assertEqual(rd.readline(), b"ef\r\n")
        self.assertEqual(rd.readline(), b"gh\r\n")

    def test_read_passthrough_no_byte_loss(self):
        data = b"GET / HTTP/1.1\r\r\n\r\r\n" + bytes(range(256))
        rd = caching_server._LenientCRLFLineReader(_ChunkyRaw(data, piece=5))
        self.assertEqual(rd.readline(), b"GET / HTTP/1.1\r\n")
        self.assertEqual(rd.readline(), b"\r\n")
        self.assertEqual(rd.read(10), bytes(range(10)))
        # read1 returns at most ONE underlying chunk (piece=5 here)
        self.assertEqual(rd.read1(6), bytes(range(10, 15)))
        self.assertEqual(rd.read(300), bytes(range(15, 256)))
        self.assertEqual(rd.read(1), b"")

    def test_read_with_leftover_buffer(self):
        rd = caching_server._LenientCRLFLineReader(_ChunkyRaw(b"line\r\r\nXY"))
        self.assertEqual(rd.readline(), b"line\r\n")
        self.assertEqual(rd.read(1), b"X")
        self.assertEqual(rd.read(1), b"Y")

    def test_close_delegates(self):
        raw = _ChunkyRaw(b"x\r\n")
        caching_server._LenientCRLFLineReader(raw).close()
        self.assertTrue(raw.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
