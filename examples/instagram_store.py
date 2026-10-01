import hashlib
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = ROOT / 'tools/accounts'


def account_path(account_id, suffix):
    key = hashlib.sha256(str(account_id).lower().encode()).hexdigest()[:12]
    return ACCOUNTS / f'{key}-{suffix}'


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def webhook_items(payload):
    for entry in payload.get('entry', []):
        account = str(entry.get('id', ''))
        for change in entry.get('changes', []):
            value = change.get('value', {})
            if change.get('field') == 'comments' and value.get('id'):
                yield account, {'id': 'comment:'+str(value['id']), 'kind': 'comment',
                                'source': '@'+((value.get('from') or {}).get('username') or 'unknown'),
                                'text': value.get('text', ''), 'media': []}
        for event in entry.get('messaging', []):
            message = event.get('message', {})
            if not message.get('mid') or message.get('is_echo') or str(event.get('sender', {}).get('id')) == account:
                continue
            assets = [(a.get('payload', {}).get('url'), a.get('type'))
                      for a in message.get('attachments', []) if a.get('type') in {'image', 'video', 'audio'}]
            assets = [(url, kind) for url, kind in assets if url]
            yield account, {'id': 'dm:'+message['mid'], 'kind': assets[0][1] if assets else 'dm',
                            'source': str(event.get('sender', {}).get('id', 'unknown')),
                            'text': message.get('text', 'Incoming media'), 'media': assets}
