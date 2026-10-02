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
from azure_moderation import AzureModerator, GeminiVideoModerator, SarvamTranscriber, normalize


def result(bullying=False):
    return {'bullying': bullying, 'confidence': .8, 'severity': 'high' if bullying else 'none',
            'reason': 'Test classification', 'categories': [], 'content_summary': 'Test image content',
            'needs_review': False, 'languages': ['English'], 'evidence': ['Test evidence'], 'uncertainties': [],
            'status': 'bullying' if bullying else 'clear'}


def response_for(answer, finish='STOP'):
    return httpx.Response(200, json={'candidates': [{'finishReason': finish,
                               'content': {'parts': [{'text': answer}]}}]})


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            'GEMINI_API_PROVIDER': 'gemini', 'GEMINI_API_KEY': 'test-google-key',
            'GEMINI_MODEL': 'gemini-2.5-flash', 'AZURE_OPENAI_KEY': 'test-azure-key',
            'AZURE_OPENAI_ENDPOINT': 'https://test.openai.azure.com',
            'AZURE_OPENAI_DEPLOYMENT': 'test-deployment', 'SARVAM_API_KEY': 'test-sarvam-key',
        })
        env.start()
        self.addCleanup(env.stop)

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
            analysis = await model.analyze_images([image.getvalue()])
        self.assertEqual([r['generationConfig']['maxOutputTokens'] for r in requests], [1600, 3200])
        self.assertEqual(requests[0]['contents'][0]['parts'][0]['inlineData']['mimeType'], 'image/jpeg')
        self.assertIn('responseSchema', requests[0]['generationConfig'])
        self.assertEqual(analysis['content_summary'], 'Test image content')
        self.assertTrue(analysis['analyzer'].startswith('Gemini /'))

    async def test_repeated_invalid_output_is_not_classified_safe(self):
        count = 0

        def respond(request):
            nonlocal count
            count += 1
            return response_for('{}')

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            model = GeminiVideoModerator(http)
            with self.assertRaisesRegex(ValueError, 'invalid analysis after retry'):
                await model._generate([{'text': 'test'}])
        self.assertEqual(count, 2)

    async def test_google_api_key_routing_without_cli(self):
        for provider, host, path in (
            ('gemini', 'generativelanguage.googleapis.com', '/v1beta/models/'),
            ('vertex-express', 'aiplatform.googleapis.com', '/v1/publishers/google/models/'),
        ):
            def respond(request):
                self.assertEqual(request.url.host, host)
                self.assertEqual(request.url.path, path + 'gemini-2.5-flash:generateContent')
                self.assertEqual(request.headers['x-goog-api-key'], 'test-google-key')
                self.assertNotIn('authorization', request.headers)
                self.assertNotIn('test-google-key', str(request.url))
                self.assertNotIn('test-google-key', request.content.decode())
                return response_for(json.dumps(result()))

            with self.subTest(provider=provider), patch.dict(os.environ, {'GEMINI_API_PROVIDER': provider}), \
                 patch('azure_moderation.subprocess.run', side_effect=AssertionError('CLI invoked')):
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                    self.assertEqual((await GeminiVideoModerator(http)._generate([{'text': 'test'}]))['severity'], 'none')

    async def test_azure_uses_configured_key_without_cli(self):
        def respond(request):
            self.assertEqual(str(request.url).split('?')[0],
                             'https://test.openai.azure.com/openai/deployments/test-deployment/chat/completions')
            self.assertEqual(request.headers['api-key'], 'test-azure-key')
            self.assertNotIn('test-azure-key', str(request.url))
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result())}}]})

        model = AzureModerator()
        await model.http.aclose()
        with patch('azure_moderation.subprocess.run', side_effect=AssertionError('CLI invoked')):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                model.http = http
                self.assertEqual((await model.analyze('test'))['severity'], 'none')

    async def test_missing_keys_fail_without_cli_or_network(self):
        with patch.dict(os.environ, {'AZURE_OPENAI_KEY': '', 'GEMINI_API_KEY': ''}), \
             patch('azure_moderation.subprocess.run', side_effect=AssertionError('CLI invoked')):
            azure = AzureModerator()
            try:
                with self.assertRaisesRegex(RuntimeError, 'AZURE_OPENAI_KEY'):
                    await azure.analyze('test')
                with self.assertRaisesRegex(RuntimeError, 'GEMINI_API_KEY'):
                    await GeminiVideoModerator(azure.http)._generate([{'text': 'test'}])
            finally:
                await azure.http.aclose()

    async def test_invalid_google_config_fails_before_request(self):
        async with httpx.AsyncClient() as http:
            for config, message in (({'GEMINI_API_PROVIDER': 'unknown'}, 'GEMINI_API_PROVIDER'),
                                    ({'GEMINI_MODEL': '../invalid?key=secret'}, 'GEMINI_MODEL')):
                with self.subTest(config=config), patch.dict(os.environ, config):
                    with self.assertRaisesRegex(RuntimeError, message):
                        await GeminiVideoModerator(http)._generate([{'text': 'test'}])

    async def test_google_http_errors_do_not_expose_api_key(self):
        for status in (400, 401, 403, 404, 429):
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, json={'error': {'message': 'Test error'}}))) as http:
                with self.assertRaises(httpx.HTTPStatusError) as raised:
                    await GeminiVideoModerator(http)._generate([{'text': 'test'}])
                self.assertNotIn('test-google-key', str(raised.exception))

    async def test_sarvam_keeps_api_key_authentication(self):
        def respond(request):
            self.assertEqual(request.url.host, 'api.sarvam.ai')
            self.assertEqual(request.headers['api-subscription-key'], 'test-sarvam-key')
            return httpx.Response(200, json={'transcript': 'test transcript'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            with patch.object(SarvamTranscriber, '_audio_chunks', return_value=[b'audio']):
                self.assertEqual(await SarvamTranscriber(http).transcribe(b'media'), 'test transcript')

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

    def test_animated_image_is_not_silently_reduced_to_first_frame(self):
        frames = [Image.new('RGB', (20, 20), color) for color in ('white', 'red')]
        data = io.BytesIO()
        frames[0].save(data, 'GIF', save_all=True, append_images=frames[1:], duration=100)
        with self.assertRaisesRegex(ValueError, 'Animated images require review'):
            AzureModerator._prepare_image(data.getvalue())

    def test_project_endpoint_overrides_an_unrelated_inherited_endpoint(self):
        with patch.object(Path, 'read_text', return_value='AZURE_OPENAI_ENDPOINT=https://project.example\n'), \
             patch.dict(os.environ, {'AZURE_OPENAI_ENDPOINT': 'https://other.example'}):
            monitor.load_dotenv()
            self.assertEqual(os.environ['AZURE_OPENAI_ENDPOINT'], 'https://project.example')


if __name__ == '__main__':
    unittest.main()
