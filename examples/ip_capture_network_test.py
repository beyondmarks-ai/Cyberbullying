"""Verify capture against real client sockets; never touches live visit records.

Usage: python examples/ip_capture_network_test.py --lan-ip <this computer's Wi-Fi IPv4>
"""
import argparse
import http.client
import ipaddress
import json
import secrets
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from student_test import CONSENT_VERSION, VisitStore, collector_handler


def run(lan_ip):
    lan_ip = str(ipaddress.IPv4Address(lan_ip))
    folder = ROOT / "tools/student-test"
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="network-check-", dir=folder) as temporary:
        store = VisitStore(Path(temporary) / "visits.json")
        token = secrets.token_urlsafe(32)
        server = ThreadingHTTPServer(("0.0.0.0", 0), collector_handler(store, token))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        checks = []

        def request(source, endpoint, payload):
            connection = http.client.HTTPConnection(source, server.server_port, timeout=5,
                                                   source_address=(source, 0))
            try:
                connection.connect()
                actual_source = connection.sock.getsockname()[0]
                connection.request("POST", endpoint, json.dumps(payload), {
                    "Content-Type": "application/json", "X-Forwarded-For": "203.0.113.77",
                    "X-Real-IP": "198.51.100.88"})
                response = connection.getresponse()
                return response.status, json.loads(response.read()), actual_source
            finally:
                connection.close()

        def verify(name, source, endpoint, payload, expected_count, changed=None):
            status, result, actual_source = request(source, endpoint, payload)
            assert status in (200, 201), (name, status, result)
            assert result["ip"] == actual_source, (name, result["ip"], actual_source)
            assert store.read()[0]["ip"] == actual_source
            assert len(store.read()) == expected_count
            if changed is not None:
                assert result["changed"] is changed
            checks.append({"test": name, "socket_source": actual_source, "captured_ip": result["ip"],
                           "saved_records": expected_count, "passed": True})
            return result

        try:
            first = verify("Initial loopback capture", "127.0.0.1", "/api/submit", {
                "token": token, "username": "socket_check", "consent": True,
                "consent_version": CONSENT_VERSION, "ip": "192.0.2.99"}, 1)
            payload = {"session_id": first["session_id"], "ip": "192.0.2.99"}
            verify("Unchanged loopback is not duplicated", "127.0.0.1", "/api/check", payload, 1, False)
            verify("Same session changes to real LAN source", lan_ip, "/api/check", payload, 2, True)
            verify("Unchanged LAN address is not duplicated", lan_ip, "/api/check", payload, 2, False)
            verify("Return to original source is recorded", "127.0.0.1", "/api/check", payload, 3, True)
            assert request("127.0.0.1", "/api/stop", payload)[0] == 200
            assert request(lan_ip, "/api/check", payload)[0] == 403
            assert len(store.read()) == 3
            checks.append({"test": "Stopped session rejects another real connection", "passed": True})
            other = verify("Another connection can enter the same unverified username", lan_ip, "/api/submit", {
                "token": token, "username": "socket_check", "consent": True,
                "consent_version": CONSENT_VERSION}, 4)
            assert request(lan_ip, "/api/stop", {"session_id": other["session_id"]})[0] == 200
            report = {"time": datetime.now(timezone.utc).isoformat(), "checks": checks,
                      "username_ownership_verified": False,
                      "scope": "Real TCP requests from this computer's loopback and LAN interfaces.",
                      "limitations": ["Not a separate physical phone test.",
                                      "Does not verify an Instagram sender's IP or ownership of an entered username.",
                                      "No public-IP, VPN or mobile-data test was performed."]}
            (folder / "network-verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lan-ip", required=True)
    run(parser.parse_args().lan_ip)
