"""Opt-in classroom IP demonstration, isolated from the Instagram dashboard."""
import ipaddress
import json
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RETENTION_SECONDS = 24 * 60 * 60
MAX_RECORDS = 500
CONSENT_VERSION = "classroom-ip-v2"
TRACKING_SECONDS = 60 * 60
CHECK_SECONDS = 15
MAX_SESSIONS = 500


def address_type(address):
    ip = ipaddress.ip_address(address)
    if ip.is_loopback:
        return "Loopback (this computer)"
    if ip.is_global:
        return "Public IP"
    return "Local/private IP"


class VisitStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def _read(self):
        try:
            records = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        cutoff = time.time() - RETENTION_SECONDS
        return [record for record in records if record.get("timestamp", 0) > cutoff][-MAX_RECORDS:]

    def _write(self, records):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def read(self):
        with self.lock:
            records = self._read()
            if self.path.exists():
                self._write(records)  # Purge expired records on dashboard refresh.
            return list(reversed(records))

    def add(self, username, peer_address):
        # Only the TCP peer is authoritative here. Never trust supplied IPs or
        # forwarded headers from arbitrary clients/proxies.
        address = str(ipaddress.ip_address(peer_address))
        record = {"username": username, "ip": address, "address_type": address_type(address),
                  "source": "Consented test-page connection check", "identity": "Self-reported username",
                  "simulated": False, "consent_version": CONSENT_VERSION,
                  "timestamp": time.time(), "time": datetime.now(timezone.utc).isoformat()}
        with self.lock:
            records = self._read()
            records.append(record)
            self._write(records[-MAX_RECORDS:])
        return record

    def clear(self):
        with self.lock:
            self.path.unlink(missing_ok=True)


class TrackingSessions:
    """Short-lived consent sessions; only changed socket addresses are saved."""

    def __init__(self, store):
        self.store = store
        self.sessions = {}
        self.lock = threading.Lock()

    def start(self, username, address):
        with self.lock:
            now = time.monotonic()
            self.sessions = {key: value for key, value in self.sessions.items() if value["expires"] > now}
            if len(self.sessions) >= MAX_SESSIONS:
                return None
            record = self.store.add(username, address)
            session_id = secrets.token_urlsafe(32)
            self.sessions[session_id] = {"username": username, "ip": record["ip"], "expires": now + TRACKING_SECONDS}
            return {**record, "session_id": session_id, "expires_in_seconds": TRACKING_SECONDS,
                    "check_seconds": CHECK_SECONDS}

    def check(self, session_id, address):
        with self.lock:
            session = self.sessions.get(session_id)
            if not session or session["expires"] <= time.monotonic():
                self.sessions.pop(session_id, None)
                return None
            address = str(ipaddress.ip_address(address))
            changed = session["ip"] != address
            if changed:
                self.store.add(session["username"], address)
                session["ip"] = address
            return {"ip": address, "address_type": address_type(address), "changed": changed,
                    "checked_at": datetime.now(timezone.utc).isoformat()}

    def stop(self, session_id):
        with self.lock:
            self.sessions.pop(session_id, None)


def collector_handler(store, token):
    tracking = TrackingSessions(store)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def respond(self, status, body, content_type="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in {"/", "/test"}:
                return self.respond(200, (ROOT / "dashboard/student-test.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
            return self.respond(404, {"error": "Not found"})

        def do_POST(self):
            if self.path not in {"/api/submit", "/api/check", "/api/stop"}:
                return self.respond(404, {"error": "Not found"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.respond(415, {"error": "JSON is required"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048:
                    raise ValueError()
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError()
            except (ValueError, UnicodeError):
                return self.respond(400, {"error": "Invalid request"})
            if self.path in {"/api/check", "/api/stop"}:
                session_id = body.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    return self.respond(403, {"error": "Start a new test with consent."})
                if self.path == "/api/stop":
                    tracking.stop(session_id)
                    return self.respond(200, {"stopped": True})
                result = tracking.check(session_id, self.client_address[0])
                if result is None:
                    return self.respond(403, {"error": "Test session ended. Start again only if you agree."})
                return self.respond(200, result)
            supplied_token = body.get("token")
            if not isinstance(supplied_token, str) or not secrets.compare_digest(supplied_token.encode(), token.encode()):
                return self.respond(403, {"error": "Open the current test link provided by your instructor."})
            if body.get("consent") is not True or body.get("consent_version") != CONSENT_VERSION:
                return self.respond(400, {"error": "Consent is required before saving an IP address."})
            username = body.get("username")
            if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
                return self.respond(400, {"error": "Enter a username using letters, numbers, dots or underscores."})
            record = tracking.start(username, self.client_address[0])
            if record is None:
                return self.respond(429, {"error": "The classroom test is full. Try again later."})
            return self.respond(201, record)

        def log_message(self, *_):
            # No access logs: loading the page does not persist an IP address.
            pass

    return Handler


def start_collector(store, port=8766):
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(("0.0.0.0", port), collector_handler(store, token))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        addresses = sorted({entry[4][0] for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                            if not ipaddress.ip_address(entry[4][0]).is_loopback})
    except OSError:
        addresses = []
    port = server.server_port
    return server, {"enabled": True, "port": port,
                    "local_url": f"http://127.0.0.1:{port}/test#{token}",
                    "phone_urls": [f"http://{address}:{port}/test#{token}" for address in addresses]}
