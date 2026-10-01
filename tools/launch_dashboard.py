"""Start a local dashboard and its matching Cloudflare tunnel (standard library only)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
LOCAL_URL = 'http://127.0.0.1:8765'


def url_settings(origin):
    parsed = urllib.parse.urlsplit(origin)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Public URL must be an HTTPS origin, such as https://ig.example.com')
    origin = origin.rstrip('/')
    return {'IG_REDIRECT_URI': origin + '/auth/instagram/callback',
            'IG_WEBHOOK_URL': origin + '/webhooks/instagram'}


def update_env(path, values):
    """Replace just the two tunnel settings atomically, retaining other lines and secrets."""
    original = path.read_bytes()
    source = original.decode('utf-8-sig')
    newline = '\r\n' if '\r\n' in source else '\n'
    updated, seen = [], set()
    for line in source.splitlines():
        key, sep, _ = line.partition('=')
        key = key.strip()
        if sep and key in values:
            if key not in seen:
                updated.append(f'{key}={values[key]}')
                seen.add(key)
        else:
            updated.append(line)
    updated.extend(f'{key}={value}' for key, value in values.items() if key not in seen)
    content = (newline.join(updated) + newline).encode('utf-8')
    if original.startswith(b'\xef\xbb\xbf'):
        content = b'\xef\xbb\xbf' + content
    if content == original:
        return False
    fd, name = tempfile.mkstemp(prefix='.env.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return True


def request(url):
    return urllib.request.Request(url, headers={'User-Agent': 'InstagramSafetyMonitor-Launcher/1.0'})


def find_cloudflared():
    installed = shutil.which('cloudflared')
    if installed:
        return installed
    if os.name != 'nt':
        raise RuntimeError('Install cloudflared on this system and add it to PATH first.')
    # Also recognize the per-user location used by the previous manual installation.
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        existing = Path(local_app_data) / 'cloudflared/cloudflared.exe'
        if existing.is_file():
            return str(existing)
    binary = RUNTIME / 'bin/cloudflared.exe'
    if binary.is_file():
        return str(binary)
    print('Downloading Cloudflare Tunnel from the official GitHub release...', flush=True)
    with urllib.request.urlopen(request('https://api.github.com/repos/cloudflare/cloudflared/releases/latest'), timeout=30) as response:
        release = json.load(response)
    architecture = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'amd64'
    name = f'cloudflared-windows-{architecture}.exe'
    asset = next((item for item in release.get('assets', []) if item['name'] == name), None)
    if not asset or not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', asset.get('digest') or ''):
        raise RuntimeError('Cloudflare did not provide a verified download for this PC. Install cloudflared manually.')
    url = asset['browser_download_url']
    if not url.startswith('https://github.com/cloudflare/cloudflared/releases/download/'):
        raise RuntimeError('Unexpected Cloudflare download URL.')
    binary.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    fd, filename = tempfile.mkstemp(dir=binary.parent, suffix='.download')
    try:
        with os.fdopen(fd, 'wb') as target:
            with urllib.request.urlopen(request(url), timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    target.write(chunk)
        if digest.hexdigest() != asset['digest'].split(':', 1)[1].lower():
            raise RuntimeError('Cloudflare download checksum failed. The file will not be executed.')
        os.replace(filename, binary)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)
    return str(binary)


def start_child(args, logfile, environment=None):
    with logfile.open('w', encoding='utf-8') as output:
        return subprocess.Popen(args, cwd=ROOT, env=environment, stdout=output,
                                stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()


def wait_for_tunnel(child, logfile, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f'Cloudflare exited. See {logfile}')
        match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com\b', logfile.read_text(errors='replace'))
        if match:
            return match.group()
        time.sleep(.5)
    raise RuntimeError(f'Cloudflare did not publish a URL within {timeout} seconds. See {logfile}')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def redirect_location(opener, url):
    try:
        with opener.open(url, timeout=8):
            raise RuntimeError('Expected a login redirect but received a page instead.')
    except urllib.error.HTTPError as error:
        try:
            if error.code != 302 or not error.headers.get('Location'):
                raise RuntimeError(f'Login bridge returned HTTP {error.code}.') from None
            return error.headers['Location']
        finally:
            error.close()


def verify_bridge(origin):
    """A one-use ticket proves the tunnel reaches this dashboard, without contacting Instagram."""
    opener = urllib.request.build_opener(NoRedirect())
    public_start = redirect_location(opener, LOCAL_URL + '/auth/instagram/start')
    expected = urllib.parse.urlsplit(origin)
    actual = urllib.parse.urlsplit(public_start)
    if (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, '/auth/instagram/start'):
        raise RuntimeError('The local dashboard is using a different tunnel URL.')
    instagram = urllib.parse.urlsplit(redirect_location(opener, public_start))
    params = urllib.parse.parse_qs(instagram.query)
    if (instagram.scheme != 'https' or instagram.netloc != 'www.instagram.com'
            or instagram.path != '/oauth/authorize'
            or params.get('redirect_uri') != [url_settings(origin)['IG_REDIRECT_URI']]):
        raise RuntimeError('The tunnel did not return the expected Instagram login redirect.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local-only', action='store_true', help='Use an already running external tunnel; do not change .env')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    python = ROOT / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
    if not python.exists():
        raise RuntimeError('Create the Python environment and install requirements.txt first (see README).')
    check = subprocess.run([str(python), '-c', 'import httpx; import PIL'], capture_output=True)
    if check.returncode:
        raise RuntimeError('Install dependencies with .venv\\Scripts\\python.exe -m pip install -r requirements.txt')
    try:
        with socket.create_connection(('127.0.0.1', 8765), timeout=1):
            raise RuntimeError('A dashboard is already running on port 8765. Stop its launcher with Ctrl+C first. No settings changed.')
    except (ConnectionRefusedError, socket.timeout):
        pass
    env_file = ROOT / '.env'
    if not env_file.exists():
        shutil.copyfile(ROOT / '.env.example', env_file)
        raise RuntimeError('Created .env from the template. Fill in the Instagram and AI credentials, then run again.')
    sys.path.insert(0, str(ROOT))
    from dashboard import setting
    if not setting('IG_APP_ID') or not setting('IG_APP_SECRET'):
        raise RuntimeError('Fill in IG_APP_ID and IG_APP_SECRET in .env first. See Instagram Business login settings in Meta.')
    RUNTIME.mkdir(exist_ok=True)
    tunnel = dashboard = None
    try:
        environment = os.environ.copy()
        if not args.local_only:
            executable = find_cloudflared()
            logfile = RUNTIME / 'tunnel.log'
            tunnel = start_child([executable, 'tunnel', '--no-autoupdate', '--url', LOCAL_URL], logfile)
            print('Starting a tunnel for this PC...', flush=True)
            origin = wait_for_tunnel(tunnel, logfile)
            values = url_settings(origin)
            update_env(env_file, values)
            environment.update(values)  # Override any inherited URL from another installation.
            print('\nUpdated .env automatically. Meta still needs these exact URLs:', flush=True)
            print('Instagram Business login redirect URL: ' + values['IG_REDIRECT_URI'], flush=True)
            print('Webhook callback URL: ' + values['IG_WEBHOOK_URL'], flush=True)
            print('In Meta, save the redirect URL and verify the webhook callback; enable comments/messages.', flush=True)
            print('The verify token is in the dashboard under Admin setup. Never copy your app secret there.\n', flush=True)
        dashboard = start_child([str(python), str(ROOT / 'dashboard.py'), '--no-browser'], RUNTIME / 'dashboard.log', environment)
        deadline = time.monotonic() + 90
        print('Checking the local dashboard and login bridge...', flush=True)
        last_notice = time.monotonic()
        while True:
            if dashboard.poll() is not None or (tunnel and tunnel.poll() is not None):
                raise RuntimeError(f'A service exited during startup. See logs in {RUNTIME}')
            try:
                with urllib.request.urlopen(LOCAL_URL + '/api/status', timeout=5) as response:
                    json.load(response)
                if tunnel:
                    verify_bridge(origin)
                break
            except (OSError, ValueError, RuntimeError):
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'The dashboard/tunnel did not become ready. See logs in {RUNTIME}') from None
                if time.monotonic() - last_notice > 15:
                    print('Still waiting for the public tunnel to become reachable...', flush=True)
                    last_notice = time.monotonic()
                time.sleep(1)
        print('Ready: ' + LOCAL_URL, flush=True)
        print('Keep this window open. Press Ctrl+C here to stop the services started by this launcher.', flush=True)
        if not args.no_browser:
            webbrowser.open(LOCAL_URL + ('/?setup=1' if tunnel else '/'))
        while dashboard.poll() is None and (tunnel is None or tunnel.poll() is None):
            time.sleep(1)
        raise RuntimeError(f'A service stopped. See logs in {RUNTIME}')
    except KeyboardInterrupt:
        print('\nStopping the dashboard and tunnel...', flush=True)
    finally:
        stop_child(dashboard)
        stop_child(tunnel)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Startup failed: {error}', file=sys.stderr)
        sys.exit(1)
