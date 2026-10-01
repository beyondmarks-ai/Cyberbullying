"""Offline checks for failed-login diagnostics and credential redaction."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard


class OAuthDiagnosticsTests(unittest.TestCase):
    def test_login_requires_fresh_instagram_authentication_and_keeps_state(self):
        with patch.object(dashboard, 'setting', return_value='test-app'), \
             patch.object(dashboard, 'instagram_redirect_uri', return_value='https://example.com/callback'):
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(dashboard.instagram_oauth_url('csrf-state')).query)
        self.assertEqual(params['enable_fb_login'], ['false'])
        self.assertEqual(params['force_reauth'], ['true'])
        self.assertEqual(params['state'], ['csrf-state'])
        self.assertEqual(params['redirect_uri'], ['https://example.com/callback'])

    def test_token_without_basic_permission_is_rejected_without_logging_secrets(self):
        with patch.object(dashboard, 'setting', return_value='test-secret'), \
             patch.object(dashboard, 'instagram_request', return_value={
                 'access_token': 'private-token', 'user_id': '123',
                 'permissions': 'instagram_business_manage_comments'}), \
             patch.object(dashboard, 'write_json') as save:
            with self.assertRaisesRegex(ValueError, 'did not grant basic account access'):
                dashboard.instagram_token('private-code')
            logged = json.dumps(save.call_args.args[1])
            for secret in ('private-token', 'private-code', 'test-secret'):
                self.assertNotIn(secret, logged)

    def test_verified_profile_uses_documented_endpoint_and_account_id(self):
        response = io.BytesIO(json.dumps({'user_id': '987', 'username': 'tester'}).encode())
        with patch.object(dashboard.urllib.request, 'urlopen', return_value=response) as fetch, \
             patch.object(dashboard, 'write_json') as save:
            dashboard.save_instagram_token({'access_token': 'test-token', 'user_id': '123'})
            request = fetch.call_args.args[0]
            url = urllib.parse.urlsplit(request.full_url)
            self.assertEqual(url.path, '/v26.0/me')
            self.assertEqual(urllib.parse.parse_qs(url.query),
                             {'fields': ['user_id,username'], 'access_token': ['test-token']})
            self.assertEqual(save.call_args.args[0], dashboard.account_path('987', 'instagram.json'))
            self.assertEqual(save.call_args.args[1]['username'], 'tester')

    def test_profile_failure_has_meta_details_and_does_not_save_account(self):
        token = 'private/token+value'
        secret = 'private-secret'
        payload = {'error': {'message': 'Rejected ' + token + ' ' + secret + ' ' +
                            urllib.parse.quote(token, safe=''),
                            'code': 100, 'fbtrace_id': 'test-trace'}}
        error = urllib.error.HTTPError('https://graph.instagram.com/me', 400,
                                       'Bad Request', {}, io.BytesIO(json.dumps(payload).encode()))
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(dashboard, 'ROOT', Path(folder)), \
                 patch.object(dashboard, 'setting', return_value=secret), \
                 patch.object(dashboard.urllib.request, 'urlopen', side_effect=error), \
                 patch.object(dashboard, 'account_path') as account_path:
                with self.assertRaisesRegex(ValueError, 'Instagram profile lookup failed.*Meta code 100') as raised:
                    dashboard.save_instagram_token({'access_token': token, 'user_id': '123'})
                account_path.assert_not_called()
                saved = (Path(folder) / 'tools/accounts/oauth-error.json').read_text()
                diagnostic = json.loads(saved)
                self.assertEqual(diagnostic['trace_id'], 'test-trace')
                self.assertEqual(diagnostic['stage'], 'Instagram profile lookup')
                for value in (token, secret, urllib.parse.quote(token, safe='')):
                    self.assertNotIn(value, saved + str(raised.exception))

    def test_non_json_error_does_not_expose_response_body(self):
        error = urllib.error.HTTPError('https://graph.instagram.com/me', 502,
                                       'Bad Gateway', {}, io.BytesIO(b'<html>private-token</html>'))
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(dashboard, 'ROOT', Path(folder)), \
                 patch.object(dashboard.urllib.request, 'urlopen', side_effect=error):
                with self.assertRaisesRegex(ValueError, 'without a JSON explanation'):
                    dashboard.instagram_request(urllib.request.Request('https://graph.instagram.com/me'),
                                                'Instagram profile lookup')
                self.assertNotIn('private-token', (Path(folder) / 'tools/accounts/oauth-error.json').read_text())


if __name__ == '__main__':
    unittest.main()
