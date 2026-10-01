import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launch_dashboard as launcher
import dashboard


class LauncherTests(unittest.TestCase):
    def test_update_preserves_credentials_comments_and_crlf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_bytes(b'# comment\r\nIG_APP_SECRET="private=value"\r\n'
                             b'IG_REDIRECT_URI=https://old/callback\r\n'
                             b'IG_REDIRECT_URI=https://duplicate/callback\r\nSARVAM_API_KEY=private\r\n')
            values = launcher.url_settings('https://new.trycloudflare.com/')
            self.assertTrue(launcher.update_env(path, values))
            content = path.read_bytes()
            self.assertIn(b'IG_APP_SECRET="private=value"\r\n', content)
            self.assertIn(b'SARVAM_API_KEY=private\r\n', content)
            self.assertEqual(content.count(b'IG_REDIRECT_URI='), 1)
            self.assertIn(b'IG_WEBHOOK_URL=https://new.trycloudflare.com/webhooks/instagram\r\n', content)
            self.assertFalse(launcher.update_env(path, values))

    def test_only_https_origins_are_allowed(self):
        for url in ('http://example.com', 'https://user:secret@example.com',
                    'https://example.com/path', 'https://example.com?x=1'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                launcher.url_settings(url)

    def test_bridge_rejects_another_pc(self):
        opener = Mock()
        first = urllib.error.HTTPError('http://localhost', 302, 'Found',
                                      {'Location': 'https://other.trycloudflare.com/auth/instagram/start?ticket=abc'}, io.BytesIO())
        opener.open.side_effect = first
        with patch.object(launcher.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(RuntimeError, 'different tunnel'):
                launcher.verify_bridge('https://correct.trycloudflare.com')
        self.assertEqual(opener.open.call_count, 1)

    def test_bridge_checks_returned_callback_without_contacting_instagram(self):
        opener = Mock()
        origin = 'https://correct.trycloudflare.com'
        first = urllib.error.HTTPError('http://localhost', 302, 'Found',
                                      {'Location': origin + '/auth/instagram/start?ticket=abc'}, io.BytesIO())
        oauth = 'https://www.instagram.com/oauth/authorize?redirect_uri=' + origin + '/auth/instagram/callback'
        second = urllib.error.HTTPError(origin, 302, 'Found', {'Location': oauth}, io.BytesIO())
        opener.open.side_effect = [first, second]
        with patch.object(launcher.urllib.request, 'build_opener', return_value=opener):
            launcher.verify_bridge(origin)
        self.assertEqual(opener.open.call_count, 2)

    def test_launcher_passes_updated_urls_and_stops_only_its_children(self):
        tunnel, server = Mock(), Mock()
        tunnel.poll.return_value = None
        server.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
            python.parent.mkdir(parents=True)
            python.touch()
            (root / '.env').write_text('IG_APP_SECRET=private\n')
            with patch.object(launcher, 'ROOT', root), \
                 patch.object(launcher, 'RUNTIME', root / '.runtime'), \
                 patch.object(sys, 'argv', ['launcher', '--no-browser']), \
                 patch.object(launcher.subprocess, 'run', return_value=Mock(returncode=0)), \
                 patch.object(launcher.socket, 'create_connection', side_effect=ConnectionRefusedError), \
                 patch.object(dashboard, 'setting', return_value='configured'), \
                 patch.object(launcher, 'find_cloudflared', return_value='cloudflared'), \
                 patch.object(launcher, 'start_child', side_effect=[tunnel, server]) as start, \
                 patch.object(launcher, 'wait_for_tunnel', return_value='https://new.trycloudflare.com'), \
                 patch.object(launcher.urllib.request, 'urlopen', return_value=io.BytesIO(b'{}')), \
                 patch.object(launcher, 'verify_bridge') as verify, \
                 patch.object(launcher.time, 'sleep', side_effect=KeyboardInterrupt), \
                 patch.object(launcher, 'stop_child') as stop:
                launcher.main()
            environment = start.call_args_list[1].args[2]
            self.assertEqual(environment['IG_REDIRECT_URI'], 'https://new.trycloudflare.com/auth/instagram/callback')
            self.assertIn('IG_APP_SECRET=private', (root / '.env').read_text())
            verify.assert_called_once_with('https://new.trycloudflare.com')
            self.assertEqual([c.args[0] for c in stop.call_args_list], [server, tunnel])

    @unittest.skipUnless(os.name == 'nt', 'Windows automatic installer')
    def test_download_checksum_failure_leaves_no_executable(self):
        payload = b'untrusted-binary'
        metadata = {'assets': [{'name': 'cloudflared-windows-amd64.exe',
                               'digest': 'sha256:' + hashlib.sha256(b'other-binary').hexdigest(),
                               'browser_download_url': 'https://github.com/cloudflare/cloudflared/releases/download/test/cloudflared-windows-amd64.exe'}]}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(launcher, 'RUNTIME', Path(directory)), \
                 patch.object(launcher.shutil, 'which', return_value=None), \
                 patch.object(launcher.platform, 'machine', return_value='AMD64'), \
                 patch.dict(os.environ, {'LOCALAPPDATA': directory}), \
                 patch.object(launcher.urllib.request, 'urlopen', side_effect=[
                     io.BytesIO(json.dumps(metadata).encode()), io.BytesIO(payload)]):
                with self.assertRaisesRegex(RuntimeError, 'checksum failed'):
                    launcher.find_cloudflared()
                self.assertFalse((Path(directory) / 'bin/cloudflared.exe').exists())
                self.assertEqual(list((Path(directory) / 'bin').glob('*.download')), [])


if __name__ == '__main__':
    unittest.main()
