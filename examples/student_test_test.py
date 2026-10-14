"""Offline HTTP tests; no Instagram login or external requests."""
import http.client
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from student_test import CONSENT_VERSION, TRACKING_SECONDS, TrackingSessions, VisitStore, address_type, collector_handler


class StudentTestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.store = VisitStore(Path(cls.folder.name) / "visits.json")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), collector_handler(cls.store, "test-token"))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.folder.cleanup()

    def setUp(self):
        self.store.clear()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def submit(self, **overrides):
        body = {"username": "student.one", "consent": True, "consent_version": CONSENT_VERSION, "token": "test-token"}
        body.update(overrides)
        return self.request("POST", "/api/submit", json.dumps(body), {"Content-Type": "application/json"})

    def test_opening_page_does_not_save_address(self):
        status, body, headers = self.request("GET", "/test")
        self.assertEqual(status, 200)
        self.assertIn(b"consent", body)
        self.assertNotIn(b"test-token", body)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertFalse(self.store.path.exists())

    def test_explicit_consent_is_required(self):
        for consent in (False, None, "true", 1):
            self.assertEqual(self.submit(consent=consent)[0], 400)
        self.assertEqual(self.submit(consent_version="old")[0], 400)
        self.assertEqual(self.store.read(), [])

    def test_valid_submission_uses_connection_address(self):
        status, body, _ = self.submit(ip="8.8.8.8", simulated=True)
        record = json.loads(body)
        self.assertEqual(status, 201)
        self.assertEqual(record["ip"], "127.0.0.1")
        self.assertEqual(record["address_type"], "Loopback (this computer)")
        self.assertFalse(record["simulated"])
        self.assertEqual(record["identity"], "Self-reported username")
        self.assertEqual(len(self.store.read()), 1)
        self.assertNotIn("session_id", self.store.read()[0])

    def test_checks_require_consent_session_and_stop_revokes_it(self):
        for body in ({}, {"session_id": "unknown"}, {"session_id": []}):
            self.assertEqual(self.request("POST", "/api/check", json.dumps(body), {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.store.read(), [])
        session = json.loads(self.submit()[1])["session_id"]
        payload = json.dumps({"session_id": session, "ip": "8.8.8.8"})
        status, body, _ = self.request("POST", "/api/check", payload, {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["ip"], "127.0.0.1")
        self.assertFalse(json.loads(body)["changed"])
        self.assertEqual(len(self.store.read()), 1)
        self.assertEqual(self.request("POST", "/api/stop", payload, {"Content-Type": "application/json"})[0], 200)
        self.assertEqual(self.request("POST", "/api/check", payload, {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(len(self.store.read()), 1)

    def test_old_one_time_consent_cannot_enable_automatic_checks(self):
        self.assertEqual(self.submit(consent_version="classroom-ip-v1")[0], 400)

    def test_changed_addresses_are_recorded_once_per_change(self):
        sessions = TrackingSessions(self.store)
        session = sessions.start("student", "192.168.1.5")["session_id"]
        self.assertFalse(sessions.check(session, "192.168.1.5")["changed"])
        self.assertTrue(sessions.check(session, "192.168.1.6")["changed"])
        self.assertFalse(sessions.check(session, "192.168.1.6")["changed"])
        self.assertTrue(sessions.check(session, "192.168.1.5")["changed"])
        self.assertEqual([row["ip"] for row in self.store.read()], ["192.168.1.5", "192.168.1.6", "192.168.1.5"])

    def test_expired_session_cannot_record_new_address(self):
        sessions = TrackingSessions(self.store)
        with patch("student_test.time.monotonic", return_value=100):
            session = sessions.start("student", "192.168.1.5")["session_id"]
        with patch("student_test.time.monotonic", return_value=100 + TRACKING_SECONDS):
            self.assertIsNone(sessions.check(session, "192.168.1.6"))
        self.assertEqual(len(self.store.read()), 1)

    def test_session_capacity_does_not_evict_active_students(self):
        sessions = TrackingSessions(self.store)
        with patch("student_test.MAX_SESSIONS", 1):
            session = sessions.start("first", "192.168.1.5")["session_id"]
            self.assertIsNone(sessions.start("second", "192.168.1.6"))
            sessions.stop(session)
            self.assertIsNotNone(sessions.start("second", "192.168.1.6"))

    def test_forwarded_headers_cannot_spoof_ip(self):
        body = {"username": "student", "consent": True, "consent_version": CONSENT_VERSION, "token": "test-token"}
        status, response, _ = self.request("POST", "/api/submit", json.dumps(body), {
            "Content-Type": "application/json", "X-Forwarded-For": "8.8.8.8", "X-Real-IP": "1.1.1.1"})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(response)["ip"], "127.0.0.1")

    def test_current_classroom_link_is_required(self):
        for token in ("wrong", "", None, 123, "é"):
            self.assertEqual(self.submit(token=token)[0], 403)
        self.assertEqual(self.store.read(), [])

    def test_invalid_usernames_are_rejected(self):
        for username in ("", "@student", "<script>", "a" * 31, None):
            self.assertEqual(self.submit(username=username)[0], 400)
        self.assertEqual(self.store.read(), [])

    def test_malformed_requests_do_not_write(self):
        self.assertEqual(self.request("POST", "/api/submit", "{", {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/api/submit", "[]", {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/api/submit", "{}", {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/submit", "x" * 2049, {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.store.read(), [])

    def test_collector_does_not_expose_dashboard_or_records(self):
        for path in ("/api/student-test", "/api/events", "/api/account", "/../.env", "/visits.json"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", "/api/student-test/clear", "{}")[0], 404)

    def test_address_labels(self):
        self.assertEqual(address_type("192.168.1.8"), "Local/private IP")
        self.assertEqual(address_type("8.8.8.8"), "Public IP")
        self.assertEqual(address_type("::1"), "Loopback (this computer)")
        self.assertEqual(address_type("fd00::1"), "Local/private IP")

    def test_expired_records_are_removed_from_disk(self):
        with patch("student_test.time.time", return_value=time.time() - 90000):
            self.store.add("expired", "127.0.0.1")
        self.assertEqual(self.store.read(), [])
        self.assertEqual(json.loads(self.store.path.read_text()), [])

    def test_retention_is_bounded_and_clear_removes_file(self):
        with patch("student_test.MAX_RECORDS", 2):
            for username in ("first", "second", "third"):
                self.store.add(username, "127.0.0.1")
            self.assertEqual([r["username"] for r in self.store.read()], ["third", "second"])
        self.store.clear()
        self.assertFalse(self.store.path.exists())


class DashboardIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls.folder.name)
        shutil.copytree(Path(__file__).resolve().parents[1] / "dashboard", cls.root / "dashboard")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        code = (
            "import sys; from pathlib import Path; import dashboard; "
            "dashboard.ROOT=Path(sys.argv[1]); dashboard.ENV_FILE=dashboard.ROOT/'.env'; "
            "sys.argv=['dashboard.py','--no-browser','--no-monitor','--student-test',"
            "'--student-test-port','0','--port',sys.argv[2]]; dashboard.main()"
        )
        cls.process = subprocess.Popen([sys.executable, "-c", code, str(cls.root), str(cls.port)],
                                       cwd=Path(__file__).resolve().parents[1],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls.folder.cleanup)
        # LIFO: wait for the process to exit before deleting its temporary data.
        cls.addClassCleanup(cls.stop_process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                cls.call("GET", "/api/student-test")
                break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError("Test dashboard did not start")

    @classmethod
    def stop_process(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)

    @classmethod
    def call(cls, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=5)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_dashboard_status_and_separate_collector(self):
        status, data = self.call("GET", "/api/student-test")
        self.assertEqual(status, 200)
        self.assertTrue(data["enabled"])
        self.assertNotEqual(data["port"], self.port)
        self.assertIn("/test#", data["local_url"])
        self.assertEqual(self.call("GET", "/api/status")[1]["monitorEnabled"], False)

    def test_clear_requires_local_origin_and_really_deletes(self):
        store = VisitStore(self.root / "tools/student-test/visits.json")
        store.add("temporary", "127.0.0.1")
        self.assertEqual(self.call("POST", "/api/student-test/clear")[0], 403)
        self.assertEqual(self.call("POST", "/api/student-test/clear", {"Origin": "http://untrusted.example"})[0], 403)
        self.assertTrue(store.path.exists())
        self.assertEqual(self.call("POST", "/api/student-test/clear", {"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)
        self.assertFalse(store.path.exists())

    def test_untrusted_host_cannot_read_test_records_or_link(self):
        self.assertEqual(self.call("GET", "/api/student-test", {"Host": "untrusted.example"})[0], 403)


if __name__ == "__main__":
    unittest.main()
