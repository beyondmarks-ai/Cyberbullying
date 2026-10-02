"""On-demand history import. No messages are sent and no historical AI calls are made."""
import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from instagram_graph import InstagramGraph
from instagram_store import account_path, write_json
from preview_storage import PreviewStore, MAX_BYTES

MAX_CONVERSATIONS = 20
MAX_PER_CONVERSATION = 50
MAX_MESSAGES = 200


def read_history(account_id):
    path = account_path(account_id, 'history.json')
    if not path.exists():
        return {'events': {}, 'conversations': []}
    return json.loads(path.read_text(encoding='utf-8'))


def read_live_events(account_id):
    path = account_path(account_id, 'events.jsonl')
    events = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                event = json.loads(line)
                if event.get('id'):
                    events[event['id']] = event
            except (ValueError, AttributeError):
                continue  # A concurrent writer may not have finished its final line.
    return events


def attachments(message):
    """Retain unavailable attachment slots so indexes cannot shift between retries."""
    raw = message.get('attachments', {}) or {}
    rows = raw.get('data', []) if isinstance(raw, dict) else []
    result = []
    for item in rows:
        if not isinstance(item, dict):
            result.append(('media', None))
            continue
        for field, kind in (('image_data', 'image'), ('video_data', 'video'), ('audio_data', 'audio')):
            if field in item:
                nested = item.get(field) or {}
                result.append((kind, nested.get('url') if isinstance(nested, dict) else None))
                break
        else:
            mime = str(item.get('mime_type', ''))
            kind = mime.split('/')[0] if mime.split('/')[0] in {'image', 'video', 'audio'} else 'media'
            result.append((kind, item.get('file_url') or item.get('url')))
    return result


def is_self(person, account):
    return (str(person.get('id', '')) == str(account['user_id']) or
            bool(person.get('username') and account.get('username') and
                 person['username'].casefold() == account['username'].casefold()))


def history_event(message, conversation, account):
    sender = message.get('from') or {}
    outgoing = is_self(sender, account)
    participants = (conversation.get('participants') or {}).get('data', [])
    others = [p for p in participants if not is_self(p, account)]
    label = ', '.join('@' + p['username'] for p in others if p.get('username')) or 'Instagram conversation'
    media = attachments(message)
    kind = next((kind for kind, _ in media if kind in {'image', 'video', 'audio'}), 'dm')
    created = str(message.get('created_time') or '')
    try:
        timestamp = datetime.fromisoformat(created.replace('Z', '+00:00'))
        created = timestamp.replace(tzinfo=timestamp.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
    except ValueError:
        created = ''
    return {'id': 'dm:' + str(message['id']), 'kind': kind,
            'source': 'You' if outgoing else ('@' + sender['username'] if sender.get('username') else str(sender.get('id', 'Unknown sender'))),
            'text': message.get('message') or '', 'original_text': message.get('message') or '',
            'time': created, 'direction': 'outgoing' if outgoing else 'incoming',
            'conversation_id': str(conversation['id']), 'conversation_label': label,
            'history_imported': True, 'analysis': None, 'previews': []}, media


def allowed_media_url(url):
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password and
            parsed.port in (None, 443) and any(host == suffix or host.endswith('.' + suffix)
            for suffix in ('fbcdn.net', 'cdninstagram.com', 'fbsbx.com', 'instagram.com', 'facebook.com')))


async def download_attachment(http, url):
    for _ in range(4):
        if not allowed_media_url(url):
            raise ValueError('Unsupported media source')
        async with http.stream('GET', url, follow_redirects=False) as response:
            if response.is_redirect:
                url = str(response.url.join(response.headers.get('location', '')))
                continue
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError('Attachment exceeds 50 MB')
            return bytes(data)
    raise ValueError('Too many media redirects')


async def backfill_previews(store, http, account_id, event_id, media, progress):
    previews = []
    for index, (kind, url) in enumerate(media):
        existing = await asyncio.to_thread(store.find, account_id, event_id, index)
        if existing and existing['state'] != 'pending':
            previews.append(existing)
            progress['previews_available'] += existing['state'] == 'available'
            progress['previews_unavailable'] += existing['state'] != 'available'
            continue
        if not store.enabled:
            preview = {'kind': kind, 'state': 'disabled', 'message': 'Azure preview storage is not configured.'}
        elif not url:
            preview = {'kind': kind, 'state': 'unavailable', 'message': 'Instagram did not provide a downloadable attachment.'}
        else:
            try:
                data = await download_attachment(http, url)
                preview = await asyncio.to_thread(store.capture, account_id, event_id, index, kind, data)
                progress['previews_available'] += preview['state'] == 'available'
            except httpx.HTTPStatusError as error:
                preview = {'kind': kind, 'state': 'unavailable', 'message':
                           'Instagram attachment is expired or inaccessible.' if error.response.status_code in (400, 401, 403, 404, 410)
                           else 'Attachment download failed. Try syncing again.'}
            except (ValueError, OSError):
                preview = {'kind': kind, 'state': 'unsupported', 'message': 'This attachment source, format or size is unsupported.'}
            except Exception:
                preview = {'kind': kind, 'state': 'unavailable', 'message': 'Could not save this preview. Check storage and try syncing again.'}
        if preview['state'] != 'available':
            progress['previews_unavailable'] += 1
        previews.append(preview)
    return previews


