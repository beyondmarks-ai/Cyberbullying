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
from comment_monitor import ChallengeRequired, SenderProfiles, sender_label


class SenderProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "test-profiles.json"
        self.profiles = SenderProfiles(self.path)
        self.user = SimpleNamespace(pk=123, username="example.user", full_name="Example User")

    async def test_participant_profile_avoids_lookup(self):
        self.profiles.remember(self.user)
        client = SimpleNamespace(user_info_v1=AsyncMock())
        sender = await self.profiles.resolve(client, "123")
        self.assertEqual(sender_label(sender), "@example.user")
        client.user_info_v1.assert_not_awaited()

    async def test_missing_participant_is_resolved_and_persisted(self):
        client = SimpleNamespace(user_info_v1=AsyncMock(return_value=self.user))
        await self.profiles.resolve(client, 123)
        cached = SenderProfiles(self.path)
        sender = await cached.resolve(client, "123")
        self.assertEqual(sender["full_name"], "Example User")
        client.user_info_v1.assert_awaited_once_with("123")

    async def test_failed_lookup_keeps_id_and_does_not_repeat_immediately(self):
        client = SimpleNamespace(user_info_v1=AsyncMock(side_effect=RuntimeError("unavailable")))
        sender = await self.profiles.resolve(client, "123")
        await self.profiles.resolve(client, "123")
        self.assertEqual(sender_label(sender), "Username unavailable (ID: 123)")
        client.user_info_v1.assert_awaited_once()

    async def test_missing_id_does_not_trigger_lookup(self):
        client = SimpleNamespace(user_info_v1=AsyncMock())
        self.assertEqual(sender_label(await self.profiles.resolve(client, None)), "Unknown sender")
        client.user_info_v1.assert_not_awaited()

    async def test_security_checkpoint_propagates(self):
        client = SimpleNamespace(user_info_v1=AsyncMock(side_effect=ChallengeRequired()))
        with self.assertRaises(ChallengeRequired):
            await self.profiles.resolve(client, "123")

    async def test_latest_participant_name_replaces_cached_name(self):
        self.profiles.remember(self.user)
        self.user.username = "renamed.user"
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
        with patch.object(dashboard, "event_file", return_value=events_path):
            result = list(reversed(dashboard.read_events("test")))
        self.assertEqual(result[0]["source"], "@example.user")
        self.assertEqual(result[1]["source"], "@example.user · direct message")
        self.assertEqual(result[2]["source"], "Username unavailable (ID: 999)")
        self.assertEqual(result[3]["source"], "@123 · post ABC")
        self.assertEqual(events_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
