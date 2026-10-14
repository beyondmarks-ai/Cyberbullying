import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from student_test import VisitStore, start_collector

ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"
USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def login_error(output):
    """Translate aiograpi failures without exposing its traceback to the browser."""
    message = output.lower()
    if "needs_upgrade" in message or "out of date" in message:
        return "The Instagram client is out of date. Reinstall the Python requirements and try again."
    if "challenge" in message or "checkpoint" in message:
        return "Instagram requires a security check. Complete it in the Instagram app, then try again."
    if "two_factor" in message or "two-factor" in message:
        return "Instagram requires two-factor authentication; complete the login in the Instagram app first."
    if "bad_password" in message or "incorrect password" in message:
        return "Instagram says the password is incorrect."
    if "connectproxyerror" in message or "connecterror" in message or "timed out" in message:
        return "Could not reach Instagram. Check the internet or proxy connection and try again."
    if "rate limit" in message or "please wait" in message:
        return "Instagram temporarily limited login attempts. Wait a while, then try again."
    return "Instagram could not verify this login. Check the Instagram app for a security prompt and try again."


def credentials():
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and not key.lstrip().startswith("#"):
                values[key.strip()] = value.strip().strip("'\"")
    return values.get("IG_USERNAME", ""), values.get("IG_PASSWORD", "")


def event_file(username):
    account = hashlib.sha256(username.lower().encode()).hexdigest()[:12]
    return ROOT / "tools/accounts" / f"{account}-events.jsonl"


def monitor_log_file(username):
    account = hashlib.sha256(username.lower().encode()).hexdigest()[:12]
    return ROOT / "tools/accounts" / f"{account}-monitor.log"


def monitor_error(username):
    path = monitor_log_file(username)
    output = path.read_text(encoding="utf-8", errors="replace")[-20000:].lower() if path.exists() else ""
    if "challenge_required" in output or "challengerequired" in output or "checkpoint" in output:
        return "Instagram security check required"
    if "login_required" in output or "loginrequired" in output:
        return "Instagram session expired; reconnect account"
    if "needs_upgrade" in output or "out of date" in output:
        return "Instagram client update required"
    if "bad_password" in output or "incorrect password" in output:
        return "Instagram rejected the password"
    if "connectproxyerror" in output or "connecterror" in output:
        return "Cannot connect to Instagram"
    return "Monitor stopped" if output else None


def read_events(username):
    path = event_file(username)
    if not path.exists():
        return []
    profile_path = path.with_name(path.name.replace("-events.jsonl", "-profiles.json"))
    try:
        profiles = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        profiles = {}
    events = []
    for line in path.read_text(encoding="utf-8").splitlines()[-200:]:
        try:
            event = json.loads(line)
            sender = event.get("sender")
            source = event.get("source", "")
            # Older events stored the numeric sender ID as an @handle.
            legacy = re.match(r"^@(\d+)(?=\s|$)", source) if (
                event.get("kind") == "dm" or "direct message" in source
            ) else None
            sender_id = str(sender.get("id") or "") if sender else legacy.group(1) if legacy else ""
            profile = profiles.get(sender_id)
            if profile:
                event["sender"] = profile
                label = f"@{profile['username']}"
                event["source"] = label + (" · direct message" if event.get("kind") != "dm" else "")
            elif legacy:
                event["source"] = source.replace(legacy.group(0), f"Username unavailable (ID: {sender_id})", 1)
            events.append(event)
        except json.JSONDecodeError:
            pass
    return list(reversed(events))


