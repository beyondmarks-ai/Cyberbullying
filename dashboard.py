import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"
USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")


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


def read_events(username):
    path = event_file(username)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines()[-200:]:
        try:
            events.append(json.loads(line))
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
    args = parser.parse_args()
    monitor = None
    lock = threading.Lock()

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
        monitor = subprocess.Popen([sys.executable, ROOT / "examples/comment_monitor.py"], cwd=ROOT, env=environment)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *handler_args, **kwargs):
            super().__init__(*handler_args, directory=ROOT / "dashboard", **kwargs)

        def do_GET(self):
            username, _ = credentials()
            if self.path == "/api/events":
                return self.send_json(read_events(username) if username else [])
            if self.path == "/api/status":
                return self.send_json({"running": monitor is not None and monitor.poll() is None, "username": username})
            if self.path == "/api/account":
                return self.send_json({"username": username})
            return super().do_GET()

        def do_POST(self):
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
                return self.send_json({"error": "Instagram rejected this login. Check the credentials or Instagram challenge."}, 401)
            with lock:
                save_credentials(username, password)
                start_monitor()
            return self.send_json({"username": username, "running": True})

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

    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    start_monitor()
    print("Dashboard: http://127.0.0.1:8765")
    if not args.no_browser:
        webbrowser.open("http://127.0.0.1:8765")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if monitor and monitor.poll() is None:
            monitor.terminate()
        server.server_close()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        assert USERNAME.fullmatch("valid.user_1") and not USERNAME.fullmatch("invalid user")
        print("Self-test passed")
    else:
        main()
