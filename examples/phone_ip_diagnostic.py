"""Read-only ADB phone network check; does not submit consent or save live visits."""
import argparse
import ipaddress
import json
import re
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def run(adb, device, host):
    host = str(ipaddress.IPv4Address(host))

    def phone(*args):
        return subprocess.run([adb, "-s", device, "shell", *args], check=True,
                              capture_output=True, text=True, timeout=20).stdout.strip()

    route = phone("ip", "route", "get", host)
    match = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", route)
    if not match:
        raise RuntimeError("Could not determine the phone's source IPv4 address")
    phone_ip = str(ipaddress.IPv4Address(match.group(1)))
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append(self.client_address[0])
            body = json.dumps({"observed_ip": self.client_address[0]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer((host, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = json.loads(phone("curl", "--max-time", "10", "--fail", "-sS",
                                  f"http://{host}:{server.server_port}/"))
        assert result["observed_ip"] == phone_ip == observed[-1]
        app_result = phone("curl", "--max-time", "10", "--fail", "-sS", "-o", "/dev/null",
                           "-w", "%{http_code},%{local_ip}", f"http://{host}:8766/test")
        status, app_source = app_result.split(",")
        assert status == "200" and app_source == phone_ip
        report = {"time": datetime.now(timezone.utc).isoformat(), "phone_route_ip": phone_ip,
                  "server_observed_ip": result["observed_ip"], "addresses_match": True,
                  "live_test_page_http_status": int(status), "live_test_page_client_ip": app_source,
                  "scope": "Real HTTP GET requests from the attached physical Android phone over Wi-Fi.",
                  "limitations": ["Read-only network test; no consent submission or phone-browser interaction.",
                                  "Does not verify an Instagram message's source IP or username ownership.",
                                  "No mobile-data, public-IP or VPN test was performed."]}
        folder = Path(__file__).resolve().parents[1] / "tools/student-test"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "phone-verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--host", required=True)
    args = parser.parse_args()
    run(args.adb, args.device, args.host)
