import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from azure_moderation import (AzureModerator, GeminiVideoModerator, SarvamTranscriber,
                              combine_results, require_review)
from instagram_graph import InstagramGraph
from instagram_store import account_path, write_json, ROOT
from preview_storage import PreviewStore

SESSION_FILE = SEEN_FILE = EVENT_FILE = None
STATUS_FILE = None
SCAN_ERRORS = []


def load_dotenv():
    for line in (ROOT / '.env').read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.lstrip().startswith("#"):
            # This local monitor uses the project's explicit provider configuration;
            # inherited credentials/endpoints from other projects must not replace it.
            os.environ[key.strip()] = value.strip().strip("'\"")


def unseen_ids(comment_ids, seen):
    return [comment_id for comment_id in comment_ids if comment_id not in seen]


def account_files(username):
    account = hashlib.sha256(username.lower().encode()).hexdigest()[:12]
    folder = Path("tools/accounts")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{account}-session.json", folder / f"{account}-seen.json", folder / f"{account}-events.jsonl"


def record_event(kind, source, text, analysis, event_id=None, previews=None, original_text=None):
    event = {"kind": kind, "source": source, "text": text, "analysis": analysis,
             "time": datetime.now(timezone.utc).isoformat(), "id": event_id,
             "previews": previews or [], "original_text": original_text if original_text is not None else text}
    with EVENT_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")


