import argparse
import hashlib
import hmac
import time
from http.cookies import SimpleCookie
import html
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'examples'))
from instagram_store import account_path, write_json, webhook_items
ENV_FILE = ROOT / ".env"
USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")
OAUTH_STATES = {}
OAUTH_TICKETS = {}
OAUTH_SCOPES = ",".join((
    "instagram_business_basic",
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
))


def setting(name, default=""):
    if name in os.environ:
        return os.environ[name]
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip().strip("'\"")
    return default


def instagram_token_file():
    files = sorted((ROOT / "tools/accounts").glob("*-instagram.json"), key=lambda path: path.stat().st_mtime)
    return files[-1] if files else None


def connected_account():
    path = instagram_token_file()
    return json.loads(path.read_text(encoding='utf-8')) if path else {}


def read_events(account_id):
    path = account_path(account_id, 'events.jsonl')
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines()[-200:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    latest = {}
    for index, event in enumerate(events):
        latest[event.get('id') or str(index)] = event
    return list(reversed(list(latest.values())))


def instagram_redirect_uri():
    return setting("IG_REDIRECT_URI", "http://127.0.0.1:8765/auth/instagram/callback")


def instagram_oauth_url(state):
    app_id = setting("IG_APP_ID")
    if not app_id:
        raise ValueError("Set IG_APP_ID in .env")
    return "https://www.instagram.com/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": app_id,
        "redirect_uri": instagram_redirect_uri(),
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": state,
    })


def instagram_token(code):
    app_id = setting("IG_APP_ID")
    app_secret = setting("IG_APP_SECRET")
    if not app_id or not app_secret:
        raise ValueError("Set IG_APP_ID and IG_APP_SECRET in .env")
    body = urllib.parse.urlencode({
        "client_id": app_id,
        "client_secret": app_secret,
        "grant_type": "authorization_code",
        "redirect_uri": instagram_redirect_uri(),
        "code": code,
    }).encode()
    request = urllib.request.Request("https://api.instagram.com/oauth/access_token", data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError) as error:
        detail = error.read().decode(errors="replace") if isinstance(error, urllib.error.HTTPError) else str(error)
        raise ValueError(f"Instagram token exchange failed: {detail}") from error