def save_credentials(username, password):
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    replacements = {"IG_USERNAME": username, "IG_PASSWORD": password}
    output, found = [], set()
    for line in lines:
        key, separator, _ = line.partition("=")
        key = key.strip()
        if separator and key in replacements:
            output.append(f"{key}={replacements[key]}")
            found.add(key)
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in replacements.items() if key not in found)
    temporary = ENV_FILE.with_suffix(".tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    temporary.replace(ENV_FILE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--student-test", action="store_true", help="Enable the consent page for phones on port 8766")
    parser.add_argument("--no-monitor", action="store_true", help="Run the dashboard without connecting to Instagram")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--student-test-port", type=int, default=8766)
    args = parser.parse_args()
    monitor = None
    lock = threading.Lock()
    visits = VisitStore(ROOT / "tools/student-test/visits.json")
    collector = None
    collector_info = {"enabled": False, "phone_urls": []}

    def start_monitor():
        nonlocal monitor
        if monitor and monitor.poll() is None:
            monitor.terminate()
            try:
                monitor.wait(timeout=10)
            except subprocess.TimeoutExpired:
                monitor.kill()
        username, password = credentials()
        if not username or not password:
            monitor = None
            return
        environment = os.environ.copy()
        environment.update({"IG_USERNAME": username, "IG_PASSWORD": password})
        log_path = monitor_log_file(username)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as output:
            monitor = subprocess.Popen([sys.executable, ROOT / "examples/comment_monitor.py"], cwd=ROOT,
                                       env=environment, stdout=output, stderr=subprocess.STDOUT)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *handler_args, **kwargs):
            super().__init__(*handler_args, directory=ROOT / "dashboard", **kwargs)

        def do_GET(self):
            if self.path == "/api/student-test":
                if not self.local_test_request():
                    return self.send_json({"error": "Open the dashboard on this computer"}, 403)
                return self.send_json({**collector_info, "visits": visits.read()})
            username, _ = credentials()
            if self.path == "/api/events":
                return self.send_json(read_events(username) if username else [])
            if self.path == "/api/status":
                returncode = monitor.poll() if monitor is not None else None
                return self.send_json({"running": monitor is not None and returncode is None, "username": username,
                                       "monitorEnabled": not args.no_monitor,
                                       "workerPid": monitor.pid if monitor is not None else None,
                                       "exitCode": returncode,
                                       "error": monitor_error(username) if username and returncode is not None else None})
            if self.path == "/api/account":
                return self.send_json({"username": username})
            return super().do_GET()

        def do_POST(self):
            if self.path == "/api/student-test/clear":
                if not self.local_test_request(require_origin=True):
                    return self.send_json({"error": "Clear records from the local dashboard"}, 403)
                visits.clear()
                return self.send_json({"cleared": True})
            if self.path != "/api/account":
                return self.send_json({"error": "Not found"}, 404)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send_json({"error": "JSON is required"}, 415)
            length = int(self.headers.get("Content-Length", 0))
            if length < 1 or length > 8192:
                return self.send_json({"error": "Invalid request size"}, 400)
            try:
                body = json.loads(self.rfile.read(length))
                username, password = str(body.get("username", "")).strip(), str(body.get("password", ""))
            except (json.JSONDecodeError, AttributeError):
                return self.send_json({"error": "Invalid request"}, 400)
            if not USERNAME.fullmatch(username) or not password or len(password) > 200 or "\n" in password or "\r" in password:
                return self.send_json({"error": "Enter a valid Instagram username and password"}, 400)

            environment = os.environ.copy()
            environment.update({"IG_USERNAME": username, "IG_PASSWORD": password})
            try:
                result = subprocess.run([sys.executable, ROOT / "examples/comment_monitor.py", "--login-test"],
                                        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=90)
            except subprocess.TimeoutExpired:
                return self.send_json({"error": "Instagram login timed out. Try again."}, 504)
            if result.returncode:
                return self.send_json({"error": login_error(result.stdout + "\n" + result.stderr)}, 401)
            with lock:
                save_credentials(username, password)
                if not args.no_monitor:
                    start_monitor()
            return self.send_json({"username": username, "running": not args.no_monitor})

        def local_test_request(self, require_origin=False):
            hosts = {f"127.0.0.1:{server.server_port}", f"localhost:{server.server_port}"}
            origin = self.headers.get("Origin")
            return self.headers.get("Host") in hosts and (
                (not origin and not require_origin) or origin in {f"http://{host}" for host in hosts}
            )

        def send_json(self, body, status=200):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    class DashboardServer(ThreadingHTTPServer):
        next_cleanup = 0

        def service_actions(self):
            if time.monotonic() >= self.next_cleanup:
                visits.read()
                self.next_cleanup = time.monotonic() + 60

    server = DashboardServer(("127.0.0.1", args.port), Handler)
    try:
        if args.student_test:
            collector, collector_info = start_collector(visits, args.student_test_port)
        if not args.no_monitor:
            start_monitor()
        dashboard_url = f"http://127.0.0.1:{server.server_port}"
        print(f"Dashboard: {dashboard_url}", flush=True)
        if args.student_test:
            print("Student test links are available in the local dashboard.", flush=True)
        if not args.no_browser:
            webbrowser.open(dashboard_url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if monitor and monitor.poll() is None:
            monitor.terminate()
        if collector:
            collector.shutdown()
            collector.server_close()
        server.server_close()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        assert USERNAME.fullmatch("valid.user_1") and not USERNAME.fullmatch("invalid user")
        assert "out of date" in login_error("error_type: needs_upgrade")
        assert "password" in login_error("bad_password")
        assert "proxy" in login_error("ConnectProxyError")
        print("Self-test passed")
    else:
        main()
