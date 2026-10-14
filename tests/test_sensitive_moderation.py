import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
import comment_monitor as monitor
from azure_moderation import combine_results, normalize, require_review, segment_ranges, GeminiVideoModerator, azure_schema, RESULT_SCHEMA


def assessment(bullying=False, **changes):
    return normalize(dict(bullying=bullying, severity='low' if bullying else 'none', confidence=.9,
                          needs_review=False, evidence=['An observed cue'], uncertainties=[],
                          languages=['English'], reason='Test decision', content_summary='Test content', **changes))


class SensitiveTests(unittest.IsolatedAsyncioTestCase):
    def test_azure_schema_is_strict_and_complete(self):
        schema = azure_schema(RESULT_SCHEMA)
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(schema['type'], 'object')
        self.assertEqual(schema['properties']['needs_review']['type'], 'boolean')
        self.assertEqual(set(schema['required']), set(schema['properties']))

    def test_positive_evidence_is_not_lost_after_many_neutral_segments(self):
        neutral = {**assessment(), 'evidence': [f'Neutral frame {i}' for i in range(12)]}
        harmful = {**assessment(True), 'evidence': ['Actual targeted threat']}
        self.assertEqual(combine_results([neutral, harmful])['evidence'][0], 'Actual targeted threat')

    def test_uncertainty_and_invalid_confidence_never_clear(self):
        for field, value in [('confidence', .3), ('confidence', float('nan')),
                             ('confidence', float('inf')), ('needs_review', True),
                             ('uncertainties', ['Meaning is unclear'])]:
            raw = dict(bullying=False, confidence=.9, severity='none', needs_review=False)
            raw[field] = value
            self.assertEqual(normalize(raw)['status'], 'review')

    def test_disagreement_requires_review_and_retains_positive_evidence(self):
        result = combine_results([assessment(), assessment(True)], second_opinion=True)
        self.assertTrue(result['bullying'])
        self.assertEqual(result['status'], 'review')

    def test_segment_aggregation_does_not_erase_abuse_or_uncertainty(self):
        result = combine_results([assessment(True), require_review(assessment(), 'Unclear gesture'), assessment()])
        self.assertTrue(result['bullying'])
        self.assertEqual(result['status'], 'review')
        self.assertIn('Unclear gesture', result['uncertainties'])

    def test_segments_overlap_and_cover_tail(self):
        self.assertEqual(list(segment_ranges(121)), [(0, 60), (58, 118), (116, 121)])
        self.assertEqual(list(segment_ranges(60)), [(0, 60)])

    async def test_native_audio_catches_abuse_past_first_minute(self):
        model = GeminiVideoModerator(None)
        model._generate = AsyncMock(side_effect=[assessment(), assessment(True)])
        with patch.object(model, '_media_segments', return_value=([(b'a', 0, 60), (b'b', 58, 90)], 90, 90)):
            result = await model.analyze_audio(b'input')
        self.assertTrue(result['bullying'])
        self.assertEqual(len(result['segments']), 2)
        self.assertIn('[58.0-90.0s]', result['evidence'][0])
        sent = model._generate.call_args_list[0].args[0][0]['inlineData']
        self.assertEqual(sent['mimeType'], 'audio/flac')

    async def test_partial_segment_failure_preserves_detection_and_retries(self):
        model = GeminiVideoModerator(None)
        model._generate = AsyncMock(side_effect=[assessment(True), RuntimeError('timeout')])
        with patch.object(model, '_media_segments', return_value=([(b'a', 0, 60), (b'b', 58, 90)], 90, 90)):
            result = await model.analyze(b'input')
        self.assertTrue(result['bullying'])
        self.assertTrue(result['retry_required'])
        self.assertEqual(result['segments'][-1]['status'], 'unavailable')

    async def test_duration_limit_requires_review(self):
        model = GeminiVideoModerator(None)
        model._generate = AsyncMock(return_value=assessment())
        with patch.object(model, '_media_segments', return_value=([(b'a', 0, 60)], 100, 60)):
            result = await model.analyze(b'input')
        self.assertEqual(result['status'], 'review')
        self.assertIn('remaining portion', result['uncertainties'][0])

    async def test_every_image_batch_is_analyzed(self):
        model = GeminiVideoModerator(None)
        model._generate = AsyncMock(side_effect=[assessment(), assessment(True)])
        with patch.object(model, '_image_parts', return_value=[]):
            result = await model.analyze_images([b'image'] * 7)
        self.assertEqual(model._generate.await_count, 2)
        self.assertTrue(result['bullying'])

    async def test_failed_image_batch_cannot_be_clear(self):
        model = GeminiVideoModerator(None)
        model._generate = AsyncMock(side_effect=[assessment(), RuntimeError('test')])
        with patch.object(model, '_image_parts', return_value=[]):
            result = await model.analyze_images([b'image'] * 7)
        self.assertEqual(result['status'], 'review')
        self.assertTrue(result['retry_required'])

    async def run_pipeline(self, azure, speech, gemini, kind, media=None):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'MODERATION_TEXT_SECOND_OPINION': 'true'}):
            with patch.object(monitor, 'EVENT_FILE', Path(folder) / 'events.jsonl'):
                complete = await monitor.moderate(azure, speech, gemini, kind, 'test', 'test', media)
                return complete, json.loads(monitor.EVENT_FILE.read_text())['analysis']

    async def test_voice_does_not_require_transcription(self):
        azure, speech, gemini = AsyncMock(), AsyncMock(), AsyncMock()
        azure.download.return_value = b'audio'
        speech.transcribe.side_effect = RuntimeError('unsupported language')
        gemini.analyze_audio.return_value = assessment(True)
        complete, result = await self.run_pipeline(azure, speech, gemini, 'audio', [('https://test/audio', 'audio')])
        self.assertTrue(complete)
        self.assertTrue(result['bullying'])
        gemini.analyze_audio.assert_awaited_once()
        azure.analyze.assert_not_awaited()

    async def test_text_disagreement_visible_as_review(self):
        azure, speech, gemini = AsyncMock(), AsyncMock(), AsyncMock()
        azure.analyze.return_value = assessment()
        gemini.analyze_text.return_value = assessment(True)
        complete, result = await self.run_pipeline(azure, speech, gemini, 'dm')
        self.assertTrue(complete)
        self.assertEqual(result['status'], 'review')

    async def test_failed_second_opinion_retries_not_clear(self):
        azure, speech, gemini = AsyncMock(), AsyncMock(), AsyncMock()
        azure.analyze.return_value = assessment()
        gemini.analyze_text.side_effect = RuntimeError('provider unavailable')
        complete, result = await self.run_pipeline(azure, speech, gemini, 'comment')
        self.assertFalse(complete)
        self.assertEqual(result['status'], 'review')

    async def test_download_failure_does_not_hide_other_attachment_abuse(self):
        azure, speech, gemini = AsyncMock(), AsyncMock(), AsyncMock()
        azure.download.side_effect = [b'image', RuntimeError('expired URL')]
        gemini.analyze_images.return_value = assessment(True)
        complete, result = await self.run_pipeline(azure, speech, gemini, 'image',
                                                   [('https://test/one', 'image'), ('https://test/two', 'image')])
        self.assertFalse(complete)
        self.assertTrue(result['bullying'])
        self.assertEqual(result['status'], 'review')

    async def test_missing_media_not_treated_as_clear_caption(self):
        azure, speech, gemini = AsyncMock(), AsyncMock(), AsyncMock()
        azure.analyze.return_value = gemini.analyze_text.return_value = assessment()
        complete, result = await self.run_pipeline(azure, speech, gemini, 'video')
        self.assertFalse(complete)
        self.assertEqual(result['status'], 'review')


if __name__ == '__main__':
    unittest.main()