def long_lived_instagram_token(token_data):
    app_secret = setting("IG_APP_SECRET")
    query = urllib.parse.urlencode({
        "grant_type": "ig_exchange_token",
        "client_secret": app_secret,
        "access_token": token_data["access_token"],
    })
    request = urllib.request.Request(f"https://graph.instagram.com/access_token?{query}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            long_lived = json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError) as error:
        detail = error.read().decode(errors="replace") if isinstance(error, urllib.error.HTTPError) else str(error)
        raise ValueError(f"Long-lived Instagram token exchange failed: {detail}") from error
    return {**token_data, **long_lived, "expires_at": time.time() + long_lived["expires_in"]}


def save_instagram_token(token_data):
    request = urllib.request.Request('https://graph.instagram.com/me?fields=id,username',
                                     headers={'Authorization': 'Bearer '+token_data['access_token']})
    with urllib.request.urlopen(request, timeout=30) as response:
        profile = json.loads(response.read())
    token_data['username'] = profile['username']
    path = account_path(token_data['user_id'], 'instagram.json')
    write_json(path, token_data)
    return path


def webhook_verify_token():
    return hmac.new(setting('IG_APP_SECRET').encode(), b'instagram-webhook-verify', 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    monitor = None
    lock = threading.Lock()

    def stop_monitor():
        if monitor and monitor.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(monitor.pid), '/T', '/F'], capture_output=True)
            else:
                monitor.terminate()
            try:
                monitor.wait(timeout=10)
            except subprocess.TimeoutExpired:
                monitor.kill()

    def start_monitor():
        nonlocal monitor
        stop_monitor()
        token_file = instagram_token_file()
        if not token_file:
            monitor = None
            return
        environment = os.environ.copy()
        environment["IG_TOKEN_FILE"] = str(token_file)
        monitor = subprocess.Popen([sys.executable, ROOT / "examples/comment_monitor.py"], cwd=ROOT, env=environment)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *handler_args, **kwargs):
            super().__init__(*handler_args, directory=ROOT / "dashboard", **kwargs)

        def do_GET(self):
            account = connected_account()
            username = account.get('username', '')
            path = urllib.parse.urlsplit(self.path).path
            if self.headers.get('CF-Connecting-IP') and path not in {'/auth/instagram/start', '/auth/instagram/callback', '/webhooks/instagram'}:
                return self.send_html('Open the dashboard locally at http://127.0.0.1:8765', 403)
            if path == '/webhooks/instagram':
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                if query.get('hub.mode') == ['subscribe'] and secrets.compare_digest(query.get('hub.verify_token', [''])[0], webhook_verify_token()):
                    data = query.get('hub.challenge', [''])[0].encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                return self.send_html('Webhook verification failed', 403)
            if path == "/auth/instagram/start":
                redirect = urllib.parse.urlsplit(instagram_redirect_uri())
                if redirect.scheme != 'https' or not redirect.netloc:
                    return self.send_html('Configure an HTTPS Instagram redirect URL.', 503)
                if self.headers.get('Host', '') != redirect.netloc:
                    if self.headers.get('CF-Connecting-IP'):
                        return self.send_html('Start Instagram login from the local dashboard.', 403)
                    ticket = secrets.token_urlsafe(32)
                    with lock:
                        for key, expiry in list(OAUTH_TICKETS.items()):
                            if expiry < time.time():
                                del OAUTH_TICKETS[key]
                        OAUTH_TICKETS[ticket] = time.time()+120
                    self.send_response(302)
                    self.send_header('Location', f'{redirect.scheme}://{redirect.netloc}/auth/instagram/start?ticket={ticket}')
                    self.end_headers()
                    return
                ticket = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('ticket', [''])[0]
                with lock:
                    expiry = OAUTH_TICKETS.pop(ticket, 0)
                if expiry < time.time():
                    return self.send_html('Start Instagram login from the local dashboard.', 403)
                try:
                    state = secrets.token_urlsafe(32)
                    with lock:
                        for key, value in list(OAUTH_STATES.items()):
                            if value[0] < time.time():
                                del OAUTH_STATES[key]
                        OAUTH_STATES[state] = (time.time()+600, instagram_redirect_uri())
                    location = instagram_oauth_url(state)
                    self.send_response(302)
                    self.send_header('Set-Cookie', f'ig_oauth_state={state}; HttpOnly; SameSite=Lax; Path=/auth/instagram; Max-Age=600; Secure')
                    self.send_header("Location", location)
                    self.end_headers()
                except ValueError as error:
                    self.send_html(str(error), 500)
                return
            if path == "/auth/instagram/callback":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                state = query.get('state', [''])[0]
                cookie = SimpleCookie()
                cookie.load(self.headers.get('Cookie', ''))
                browser_state = cookie.get('ig_oauth_state')
                if not browser_state or not secrets.compare_digest(state, browser_state.value):
                    return self.send_html('Invalid Instagram OAuth state. Start login again.', 400)
                with lock:
                    saved = OAUTH_STATES.pop(state, None)
                if not saved or saved[0] < time.time() or saved[1] != instagram_redirect_uri():
                    return self.send_html('Login expired or redirect changed. Start login again.', 400)
                if query.get("error"):
                    return self.send_html(query.get("error_description", query["error"])[0], 400)
                try:
                    code = query.get("code", [""])[0]
                    if not code:
                        return self.send_html("Instagram did not return an authorization code.", 400)
                    token_data = long_lived_instagram_token(instagram_token(code))
                    path = save_instagram_token(token_data)
                    with lock:
                        start_monitor()
                    return self.send_html(f"Instagram connected. Token saved to {path.name}. You can close this window.")
                except (ValueError, KeyError) as error:
                    return self.send_html(str(error), 500)
            if self.path == "/api/events":
                return self.send_json(read_events(account['user_id']) if account.get('user_id') else [])
            if self.path == "/api/status":
                health = {}
                if account.get('user_id'):
                    health_path = account_path(account['user_id'], 'status.json')
                    if health_path.exists():
                        health = json.loads(health_path.read_text(encoding='utf-8'))
                return self.send_json({**health, "running": monitor is not None and monitor.poll() is None, "username": username})
            if self.path == "/api/account":
                return self.send_json({"username": username})
            if self.path == '/api/setup':
                return self.send_json({'callback_url': setting('IG_WEBHOOK_URL'),
                                       'redirect_url': instagram_redirect_uri(),
                                       'verify_token': webhook_verify_token()})
            return super().do_GET()

        def do_POST(self):
            if self.path != '/webhooks/instagram':
                return self.send_json({'error': 'Use Connect Instagram for authentication'}, 404)
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                return self.send_json({'error': 'Invalid size'}, 400)
            if not 0 < length <= 1024*1024:
                return self.send_json({'error': 'Invalid size'}, 413)
            body = self.rfile.read(length)
            def webhook_status(outcome, **details):
                write_json(ROOT / 'tools/accounts/webhook-status.json',
                           {'received_at': time.time(), 'outcome': outcome, **details})
            signature = 'sha256='+hmac.new(setting('IG_APP_SECRET').encode(), body, 'sha256').hexdigest()
            if not setting('IG_APP_SECRET') or not hmac.compare_digest(signature, self.headers.get('X-Hub-Signature-256', '')):
                webhook_status('invalid_signature')
                return self.send_json({'error': 'Invalid signature'}, 403)
            try:
                payload = json.loads(body)
                if payload.get('object') != 'instagram':
                    return self.send_json({'error': 'Invalid object'}, 400)
                items = list(webhook_items(payload))
                queued = 0
                for account_id, item in items:
                    if not account_path(account_id, 'instagram.json').exists():
                        continue
                    filename = hashlib.sha256(item['id'].encode()).hexdigest()+'.json'
                    write_json(account_path(account_id, 'inbox') / filename, item)
                    queued += 1
                webhook_status('accepted', parsed=len(items), queued=queued,
                               unmatched=len(items)-queued)
            except (ValueError, TypeError, AttributeError, KeyError):
                webhook_status('invalid_payload')
                return self.send_json({'error': 'Invalid payload'}, 400)
            return self.send_json({'received': True})

        def send_json(self, body, status=200):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_html(self, message, status=200):
            data = f"<h1>{html.escape(message)}</h1>".encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler, bind_and_activate=False)
    server.allow_reuse_address = False
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    server.server_bind()
    server.server_activate()
    start_monitor()
    print("Dashboard: http://127.0.0.1:8765")
    if not args.no_browser:
        webbrowser.open("http://127.0.0.1:8765")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_monitor()
        server.server_close()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        assert USERNAME.fullmatch("valid.user_1") and not USERNAME.fullmatch("invalid user")
        print("Self-test passed")
    else:
        main()
