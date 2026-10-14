import argparse
import asyncio
import json
import sys
from pathlib import Path

from azure_moderation import AzureModerator, SarvamTranscriber
from comment_monitor import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


async def main(audio_path):
    load_dotenv()
    moderator = AzureModerator()
    try:
        transcript = await SarvamTranscriber(moderator.http).transcribe(audio_path.read_bytes())
        analysis = await moderator.analyze(f"Spoken transcript: {transcript}")
        print(json.dumps({"transcript": transcript, "analysis": analysis}, ensure_ascii=False, indent=2))
    finally:
        await moderator.http.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.audio))
