import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from aiograpi import Client
from azure_moderation import AzureModerator, GeminiVideoModerator, SarvamTranscriber

SESSION_FILE = SEEN_FILE = EVENT_FILE = None


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


def record_event(kind, source, text, analysis):
    event = {"kind": kind, "source": source, "text": text, "analysis": analysis,
             "time": datetime.now(timezone.utc).isoformat()}
    with EVENT_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")


def media_urls(media):
    items = media.resources or [media]
    return [(str(item.video_url), "video") if item.video_url else (str(item.thumbnail_url), "image")
            for item in items if item.video_url or item.thumbnail_url]


def message_media(message):
    for item in (message.media, message.media_share, message.clip):
        if item:
            if getattr(item, "audio_url", None):
                return str(item.audio_url), "audio"
            if getattr(item, "video_url", None):
                return str(item.video_url), "video"
            if getattr(item, "thumbnail_url", None):
                return str(item.thumbnail_url), "image"
    visual = getattr(message.visual_media, "media", None)
    if visual:
        if visual.video_versions:
            return str(visual.video_versions[0].url), "video"
        if visual.image_versions2 and visual.image_versions2.candidates:
            return str(visual.image_versions2.candidates[0].url), "image"
    return None, None


async def moderate(moderator, transcriber, video_moderator, kind, source, text, media=None):
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
    record_event(kind, source, text, analysis)
    print(f"AI {'BULLYING' if analysis['bullying'] else 'safe'}: {source} ({analysis['severity']})", flush=True)


async def main():
    global SESSION_FILE, SEEN_FILE, EVENT_FILE
    load_dotenv()
    username = os.environ.get("IG_USERNAME")
    password = os.environ.get("IG_PASSWORD")
    if not username or not password:
        raise SystemExit("Set IG_USERNAME and IG_PASSWORD in .env")

    SESSION_FILE, SEEN_FILE, EVENT_FILE = account_files(username)
    interval = int(os.environ.get("IG_POLL_SECONDS", "60"))
    client = Client()
    client.delay_range = [1, 3]
    if SESSION_FILE.exists():
        client.load_settings(SESSION_FILE)
    await client.login(username, password)
    client.dump_settings(SESSION_FILE)

    moderator = AzureModerator()
    transcriber = SarvamTranscriber(moderator.http)
    video_moderator = GeminiVideoModerator(moderator.http)
    user_id = await client.user_id_from_username(username)
    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    dm_baselined = any(item.startswith("dm:") for item in seen)
    print(f"Monitoring @{username}'s comments, DMs, and media every {interval} seconds. Press Ctrl+C to stop.")

    while True:
        try:
            for media in await client.user_medias(user_id, amount=3):
                media_id = f"media:{media.id}"
                if media_id not in seen:
                    assets = media_urls(media)
                    kind = "video" if any(media_kind == "video" for _, media_kind in assets) else "image"
                    await moderate(moderator, transcriber, video_moderator, kind, f"post {media.code}", media.caption_text or "Post media", assets)
                    seen.add(media_id)
                comments = await client.media_comments(media.id, amount=100)
                new_ids = unseen_ids([str(comment.pk) for comment in comments], seen)
                for comment in comments:
                    if str(comment.pk) in new_ids:
                        print(f"NEW [{media.code}] @{comment.user.username}: {comment.text}", flush=True)
                        await moderate(moderator, transcriber, video_moderator, "comment", f"@{comment.user.username} · post {media.code}", comment.text)
                seen.update(new_ids)
            threads = await client.direct_threads(amount=20, thread_message_limit=20)
            threads += await client.direct_pending_inbox(amount=20)
            for thread in threads:
                for message in thread.messages:
                    message_id = f"dm:{message.id}"
                    if dm_baselined and message_id not in seen and not message.is_sent_by_viewer:
                        sender = next((user.username for user in thread.users if str(user.pk) == str(message.user_id)),
                                      str(message.user_id))
                        if message.text:
                            print(f"NEW DM @{sender}: {message.text}", flush=True)
                            await moderate(moderator, transcriber, video_moderator, "dm", f"@{sender}", message.text)
                        url, kind = message_media(message)
                        if url:
                            print(f"NEW DM {kind} @{sender}", flush=True)
                            await moderate(moderator, transcriber, video_moderator, kind, f"@{sender} · direct message", f"Incoming {kind}", [(url, kind)])
                    seen.add(message_id)
            if not dm_baselined:
                dm_baselined = True
                print("Existing DMs baselined; new incoming messages will now be analyzed.", flush=True)
            # ponytail: unbounded test-account history; prune when this file becomes materially large.
            SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")
        except Exception as error:
            print(f"Scan failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
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
            username, password = os.environ.get("IG_USERNAME"), os.environ.get("IG_PASSWORD")
            if not username or not password:
                raise SystemExit("Missing Instagram credentials")
            SESSION_FILE, SEEN_FILE, EVENT_FILE = account_files(username)
            client = Client()
            if SESSION_FILE.exists():
                client.load_settings(SESSION_FILE)
            await client.login(username, password)
            client.dump_settings(SESSION_FILE)
            print("Login verified")
        asyncio.run(login_test())
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("Stopped")
