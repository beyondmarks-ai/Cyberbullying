import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
import comment_monitor as monitor
import instagram_store as store
from instagram_graph import InstagramGraph


async def check():
    client = InstagramGraph('test-token', '123')
    await client.http.aclose()
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers['Authorization'] == 'Bearer test-token'
        assert 'access_token' not in request.url.params
        if request.url.params.get('after') == 'page2':
            return httpx.Response(200, json={'data': [{'id': 'comment-1', 'text': 'Hello'}]})
        return httpx.Response(200, json={'data': [], 'paging': {
            'next': 'https://graph.instagram.com/123/comments?after=page2',
            'cursors': {'after': 'page2'}}})

    client.http = httpx.AsyncClient(base_url='https://graph.instagram.com',
                                   headers={'Authorization': 'Bearer test-token'},
                                   transport=httpx.MockTransport(respond))
    assert (await client.comments('123'))[0]['id'] == 'comment-1'
    assert len(calls) == 2 and calls[0].url.params['limit'] == '50'
    await client.close()

    payload = {'object': 'instagram', 'entry': [{'id': '123', 'changes': [
        {'field': 'comments', 'value': {'id': 'comment-1', 'text': 'Hello', 'from': {'username': 'tester'}}}],
        'messaging': [{'sender': {'id': '456'}, 'message': {'mid': 'dm1', 'text': 'Hi'}},
                      {'sender': {'id': '123'}, 'message': {'mid': 'echo', 'is_echo': True}}]}]}
    items = list(store.webhook_items(payload))
    assert len(items) == 2 and items[0][1]['id'] == 'comment:comment-1'
    assert items[1][1]['kind'] == 'dm'
    payload['entry'][0]['changes'][0]['value']['from'] = None
    assert next(store.webhook_items(payload))[1]['source'] == '@unknown'
    with tempfile.TemporaryDirectory() as folder:
        monitor.EVENT_FILE = Path(folder) / 'events.jsonl'
        classifier = AsyncMock()
        classifier.analyze.side_effect = RuntimeError('Provider unavailable')
        item = items[0][1]
        assert not await monitor.moderate(classifier, None, None, item['kind'], item['source'],
                                         item['text'], event_id=item['id'])
        event = json.loads(monitor.EVENT_FILE.read_text())
        assert event['text'] == 'Hello' and event['analysis']['severity'] == 'unknown'
        assert event['id'] == 'comment:comment-1'
    print('PASS: pagination, bearer auth, webhook comments/DMs, echo filtering, failed-analysis visibility')


asyncio.run(check())