async def moderate(moderator, transcriber, video_moderator, kind, source, text, media=None, event_id=None,
                   preview_store=None, account_id=None):
    previews, preview_failed, original_text = [], False, text
    try:
        if not media and not text.strip():
            raise ValueError('Empty event has no assessable content')
        images, timed, transcripts, problems = [], [], [], []
        for index, (url, media_kind) in enumerate(media or []):
            try:
                data = await moderator.download(url)
            except Exception:
                problems.append('An attachment could not be downloaded.')
                previews.append({'kind': media_kind, 'state': 'unavailable'})
                continue
            if preview_store is not None:
                try:
                    previews.append(await asyncio.to_thread(preview_store.capture, account_id, event_id, index, media_kind, data))
                except (ValueError, OSError):
                    previews.append({'kind': media_kind, 'state': 'unsupported'})
                except Exception as error:
                    previews.append({'kind': media_kind, 'state': 'unavailable'})
                    preview_failed = True
                    print(f'Preview storage failed: {type(error).__name__}', file=sys.stderr, flush=True)
                    SCAN_ERRORS.append('A media preview could not be saved to Azure; it will be retried.')
            if media_kind == "image":
                images.append(data)
            elif media_kind in {"audio", "video"}:
                transcript, speech_failed = '', False
                try:
                    transcript = await transcriber.transcribe(data)
                    if transcript:
                        transcripts.append(transcript)
                except Exception as error:
                    speech_failed = True
                    print(f"Speech transcription failed: {type(error).__name__}", file=sys.stderr, flush=True)
                timed.append((media_kind, data, transcript, speech_failed))
            else:
                problems.append('An attachment type is not supported.')
        transcript = " ".join(transcripts)
        analyzed_text = f"{text}\nSpoken transcript: {transcript}" if transcript else text
        results = []
        for media_kind, data, speech, speech_failed in timed:
            try:
                method = video_moderator.analyze if media_kind == 'video' else video_moderator.analyze_audio
                analysis = await method(data, speech, text)
            except Exception:
                try:
                    if media_kind == 'video':
                        frames = await asyncio.to_thread(moderator._video_frames, data)
                        analysis = await analyze_image_fallback(moderator, frames, analyzed_text)
                    elif speech:
                        analysis = await moderator.analyze(f'{text}\nTranscript: {speech}')
                    else:
                        raise ValueError('No readable audio or transcript')
                    analysis = require_review(analysis, 'Native media analysis unavailable; only sampled frames or a partial transcript were assessed.', retry=True)
                except Exception:
                    problems.append(f'A {media_kind} attachment could not be assessed.')
                    continue
            if speech_failed:
                analysis['coverage'] = (analysis.get('coverage', '') +
                                        ' Separate speech transcription unavailable; native audio was used.').strip()
            results.append(analysis)
        if images:
            try:
                analysis = await video_moderator.analyze_images(images, analyzed_text)
            except Exception:
                try:
                    analysis = await analyze_image_fallback(moderator, images, analyzed_text)
                    analysis['reason'] = f"Azure image fallback used. {analysis['reason']}"
                except Exception:
                    problems.append('Images could not be assessed.')
                    analysis = None
            if analysis:
                results.append(analysis)
        if not media:
            if kind in {'image', 'video', 'audio'}:
                problems.append('The media content was not supplied by Instagram; only its caption is available.')
            calls = [moderator.analyze(text)]
            if os.environ.get('MODERATION_TEXT_SECOND_OPINION', 'true').lower() != 'false' and video_moderator is not None:
                calls.append(video_moderator.analyze_text(text))
            assessments = await asyncio.gather(*calls, return_exceptions=True)
            valid = [r for r in assessments if isinstance(r, dict)]
            analysis = combine_results(valid, second_opinion=True)
            if len(valid) != len(assessments):
                analysis = require_review(analysis, 'One text assessment failed; the independent check is incomplete.', retry=True)
            results.append(analysis)
        analysis = combine_results(results)
        for problem in problems:
            analysis = require_review(analysis, problem, retry=True)
        if analysis.get('retry_required'):
            SCAN_ERRORS.append('Some content was only partially assessed and will be retried. See Needs review.')
        text = f"{text}\nTranscript: {transcript}" if transcript else text
    except Exception as error:
        analysis = {"bullying": False, "severity": "unknown", "confidence": 0,
                    "reason": f"Analysis failed: {type(error).__name__}", "categories": [],
                    "status": "unavailable", "needs_review": True}
        print(f"AI analysis failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        SCAN_ERRORS.append('AI analysis failed. Check Azure/Sarvam/Gemini configuration.')
    record_event(kind, source, text, analysis, event_id, previews, original_text)
    label = analysis.get('status', 'unavailable')
    print(f"AI {label}: {source} ({analysis['severity']})", flush=True)
    return analysis["severity"] != "unknown" and not analysis.get('retry_required', False) and not preview_failed


async def analyze_image_fallback(moderator, images, text):
    return combine_results([await moderator.analyze(text, images[i:i + 6]) for i in range(0, len(images), 6)])


async def main():
    global SESSION_FILE, SEEN_FILE, EVENT_FILE, STATUS_FILE
    load_dotenv()
    token_file = os.environ.get("IG_TOKEN_FILE")
    if not token_file or not Path(token_file).exists():
        raise SystemExit("Connect Instagram first; IG_TOKEN_FILE is missing")
    token_data = json.loads(Path(token_file).read_text(encoding="utf-8"))
    client = InstagramGraph(token_data["access_token"], token_data["user_id"])
    await client.refresh_if_needed(token_data, Path(token_file))
    profile = await client.profile()
    username = profile["username"]
    token_data['username'] = username
    write_json(Path(token_file), token_data)
    SEEN_FILE = account_path(token_data['user_id'], 'seen.json')
    EVENT_FILE = account_path(token_data['user_id'], 'events.jsonl')
    STATUS_FILE = account_path(token_data['user_id'], 'status.json')
    inbox = account_path(token_data['user_id'], 'inbox')
    interval = max(30, int(os.environ.get("IG_POLL_SECONDS", "60")))

    moderator = AzureModerator()
    transcriber = SarvamTranscriber(moderator.http)
    video_moderator = GeminiVideoModerator(moderator.http)
    preview_store = PreviewStore()
    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    print(f"Monitoring @{username}'s comments and media every {interval} seconds. Press Ctrl+C to stop.")

    while True:
        SCAN_ERRORS.clear()
        write_json(STATUS_FILE, {'state': 'scanning', 'username': username,
                                'updated_at': datetime.now(timezone.utc).isoformat()})
        try:
            await client.refresh_if_needed(token_data, Path(token_file))
            for pending in sorted(inbox.glob('*.json')):
                item = json.loads(pending.read_text(encoding='utf-8'))
                if item['id'] not in seen:
                    if await moderate(moderator, transcriber, video_moderator, item['kind'], item['source'],
                                      item['text'], item['media'], item['id'], preview_store, token_data['user_id']):
                        seen.add(item['id'])
                if item['id'] in seen:
                    write_json(SEEN_FILE, sorted(seen))
                    pending.unlink()
            for media in await client.media(100):
                media_id = f"media:{media['id']}"
                if media_id not in seen:
                    parts = await client.children(media['id']) if media.get('media_type') == 'CAROUSEL_ALBUM' else [media]
                    assets = [(p['media_url'], 'video' if p.get('media_type') == 'VIDEO' else 'image')
                              for p in parts if p.get('media_url')]
                    kind = 'video' if any(k == 'video' for _, k in assets) else 'image'
                    analyzed = await moderate(moderator, transcriber, video_moderator, kind, f"post {media['id']}", media.get("caption", "Post media"), assets, media_id, preview_store, token_data['user_id'])
                    if analyzed:
                        seen.add(media_id)
                comments = await client.comments(media["id"], 100)
                if media.get('comments_count', 0) and not comments:
                    SCAN_ERRORS.append(f"Instagram reports {media['comments_count']} comments on post {media['id']}, but returned no comment records. Access or visibility needs checking in Meta.")
                replies = []
                for comment in comments:
                    replies.extend(await client.replies(comment['id']))
                comments.extend(replies)
                for comment in comments:
                    comment_id = 'comment:'+str(comment['id'])
                    if comment_id not in seen:
                        author = comment.get("username") or (comment.get("from") or {}).get("username") or "unknown"
                        print(f"NEW [post {media['id']}] @{author}: {comment.get('text', '')}", flush=True)
                        analyzed = await moderate(moderator, transcriber, video_moderator, "comment", f"@{author} · post {media['id']}", comment.get("text", ""), event_id=comment_id, preview_store=preview_store, account_id=token_data['user_id'])
                        if analyzed:
                            seen.add(comment_id)
            # ponytail: unbounded test-account history; prune when this file becomes materially large.
            write_json(SEEN_FILE, sorted(seen))
        except Exception as error:
            SCAN_ERRORS.append(f'Scan failed: {type(error).__name__}. Check monitor log; reconnect if the token expired.')
        write_json(STATUS_FILE, {'state': 'error' if SCAN_ERRORS else 'healthy', 'username': username,
                                'errors': list(dict.fromkeys(SCAN_ERRORS)),
                                'updated_at': datetime.now(timezone.utc).isoformat()})
        if '--once' in sys.argv:
            await client.close()
            await moderator.http.aclose()
            preview_store.close()
            return
        await asyncio.sleep(interval)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        assert unseen_ids(["1", "2"], {"1"}) == ["2"]
        assert account_files("Example.User")[0] == account_files("example.user")[0]
        print("Self-test passed")
    elif "--login-test" in sys.argv:
        async def login_test():
            global SESSION_FILE, SEEN_FILE, EVENT_FILE
            load_dotenv()
            raise SystemExit("Password login was removed; use the Instagram OAuth flow")
        asyncio.run(login_test())
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("Stopped")
