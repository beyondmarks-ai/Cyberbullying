import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

from azure_moderation import GeminiVideoModerator, ffmpeg_executable
from comment_monitor import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


async def main():
    load_dotenv()
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        frame = Image.new("RGB", (960, 540), "#111827")
        draw = ImageDraw.Draw(frame)
        font_path = Path("C:/Windows/Fonts/arialbd.ttf")
        font = ImageFont.truetype(str(font_path), 48) if font_path.exists() else ImageFont.load_default()
        draw.multiline_text(
            (55, 170),
            "YOU ARE WORTHLESS\nSAB TUMSE NAFRAT KARTE HAIN",
            fill="#ffffff",
            font=font,
            spacing=22,
        )
        image_path, video_path = folder / "frame.png", folder / "test.mp4"
        frame.save(image_path)
        subprocess.run(
            [ffmpeg_executable(), "-loglevel", "error", "-loop", "1", "-i", str(image_path),
             "-t", "3", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-y", str(video_path)],
            check=True,
        )
        http = httpx.AsyncClient(timeout=180, follow_redirects=True)
        moderator = GeminiVideoModerator(http)
        try:
            result = await moderator.analyze(
                video_path.read_bytes(),
                transcript="नहीं ऐसी कोई। तेरी माँ की चूत। किस बात पे मारा है?",
                context="Synthetic safety test video",
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
