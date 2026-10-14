"""Offline regression tests for sender lookup and historical event display."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard
from sender_profiles import SenderProfiles, sender_label
from instagram_graph import InstagramGraph


class SenderProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "test-profiles.json"
        self.profiles = SenderProfiles(self.path)
        self.user = {'id': '123', 'username': 'example.user', 'name': 'Example User'}

    async def test_participant_profile_avoids_lookup(self):
        self.profiles.remember(self.user)
        client = SimpleNamespace(sender_profile=AsyncMock())
        sender = await self.profiles.resolve(client, "123")
        self.assertEqual(sender_label(sender), "@example.user")
        client.sender_profile.assert_not_awaited()

    async def test_lookup_uses_official_instagram_profile_endpoint(self):
        client = InstagramGraph('offline-test-token', 'owner')
        self.addAsyncCleanup(client.close)
        client._get = AsyncMock(return_value=self.user)
        sender = await self.profiles.resolve(client, '123')
        self.assertEqual(sender['username'], 'example.user')
        client._get.assert_awaited_once_with('/123', fields='username,name')

    async def test_missing_participant_is_resolved_and_persisted(self):
        client = SimpleNamespace(sender_profile=AsyncMock(return_value=self.user))
        await self.profiles.resolve(client, 123)
        cached = SenderProfiles(self.path)
        sender = await cached.resolve(client, "123")
        self.assertEqual(sender["full_name"], "Example User")
        client.sender_profile.assert_awaited_once_with("123")

    async def test_failed_lookup_keeps_id_and_does_not_repeat_immediately(self):
        client = SimpleNamespace(sender_profile=AsyncMock(side_effect=RuntimeError("unavailable")))
        sender = await self.profiles.resolve(client, "123")
        await self.profiles.resolve(client, "123")
        self.assertEqual(sender_label(sender), "Username unavailable (ID: 123)")
        client.sender_profile.assert_awaited_once()

    async def test_missing_id_does_not_trigger_lookup(self):
        client = SimpleNamespace(sender_profile=AsyncMock())
        self.assertEqual(sender_label(await self.profiles.resolve(client, None)), "Unknown sender")
        client.sender_profile.assert_not_awaited()

    async def test_mismatched_profile_is_not_attached_to_sender(self):
        client = SimpleNamespace(sender_profile=AsyncMock(return_value={**self.user, 'id': '456'}))
        sender = await self.profiles.resolve(client, '123')
        self.assertIsNone(sender['username'])

    async def test_latest_participant_name_replaces_cached_name(self):
        self.profiles.remember(self.user)
        self.user['username'] = "renamed.user"
        self.profiles.remember(self.user)
        self.assertEqual(SenderProfiles(self.path).profiles["123"]["username"], "renamed.user")

    async def test_old_events_are_enriched_without_rewriting_history(self):
        events_path = self.path.with_name("test-events.jsonl")
        events = [
            {"kind": "dm", "source": "@123"},
            {"kind": "audio", "source": "@123 · direct message"},
            {"kind": "dm", "source": "@999"},
            {"kind": "comment", "source": "@123 · post ABC"},
        ]
        original = "\n".join(json.dumps(event) for event in events)
        events_path.write_text(original, encoding="utf-8")
        self.profiles.remember(self.user)
        with patch.object(dashboard, 'account_path', return_value=self.path), \
             patch.object(dashboard, 'read_history', return_value={'events': {}}), \
             patch.object(dashboard, 'read_live_events', return_value={str(i): e for i, e in enumerate(events)}):
            result = dashboard.read_events('test')
        self.assertEqual(result[0]["source"], "@example.user")
        self.assertEqual(result[1]["source"], "@example.user · direct message")
        self.assertEqual(result[2]["source"], "Username unavailable (ID: 999)")
        self.assertEqual(result[3]["source"], "@123 · post ABC")
        self.assertEqual(events_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
