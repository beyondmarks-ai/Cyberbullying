import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from azure_moderation import AzureModerator, GeminiVideoModerator, SarvamTranscriber
from instagram_graph import InstagramGraph
from instagram_store import account_path, write_json, ROOT

SESSION_FILE = SEEN_FILE = EVENT_FILE = None
STATUS_FILE = None
SCAN_ERRORS = []


def load_dotenv():
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def unseen_ids(comment_ids, seen):
    return [comment_id for comment_id in comment_ids if comment_id not in seen]


def account_files(username):
    account = hashlib.sha256(username.lower().encode()).hexdigest()[:12]
    folder = Path("tools/accounts")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{account}-session.json", folder / f"{account}-seen.json", folder / f"{account}-events.jsonl"


def record_event(kind, source, text, analysis, event_id=None):
    event = {"kind": kind, "source": source, "text": text, "analysis": analysis,
             "time": datetime.now(timezone.utc).isoformat(), "id": event_id}
    with EVENT_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")


async def moderate(moderator, transcriber, video_moderator, kind, source, text, media=None, event_id=None):
    try:
        images, videos, transcripts, audio_failed = [], [], [], False
        for url, media_kind in (media or []):
            data = await moderator.download(url)
            if media_kind == "image":
                images.append(data)
            elif media_kind == "video":
                videos.append(data)
            if media_kind in {"audio", "video"}:
                try:
                    transcript = await transcriber.transcribe(data)
                    if transcript:
                        transcripts.append(transcript)
                except Exception as error:
                    audio_failed = True
                    print(f"Sarvam transcription failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        transcript = " ".join(transcripts)
        if kind == "audio" and audio_failed and not transcript:
            raise RuntimeError("Audio could not be transcribed")
        analyzed_text = f"{text}\nSpoken transcript: {transcript}" if transcript else text
        if videos:
            try:
                results = [await video_moderator.analyze(video, transcript, text) for video in videos]
                analysis = max(results, key=lambda result: (result["bullying"], result["confidence"]))
            except Exception as error:
                print(f"Gemini video analysis failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
                for video in videos:
                    images.extend(await asyncio.to_thread(moderator._video_frames, video))
                analysis = await moderator.analyze(analyzed_text, images)
                analysis["reason"] = f"Gemini unavailable; Azure frame fallback used. {analysis['reason']}"
        else:
            analysis = await moderator.analyze(analyzed_text, images)
        if audio_failed:
            analysis["reason"] = f"Audio transcription incomplete; visual/text result only. {analysis['reason']}"
        text = f"{text}\nTranscript: {transcript}" if transcript else text
    except Exception as error:
        analysis = {"bullying": False, "severity": "unknown", "confidence": 0,
                    "reason": f"Analysis failed: {type(error).__name__}", "categories": []}
        print(f"AI analysis failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        SCAN_ERRORS.append('AI analysis failed. Check Azure/Sarvam/Gemini configuration.')
    record_event(kind, source, text, analysis, event_id)
    label = 'unavailable' if analysis['severity'] == 'unknown' else ('BULLYING' if analysis['bullying'] else 'safe')
    print(f"AI {label}: {source} ({analysis['severity']})", flush=True)
    return analysis["severity"] != "unknown"


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
                                      item['text'], item['media'], item['id']):
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
                    analyzed = await moderate(moderator, transcriber, video_moderator, kind, f"post {media['id']}", media.get("caption", "Post media"), assets, media_id)
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
                        analyzed = await moderate(moderator, transcriber, video_moderator, "comment", f"@{author} · post {media['id']}", comment.get("text", ""), event_id=comment_id)
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
