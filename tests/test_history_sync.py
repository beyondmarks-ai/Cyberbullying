import asyncio
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
import dashboard
import history_sync as history
import instagram_store
from instagram_graph import InstagramGraph, InstagramGraphError


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(instagram_store, 'ACCOUNTS', Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.account = {'user_id': 'owner', 'username': 'owner_name', 'access_token': 'test-token'}

    async def test_pagination_deduplicates_and_does_not_follow_next_url(self):
        graph = InstagramGraph('test-token', 'owner')
        self.addAsyncCleanup(graph.close)
        graph._get = AsyncMock(side_effect=[
            {'data': [{'id': '1'}, {'id': '2'}], 'paging': {'next': 'https://evil.invalid/?token=secret', 'cursors': {'after': 'a'}}},
            {'data': [{'id': '2'}, {'id': '3'}]},
        ])
        rows, more = await graph.history_pages('/owner/conversations', 3)
        self.assertEqual([r['id'] for r in rows], ['1', '2', '3'])
        self.assertFalse(more)
        self.assertEqual(graph._get.call_args.args, ('/owner/conversations',))
        self.assertEqual(graph._get.call_args.kwargs['after'], 'a')

    async def test_repeated_cursor_and_limit_are_bounded(self):
        graph = InstagramGraph('test-token', 'owner')
        self.addAsyncCleanup(graph.close)
        graph._get = AsyncMock(return_value={'data': [{'id': '1'}], 'paging': {'next': 'yes', 'cursors': {'after': 'a'}}})
        rows, more = await graph.history_pages('/path', 5)
        self.assertTrue(more)
        self.assertEqual(len(rows), 1)
        self.assertEqual(graph._get.call_count, 2)
        graph._get.reset_mock()
        await graph.history_pages('/path', 1)
        self.assertEqual(graph._get.call_count, 1)

    async def test_non_json_error_does_not_expose_response(self):
        graph = InstagramGraph('secret-token', 'owner')
        self.addAsyncCleanup(graph.close)
        graph.http.get = AsyncMock(return_value=httpx.Response(502, text='secret-token upstream'))
        with self.assertRaises(InstagramGraphError) as raised:
            await graph._get('/path')
        self.assertNotIn('secret-token', str(raised.exception))

    async def test_empty_final_page_with_next_is_exhausted(self):
        graph = InstagramGraph('test-token', 'owner')
        self.addAsyncCleanup(graph.close)
        paging = {'next': 'yes', 'cursors': {'after': 'a'}}
        graph._get = AsyncMock(side_effect=[{'data': [{'id': '1'}], 'paging': paging}, {'data': [], 'paging': paging}])
        rows, more = await graph.history_pages('/path', 50)
        self.assertEqual(len(rows), 1)
        self.assertFalse(more)

    def test_normalization_preserves_original_and_missing_attachment_slots(self):
        message = {'id': 'message', 'created_time': '2026-10-01T10:00:00+0000', 'message': 'original',
                   'from': {'id': 'alternate-id', 'username': 'OWNER_NAME'},
                   'attachments': {'data': [{'image_data': {}}, {'video_data': {'url': 'https://cdn.fbsbx.com/test'}}]}}
        conversation = {'id': 'chat', 'participants': {'data': [{'id': 'owner'}, {'id': 'other', 'username': 'other'}]}}
        event, assets = history.history_event(message, conversation, self.account)
        self.assertEqual(event['direction'], 'outgoing')
        self.assertEqual(event['source'], 'You')
        self.assertEqual(event['original_text'], 'original')
        self.assertIsNone(event['analysis'])
        self.assertEqual(assets[0], ('image', None))
        self.assertNotIn('https', json.dumps(event))

    async def test_deleted_preview_is_never_downloaded_again(self):
        store = Mock(enabled=True)
        store.find.return_value = {'id': 'id', 'state': 'deleted', 'kind': 'image'}
        progress = {'previews_available': 0, 'previews_unavailable': 0}
        with patch.object(history, 'download_attachment', new_callable=AsyncMock) as download:
            previews = await history.backfill_previews(store, None, 'owner', 'dm:m', [('image', 'url')], progress)
        download.assert_not_called()
        store.capture.assert_not_called()
        self.assertEqual(previews[0]['state'], 'deleted')
        self.assertEqual(progress['previews_unavailable'], 1)

    async def test_unavailable_attachment_does_not_hide_other_previews(self):
        store = Mock(enabled=True)
        store.find.return_value = None
        store.capture.return_value = {'id': 'id', 'kind': 'image', 'state': 'available'}
        progress = {'previews_available': 0, 'previews_unavailable': 0}
        request = httpx.Request('GET', 'https://cdn.fbsbx.com/expired')
        failure = httpx.HTTPStatusError('private-url', request=request, response=httpx.Response(403, request=request))
        with patch.object(history, 'download_attachment', new_callable=AsyncMock, side_effect=[failure, b'image']):
            result = await history.backfill_previews(store, None, 'owner', 'dm:m', [('image', 'url1'), ('image', 'url2')], progress)
        self.assertEqual([p['state'] for p in result], ['unavailable', 'available'])
        self.assertNotIn('private-url', json.dumps(result))
        self.assertEqual(progress, {'previews_available': 1, 'previews_unavailable': 1})

    async def test_download_rejects_external_redirect(self):
        def handler(request):
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ValueError):
                await history.download_attachment(client, 'https://cdn.fbsbx.com/test')
        self.assertFalse(history.allowed_media_url('https://fbsbx.com.evil.invalid/file'))
        self.assertFalse(history.allowed_media_url('https://user:pass@cdn.fbsbx.com/file'))

    def test_merge_preserves_latest_analysis_and_history_metadata(self):
        instagram_store.write_json(instagram_store.account_path('owner', 'history.json'), {'events': {
            'dm:m': {'id': 'dm:m', 'time': '2026-10-01T00:00:00+00:00', 'original_text': 'old',
                     'analysis': None, 'conversation_id': 'c', 'previews': [{'id': 'preview'}]},
            'dm:n': {'id': 'dm:n', 'time': '2026-10-02T00:00:00+00:00'}}, 'conversations': []})
        with patch.object(dashboard, 'read_live_events', return_value={'dm:m': {
            'id': 'dm:m', 'time': '2026-10-02', 'analysis': {'bullying': True}, 'previews': []}}):
            events = dashboard.read_events('owner')
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]['analysis'], {'bullying': True})
        self.assertEqual(events[1]['previews'], [{'id': 'preview'}])
        self.assertEqual(events[1]['conversation_id'], 'c')

    def test_history_endpoint_guards(self):
        headers = {'Host': '127.0.0.1:8765', 'X-History-Request': '1'}
        manager = Mock()
        manager.start.return_value = {'state': 'running'}
        self.assertEqual(dashboard.history_action(headers, self.account, manager, 'POST')[1], 202)
        for extra in ({'Host': 'evil.invalid'}, {'CF-Connecting-IP': '1.2.3.4'}, {'Origin': 'https://evil.invalid'},
                      {'Sec-Fetch-Site': 'cross-site'}, {'X-History-Request': ''}):
            self.assertEqual(dashboard.history_action({**headers, **extra}, self.account, manager, 'POST')[1], 403)
        self.assertEqual(dashboard.history_action(headers, {}, manager)[1], 401)
        manager.start.assert_called_once()

    def test_manager_single_flight_and_account_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        seen = []
        async def runner(account, getter, progress, save):
            entered.set()
            release.wait(3)
            seen.append(account['user_id'])
        manager = history.HistorySyncManager(lambda *args: '', runner)
        manager.start(self.account)
        self.assertTrue(entered.wait(2))
        manager.start(self.account)
        self.account['user_id'] = 'different'
        release.set()
        manager.threads['owner'].join(3)
        self.assertEqual(seen, ['owner'])
        self.assertEqual(manager.status('owner')['state'], 'complete')
        self.assertEqual(manager.status('different')['state'], 'idle')

    async def test_import_is_idempotent_and_keeps_assessment(self):
        graph = Mock()
        graph.close = AsyncMock()
        graph.history_pages = AsyncMock(side_effect=lambda path, *args, **kwargs:
            ([{'id': 'chat', 'participants': {'data': []}}], False) if path.endswith('/conversations') else
            ([{'id': 'm', 'created_time': '2026-10-01T00:00:00Z', 'message': 'test'}], False))
        with patch.object(history, 'InstagramGraph', return_value=graph), patch.object(history, 'PreviewStore'), \
                patch.object(history, 'read_live_events', return_value={'dm:m': {'analysis': {'bullying': True}}}):
            for _ in range(2):
                progress = {'messages': 0, 'conversations': 0, 'warnings': [], 'previews_available': 0, 'previews_unavailable': 0}
                await history.sync_history(self.account, None, progress, lambda: None)
        events = history.read_history('owner')['events']
        self.assertEqual(list(events), ['dm:m'])
        self.assertTrue(events['dm:m']['analysis']['bullying'])
        self.assertFalse(instagram_store.account_path('owner', 'events.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
