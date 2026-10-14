"""Cache usernames returned by the official Instagram profile API."""
import json
import re
import sys
import time

from instagram_store import write_json


def load_profiles(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def sender_label(sender):
    if sender.get('username'):
        return '@' + sender['username']
    return f"Username unavailable (ID: {sender['id']})" if sender.get('id') else 'Unknown sender'


class SenderProfiles:
    def __init__(self, path):
        self.path = path
        self.profiles = load_profiles(path)
        self.retry_after = {}

    def remember(self, user):
        if not isinstance(user, dict):
            return
        user_id = str(user.get('id') or '')
        username = user.get('username')
        if not user_id or not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9._]{1,30}', username):
            return
        profile = {'id': user_id, 'username': username, 'full_name': user.get('name') or ''}
        if self.profiles.get(user_id) != profile:
            self.profiles[user_id] = profile
            write_json(self.path, self.profiles)

    async def resolve(self, client, user_id):
        user_id = str(user_id or '')
        if user_id and user_id not in self.profiles and time.monotonic() >= self.retry_after.get(user_id, 0):
            self.retry_after[user_id] = time.monotonic() + 300
            try:
                profile = await client.sender_profile(user_id)
                if isinstance(profile, dict) and str(profile.get('id') or user_id) == user_id:
                    self.remember({**profile, 'id': user_id})
            except Exception as error:
                print(f'Sender profile unavailable: {type(error).__name__}', file=sys.stderr, flush=True)
        return self.profiles.get(user_id, {'id': user_id or None, 'username': None, 'full_name': ''})


def enrich_sender(event, profiles):
    if event.get('direction') == 'outgoing':
        return event
    source = event.get('source', '')
    sender = event.get('sender') or {}
    is_dm = event.get('kind') == 'dm' or str(event.get('id', '')).startswith('dm:') or 'direct message' in source
    legacy = re.match(r'^@?(\d+)(?=\s|$)', source) if is_dm else None
    user_id = str(sender.get('id') or (legacy.group(1) if legacy else ''))
    profile = profiles.get(user_id) or (sender if sender.get('username') else None)
    if profile:
        suffix = ' · direct message' if 'direct message' in source else ''
        return {**event, 'sender': profile, 'source': sender_label(profile) + suffix}
    if legacy:
        return {**event, 'source': source.replace(legacy.group(0), sender_label({'id': user_id}), 1)}
    return event
