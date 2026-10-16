"""Real spawned Radar workers against a loopback-only HTTP server."""

import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests

from attendance_fixtures import radar_detail
from xmu_rollcall import radar_campaign as campaign


REAL_WORKER = campaign._campaign_worker


def local_campaign_worker(connection, settings, *args):
    """Only replace the endpoint; execute the real worker, Session and HTTP stack."""
    from xmu_rollcall import verify

    origin = settings["headers"].pop("X-Offline-Test-Origin")
    if not origin.startswith("http://127.0.0.1:"):
        raise AssertionError("Integration tests must use loopback")
    verify.base_url = origin
    original_request = requests.Session.request

    def local_request(session, method, url, **kwargs):
        if not url.startswith(origin + "/"):
            raise AssertionError("External requests are forbidden in this test")
        return original_request(session, method, url, **kwargs)

    with patch.object(requests.Session, "request", local_request):
        REAL_WORKER(connection, settings, *args)


class RadarCampaignIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.enterContext(patch.object(campaign, "CONFIG_DIR", Path(self.directory.name)))
        self.enterContext(patch.dict(os.environ, {"XMU_ROLLCALL_CONFIG_DIR": self.directory.name}))
        self.requests = []
        self.slow = False
        self.release = threading.Event()
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                test.requests.append(("GET", self.path, dict(self.headers), None))
                payload = json.dumps(radar_detail()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    if test.slow:
                        # Continuous bytes prevent an inactivity timeout; the process deadline must stop it.
                        for byte in payload:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            if test.release.wait(0.02):
                                break
                    else:
                        self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_PUT(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.requests.append(("PUT", self.path, dict(self.headers), payload))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()

        def stop_server():
            self.release.set()
            server.shutdown()
            thread.join(2)
            server.server_close()

        self.addCleanup(stop_server)
        self.session = requests.Session()
        self.addCleanup(self.session.close)
        self.session.trust_env = False
        self.session.headers["X-Offline-Test-Origin"] = f"http://127.0.0.1:{server.server_port}"
        self.session.headers["X-Test-Session"] = "synthetic-session-header"
        self.session.cookies.set("session", "synthetic-cookie", domain="127.0.0.1", path="/api")
        self.session.cookies.set("other_path", "must-not-be-sent", domain="127.0.0.1", path="/elsewhere")

    def run_campaign(self, seconds):
        with patch.object(campaign, "CAMPAIGN_SECONDS", seconds), \
                patch.object(campaign, "_campaign_worker", local_campaign_worker), \
                contextlib.redirect_stdout(io.StringIO()):
            return campaign.run_radar_campaign(self.session, 123, account_id=1, student_id=900, course_id=42)

    def state(self):
        return json.loads((Path(self.directory.name) / "radar_attempts.json").read_text())

    def test_real_worker_preserves_cookie_headers_submits_and_persists_success(self):
        before = {child.pid for child in multiprocessing.active_children()}
        self.assertTrue(self.run_campaign(5))
        self.assertEqual([(method, path) for method, path, _, _ in self.requests],
                         [("GET", "/api/rollcall/123"), ("PUT", "/api/rollcall/123/answer")])
        for _, _, headers, _ in self.requests:
            self.assertEqual(headers["X-Test-Session"], "synthetic-session-header")
            self.assertEqual(headers["Cookie"], "session=synthetic-cookie")
        self.assertEqual(self.requests[1][3]["latitude"], 24.3)
        self.assertEqual(self.requests[1][3]["longitude"], 118.0)
        self.assertEqual(self.state()["attempts"][campaign._key(1, 123)]["status"], "success")
        self.assertTrue(self.run_campaign(5))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)

    def test_real_streaming_get_stops_at_deadline_and_never_submits_or_restarts(self):
        self.slow = True
        before = {child.pid for child in multiprocessing.active_children()}
        began = time.monotonic()
        self.assertFalse(self.run_campaign(0.8))
        elapsed = time.monotonic() - began
        self.assertGreaterEqual(elapsed, 0.65)
        self.assertLess(elapsed, 2.5)
        self.assertEqual([(method, path) for method, path, _, _ in self.requests],
                         [("GET", "/api/rollcall/123")])
        self.assertEqual(self.state()["attempts"][campaign._key(1, 123)]["status"], "timeout")
        self.assertFalse(self.run_campaign(5))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)


if __name__ == "__main__":
    unittest.main()