async def sync_history(account, getter, progress, save_progress):
    account_id = account['user_id']
    graph = InstagramGraph(account['access_token'], account_id)
    store = PreviewStore(getter)
    history = read_history(account_id)
    live = read_live_events(account_id)
    imported, conversations = history.get('events', {}), {c['id']: c for c in history.get('conversations', [])}

    def persist():
        write_json(account_path(account_id, 'history.json'), {'events': imported, 'conversations': list(conversations.values())})
        save_progress()

    try:
        rows, capped = await graph.history_pages(f'/{account_id}/conversations', MAX_CONVERSATIONS,
                                               platform='instagram', fields='id,updated_time,participants')
        progress['limited'] = capped
        async with httpx.AsyncClient(timeout=45) as http:
            for conversation in rows:
                remaining = MAX_MESSAGES - progress['messages']
                if remaining <= 0:
                    progress['limited'] = True
                    break
                try:
                    messages, more = await graph.history_pages(f'/{conversation["id"]}/messages', min(MAX_PER_CONVERSATION, remaining),
                                                              fields='id,created_time,from,to,message,attachments')
                    progress['limited'] = progress['limited'] or more
                    progress['conversations'] += 1
                    for message in messages:
                        event, media = history_event(message, conversation, account)
                        previous = live.get(event['id']) or imported.get(event['id']) or {}
                        event['analysis'] = previous.get('analysis')
                        event['previews'] = await backfill_previews(store, http, account_id, event['id'], media, progress)
                        if not media and previous.get('previews'):
                            event['previews'] = previous['previews']
                            event['kind'] = previous.get('kind', event['kind'])
                        imported[event['id']] = event
                        conversations[str(conversation['id'])] = {'id': str(conversation['id']), 'label': event['conversation_label']}
                        progress['messages'] += 1
                        persist()
                except Exception:
                    progress['warnings'].append('One conversation could not be fully imported. Try syncing again.')
                    save_progress()
            # Also restore previews for existing post cards, without importing unrelated posts or re-running AI.
            missing_posts = {key for key, event in live.items() if key.startswith('media:') and not event.get('previews')}
            if missing_posts:
                try:
                    for post in await graph.media(100):
                        event_id = 'media:' + str(post['id'])
                        if event_id not in missing_posts:
                            continue
                        parts = await graph.children(post['id']) if post.get('media_type') == 'CAROUSEL_ALBUM' else [post]
                        assets = [('video' if p.get('media_type') == 'VIDEO' else 'image', p.get('media_url')) for p in parts]
                        event = dict(live[event_id])
                        event['previews'] = await backfill_previews(store, http, account_id, event_id, assets, progress)
                        imported[event_id] = event
                        persist()
                except Exception:
                    progress['warnings'].append('Some existing post previews could not be restored.')
        persist()
    finally:
        await graph.close()
        store.close()


class HistorySyncManager:
    def __init__(self, getter, runner=sync_history):
        self.getter, self.runner = getter, runner
        self.lock = threading.Lock()
        self.threads = {}

    def status(self, account_id):
        path = account_path(account_id, 'history-status.json')
        status = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'state': 'idle'}
        thread = self.threads.get(str(account_id))
        if status.get('state') == 'running' and not (thread and thread.is_alive()):
            status = {**status, 'state': 'interrupted', 'message': 'The previous sync was interrupted. Start it again.'}
        status['conversations_list'] = read_history(account_id).get('conversations', [])
        return status

    def start(self, account):
        account = dict(account)  # Never follow an account switch during an in-flight import.
        account_id = str(account['user_id'])
        with self.lock:
            thread = self.threads.get(account_id)
            if thread and thread.is_alive():
                return self.status(account_id)
            progress = {'state': 'running', 'started_at': time.time(), 'messages': 0, 'conversations': 0,
                        'previews_available': 0, 'previews_unavailable': 0, 'limited': False, 'warnings': []}
            def save():
                write_json(account_path(account_id, 'history-status.json'), {**progress, 'updated_at': time.time()})
            def run():
                try:
                    asyncio.run(self.runner(account, self.getter, progress, save))
                    progress['state'] = 'partial' if progress['warnings'] or progress['previews_unavailable'] else 'complete'
                except Exception:
                    progress.update(state='error', message='History sync failed. Check Instagram access and try again.')
                finally:
                    save()
            save()
            thread = threading.Thread(target=run, daemon=True)
            self.threads[account_id] = thread
            thread.start()
            return dict(progress)
