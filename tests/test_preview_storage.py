import base64
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from PIL import Image
from azure.core.exceptions import ResourceNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
import dashboard
from preview_storage import PreviewStore, media_type


def picture():
    data = io.BytesIO()
    Image.new('RGB', (16, 16), 'white').save(data, 'PNG')
    return data.getvalue()


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = 1000000000.0
        self.client = Mock()
        self.blob = self.client.get_blob_client.return_value
        settings = {'AZURE_STORAGE_ACCOUNT': 'teststorage', 'AZURE_STORAGE_CONTAINER': 'instagram-previews',
                    'AZURE_STORAGE_KEY': base64.b64encode(b'test-key-only').decode()}
        self.store = PreviewStore(settings.get, Path(self.folder.name) / 'index.sqlite3', self.client, lambda: self.now)

    def capture(self):
        return self.store.capture('account-1', 'event-1', 0, 'image', picture())

    def test_upload_is_private_and_retry_does_not_extend_retention(self):
        first = self.capture()
        self.now += 3600
        second = self.capture()
        self.assertEqual(first, second)
        self.blob.upload_blob.assert_called_once()
        self.assertFalse(self.blob.upload_blob.call_args.kwargs['overwrite'])
        self.assertEqual(self.blob.upload_blob.call_args.kwargs['content_settings'].content_type, 'image/png')
        self.assertNotIn('url', first)

    def test_only_owner_gets_short_lived_read_only_https_link(self):
        preview = self.capture()
        with self.assertRaises(KeyError):
            self.store.link('account-2', preview['id'])
        result = self.store.link('account-1', preview['id'])
        query = parse_qs(urlsplit(result['url']).query)
        self.assertEqual(query['sp'], ['r'])
        self.assertEqual(query['spr'], ['https'])
        self.assertEqual(result['link_expires_at'], self.now + 300)
        self.assertNotIn(self.store.key, result['url'])

    def test_expired_preview_never_reuploaded_or_signed(self):
        preview = self.capture()
        self.now += 7 * 86400 + 1
        self.assertEqual(self.store.link('account-1', preview['id'])['state'], 'expired')
        self.assertEqual(self.capture()['state'], 'expired')
        self.blob.upload_blob.assert_called_once()

    def test_delete_tombstone_prevents_resurrection(self):
        preview = self.capture()
        with self.assertRaises(KeyError):
            self.store.delete('account-2', preview['id'])
        self.store.delete('account-1', preview['id'])
        self.assertEqual(self.capture()['state'], 'deleted')
        self.assertNotIn('url', self.store.link('account-1', preview['id']))
        self.blob.upload_blob.assert_called_once()

    def test_deletion_during_upload_is_cleaned_up(self):
        def delete_while_uploading(*args, **kwargs):
            with self.store.db() as db:
                db.execute("UPDATE previews SET state='deleting'")
        self.blob.upload_blob.side_effect = delete_while_uploading
        self.assertEqual(self.capture()['state'], 'deleting')
        self.blob.delete_blob.assert_called_once()

    def test_failed_cloud_delete_can_be_retried_without_resurrection(self):
        preview = self.capture()
        self.blob.delete_blob.side_effect = RuntimeError('temporary failure')
        with self.assertRaises(RuntimeError):
            self.store.delete('account-1', preview['id'])
        self.assertEqual(self.capture()['state'], 'deleting')
        self.assertNotIn('url', self.store.link('account-1', preview['id']))
        self.blob.delete_blob.side_effect = None
        self.assertEqual(self.store.delete('account-1', preview['id'])['state'], 'deleted')

    def test_failed_upload_can_retry_without_resetting_expiry(self):
        self.blob.upload_blob.side_effect = RuntimeError('network')
        with self.assertRaises(RuntimeError):
            self.capture()
        self.now += 60
        self.blob.upload_blob.side_effect = None
        preview = self.capture()
        self.assertEqual(preview['expires_at'], 1000000000 + 7 * 86400)

    def test_deleted_in_azure_shows_expired(self):
        preview = self.capture()
        self.blob.get_blob_properties.side_effect = ResourceNotFoundError('gone')
        self.assertEqual(self.store.link('account-1', preview['id'])['state'], 'expired')

    def test_no_active_content_or_unknown_formats(self):
        for kind in ('image', 'audio', 'video'):
            with self.assertRaises((ValueError, OSError)):
                media_type(b'<svg onload="bad()"/>', kind)

    def test_disabled_storage_keeps_monitor_usable(self):
        store = PreviewStore(lambda key, default='': default)
        self.assertEqual(store.capture('a', 'e', 0, 'image', picture())['state'], 'disabled')

    def test_preview_route_rejects_cross_site_tunnel_and_rebinding(self):
        valid = {'Host': '127.0.0.1:8765', 'X-Preview-Request': '1'}
        for extra in ({'Origin': 'https://evil.example'}, {'Host': 'evil.example'},
                      {'CF-Connecting-IP': '1.2.3.4'}, {'Sec-Fetch-Site': 'cross-site'}, {'X-Preview-Request': ''}):
            for method in ('GET', 'DELETE'):
                _, status = dashboard.preview_action({**valid, **extra}, {'user_id': 'a'}, '/api/previews/' + 'a' * 64, method)
                self.assertEqual(status, 403)

    def test_preview_route_scopes_to_current_account_and_sanitizes_errors(self):
        headers = {'Host': '127.0.0.1:8765', 'X-Preview-Request': '1'}
        with patch.object(dashboard, 'PreviewStore') as constructor:
            constructor.return_value.link.return_value = {'state': 'expired'}
            self.assertEqual(dashboard.preview_action(headers, {'user_id': 'a'}, '/api/previews/' + 'b' * 64)[1], 200)
            constructor.return_value.link.assert_called_once_with('a', 'b' * 64)
            constructor.return_value.link.side_effect = RuntimeError('secret SAS token')
            body, status = dashboard.preview_action(headers, {'user_id': 'a'}, '/api/previews/' + 'b' * 64)
            self.assertEqual(status, 503)
            self.assertNotIn('secret', str(body))


if __name__ == '__main__':
    unittest.main()
