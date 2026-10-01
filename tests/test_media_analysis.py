import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
import comment_monitor as monitor
from azure_moderation import GeminiVideoModerator, normalize


def result(bullying=False):
    return {'bullying': bullying, 'confidence': .8, 'severity': 'high' if bullying else 'none',
            'reason': 'Test classification', 'categories': [], 'content_summary': 'Test image content'}


def response_for(answer, finish='STOP'):
    return httpx.Response(200, json={'candidates': [{'finishReason': finish,
                               'content': {'parts': [{'text': answer}]}}]})


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_request_retries_incomplete_json_and_preserves_summary(self):
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                # The observed service returned STOP even though its JSON was cut off.
                return response_for('{"bullying": false')
            return response_for(json.dumps(result()))

        image = io.BytesIO()
        Image.new('RGB', (20, 20), 'white').save(image, 'PNG')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            model = GeminiVideoModerator(http)
            with patch.object(model, '_credentials', return_value='test-token'):
                analysis = await model.analyze_images([image.getvalue()])
        self.assertEqual([r['generationConfig']['maxOutputTokens'] for r in requests], [1600, 3200])
        self.assertEqual(requests[0]['contents'][0]['parts'][0]['inlineData']['mimeType'], 'image/jpeg')
        self.assertIn('responseSchema', requests[0]['generationConfig'])
        self.assertEqual(analysis['content_summary'], 'Test image content')
        self.assertTrue(analysis['analyzer'].startswith('Vertex /'))

    async def test_repeated_invalid_output_is_not_classified_safe(self):
        count = 0

        def respond(request):
            nonlocal count
            count += 1
            return response_for('{}')

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            model = GeminiVideoModerator(http)
            with patch.object(model, '_credentials', return_value='test-token'):
                with self.assertRaisesRegex(ValueError, 'invalid analysis after retry'):
                    await model._generate([{'text': 'test'}])
        self.assertEqual(count, 2)

    async def test_media_pipeline_records_vertex_result_and_fallback(self):
        azure, gemini, speech = AsyncMock(), AsyncMock(), AsyncMock()
        azure.download.return_value = b'image-data'
        gemini.analyze_images.return_value = {**result(True), 'analyzer': 'Vertex / test'}
        azure.analyze.return_value = {**result(False), 'analyzer': 'Azure / test'}
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(monitor, 'EVENT_FILE', Path(folder) / 'events.jsonl'):
                self.assertTrue(await monitor.moderate(azure, speech, gemini, 'image', 'test', '',
                                                       [('https://example.com/test.png', 'image')], 'image-1'))
                azure.analyze.assert_not_awaited()
                gemini.analyze_images.side_effect = ValueError('Vertex unavailable')
                self.assertTrue(await monitor.moderate(azure, speech, gemini, 'image', 'test', '',
                                                       [('https://example.com/test.png', 'image')], 'image-2'))
                saved = [json.loads(line) for line in monitor.EVENT_FILE.read_text().splitlines()]
        self.assertEqual(saved[0]['analysis']['content_summary'], 'Test image content')
        self.assertTrue(saved[0]['analysis']['bullying'])
        self.assertIn('Azure image fallback', saved[1]['analysis']['reason'])

    async def test_video_pipeline_keeps_native_analysis_without_speech_service(self):
        azure, gemini, speech = AsyncMock(), AsyncMock(), AsyncMock()
        azure.download.return_value = b'video-data'
        speech.transcribe.side_effect = RuntimeError('Speech service unavailable')
        gemini.analyze.return_value = {**result(True), 'analyzer': 'Vertex / test'}
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(monitor, 'EVENT_FILE', Path(folder) / 'events.jsonl'):
                self.assertTrue(await monitor.moderate(azure, speech, gemini, 'video', 'test', '',
                                                       [('https://example.com/test.mp4', 'video')], 'video-1'))
                saved = json.loads(monitor.EVENT_FILE.read_text())
        gemini.analyze.assert_awaited_once()
        azure.analyze.assert_not_awaited()
        self.assertIn('transcription unavailable', saved['analysis']['coverage'])
        self.assertTrue(saved['analysis']['bullying'])

    def test_neutral_result_has_no_bullying_severity(self):
        self.assertEqual(normalize({**result(), 'severity': 'low'})['severity'], 'none')
        with self.assertRaises(ValueError):
            normalize({})

    def test_project_endpoint_overrides_an_unrelated_inherited_endpoint(self):
        with patch.object(Path, 'read_text', return_value='AZURE_OPENAI_ENDPOINT=https://project.example\n'), \
             patch.dict(os.environ, {'AZURE_OPENAI_ENDPOINT': 'https://other.example'}):
            monitor.load_dotenv()
            self.assertEqual(os.environ['AZURE_OPENAI_ENDPOINT'], 'https://project.example')


if __name__ == '__main__':
    unittest.main()
