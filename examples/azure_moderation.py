import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
from PIL import Image


SYSTEM_PROMPT = """You are a safety classifier. Detect targeted bullying, harassment, humiliation,
threats, hate, or sexual harassment in the supplied Instagram text and visual content. Do not flag
neutral discussion, friendly teasing, criticism without abuse, or content merely documenting bullying.
Return JSON only with: bullying (boolean), confidence (0 to 1), severity (none, low, medium, high),
reason (one brief plain-language sentence), and categories (array of short labels)."""

VIDEO_PROMPT = """Analyze this complete Instagram video for cyberbullying. Consider spoken words,
on-screen text, gestures, threats, targeted humiliation, hate, sexual harassment, and the interaction
between audio and visuals. Distinguish bullying from friendly teasing, neutral criticism, reporting,
education, or counterspeech. Return the same JSON fields defined by the safety classifier."""


def normalize(result):
    severity = str(result.get("severity", "none")).lower()
    if severity not in {"none", "low", "medium", "high"}:
        severity = "none"
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    categories = result.get("categories", [])
    return {
        "bullying": result.get("bullying") is True,
        "confidence": confidence,
        "severity": severity,
        "reason": str(result.get("reason", "No explanation returned."))[:300],
        "categories": [str(item)[:50] for item in categories[:5]] if isinstance(categories, list) else [],
    }


class AzureModerator:
    def __init__(self):
        self.group = os.environ.get("AZURE_OPENAI_GROUP")
        self.resource = os.environ.get("AZURE_OPENAI_RESOURCE")
        self.endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
        self.deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        self.key = os.environ.get("AZURE_OPENAI_KEY")
        self.http = httpx.AsyncClient(timeout=180, follow_redirects=True)

    def _get_key(self):
        if self.key:
            return self.key
        if not self.group or not self.resource:
            raise RuntimeError("Set AZURE_OPENAI_GROUP and AZURE_OPENAI_RESOURCE")
        az = shutil.which("az")
        if not az:
            raise RuntimeError("Azure CLI is not installed or is not on PATH")
        try:
            result = subprocess.run(
                [az, "cognitiveservices", "account", "keys", "list", "-g", self.group, "-n", self.resource,
                 "--query", "key1", "-o", "tsv"], capture_output=True, text=True, check=True,
            )
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "Azure CLI returned an error.").strip()
            raise RuntimeError(f"Azure CLI key lookup failed: {detail}") from error
        self.key = result.stdout.strip()
        if not self.key:
            raise RuntimeError("Azure CLI returned no Azure OpenAI key")
        return self.key

    async def analyze(self, text="", images=None):
        if not self.endpoint:
            raise RuntimeError("Set AZURE_OPENAI_ENDPOINT")
        content = [{"type": "text", "text": text or "Analyze the supplied visual content."}]
        for image in (images or [])[:6]:
            encoded = base64.b64encode(self._prepare_image(image)).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "low"}})
        response = await self.http.post(
            f"{self.endpoint}/openai/deployments/{self.deployment}/chat/completions",
            params={"api-version": "2024-10-21"}, headers={"api-key": await asyncio.to_thread(self._get_key)},
            json={"messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
                  "response_format": {"type": "json_object"}, "temperature": 0, "max_tokens": 250},
        )
        response.raise_for_status()
        return normalize(json.loads(response.json()["choices"][0]["message"]["content"]))

    @staticmethod
    def _prepare_image(data):
        with Image.open(io.BytesIO(data)) as image:
            image.thumbnail((1024, 1024))
            output = io.BytesIO()
            image.convert("RGB").save(output, "JPEG", quality=80)
            return output.getvalue()

    async def download(self, url):
        data = bytearray()
        async with self.http.stream("GET", str(url)) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 50 * 1024 * 1024:
                    raise ValueError("Instagram media exceeds the 50 MB analysis limit")
        return bytes(data)

    @staticmethod
    def _video_frames(data):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required to analyze video frames")
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "video.mp4"
            source.write_bytes(data)
            subprocess.run([ffmpeg, "-loglevel", "error", "-i", str(source), "-vf",
                            "fps=1/6:round=up,scale=min(1024\\,iw):-2", "-frames:v", "10",
                            str(Path(folder) / "frame-%02d.png")], capture_output=True, check=True)
            return [path.read_bytes() for path in sorted(Path(folder).glob("frame-*.png"))]


class SarvamTranscriber:
    def __init__(self, http):
        self.http = http
        self.key = os.environ.get("SARVAM_API_KEY")

    async def transcribe(self, media):
        if not self.key:
            raise RuntimeError("Set SARVAM_API_KEY in the environment")
        transcripts = []
        for chunk in await asyncio.to_thread(self._audio_chunks, media):
            response = await self.http.post(
                "https://api.sarvam.ai/speech-to-text",
                headers={"api-subscription-key": self.key},
                data={"model": "saaras:v4", "language_code": "unknown"},
                files={"file": ("audio.wav", chunk, "audio/wav")},
            )
            response.raise_for_status()
            transcript = response.json().get("transcript", "").strip()
            if transcript:
                transcripts.append(transcript)
        return " ".join(transcripts)

    @staticmethod
    def _audio_chunks(media):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required to analyze audio")
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "media"
            source.write_bytes(media)
            result = subprocess.run(
                [ffmpeg, "-loglevel", "error", "-i", str(source), "-t", "60", "-vn", "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", "-f", "segment", "-segment_time", "25", "-reset_timestamps", "1",
                 str(Path(folder) / "audio-%02d.wav")], capture_output=True,
            )
            if result.returncode and not list(Path(folder).glob("audio-*.wav")):
                raise RuntimeError("No readable audio track found")
            return [path.read_bytes() for path in sorted(Path(folder).glob("audio-*.wav"))]


class GeminiVideoModerator:
    def __init__(self, http):
        self.http = http
        self.project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        self.location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
        self.model = os.environ.get("GOOGLE_VIDEO_MODEL", "gemini-3.8-flash")

    def _credentials(self):
        gcloud = shutil.which("gcloud")
        if not gcloud:
            raise RuntimeError("Google Cloud CLI is not installed or is not on PATH")
        if not self.project:
            self.project = subprocess.run([gcloud, "config", "get-value", "project"], capture_output=True,
                                          text=True, check=True).stdout.strip()
        token = subprocess.run([gcloud, "auth", "print-access-token", "--quiet"], capture_output=True,
                               text=True, check=True).stdout.strip()
        if not self.project or not token:
            raise RuntimeError("Google Cloud CLI has no active project or login")
        return token

    async def analyze(self, video, transcript="", context=""):
        video = await asyncio.to_thread(self._prepare_video, video)
        token = await asyncio.to_thread(self._credentials)
        prompt = VIDEO_PROMPT
        if context:
            prompt += f"\nPost/message context: {context}"
        if transcript:
            prompt += f"\nSarvam speech transcript: {transcript}"
        host = "aiplatform.googleapis.com" if self.location == "global" else f"{self.location}-aiplatform.googleapis.com"
        response = await self.http.post(
            f"https://{host}/v1/projects/{self.project}/locations/{self.location}/publishers/google/models/{self.model}:generateContent",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [
                    {"inlineData": {"mimeType": "video/mp4", "data": base64.b64encode(video).decode()}},
                    {"text": prompt},
                ]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 300, "responseMimeType": "application/json"},
            },
        )
        response.raise_for_status()
        parts = response.json()["candidates"][0]["content"]["parts"]
        return normalize(json.loads("".join(part.get("text", "") for part in parts if not part.get("thought"))))

    @staticmethod
    def _prepare_video(video):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required to prepare videos")
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "source", Path(folder) / "video.mp4"
            source.write_bytes(video)
            subprocess.run(
                [ffmpeg, "-loglevel", "error", "-i", str(source), "-t", "60", "-map", "0:v:0", "-map", "0:a:0?",
                 "-vf", "scale=w=min(1280\\,iw):h=min(720\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2",
                 "-c:v", "libx264", "-preset", "veryfast", "-b:v", "1400k", "-maxrate", "1600k", "-bufsize", "3200k",
                 "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-y", str(output)],
                capture_output=True, check=True,
            )
            if output.stat().st_size > 18 * 1024 * 1024:
                raise ValueError("Prepared video exceeds the inline Vertex AI limit")
            return output.read_bytes()


if __name__ == "__main__":
    assert normalize({"bullying": True, "confidence": "1.2", "severity": "HIGH", "categories": ["threat"]}) == {
        "bullying": True, "confidence": 1.0, "severity": "high", "reason": "No explanation returned.",
        "categories": ["threat"],
    }
    with tempfile.TemporaryDirectory() as folder:
        audio = Path(folder) / "test.wav"
        subprocess.run([shutil.which("ffmpeg"), "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", "-y", str(audio)], check=True)
        assert len(SarvamTranscriber._audio_chunks(audio.read_bytes())) == 1
        video = Path(folder) / "test.mp4"
        subprocess.run([shutil.which("ffmpeg"), "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=c=blue:s=320x240:d=1", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)
        assert GeminiVideoModerator._prepare_video(video.read_bytes())
    print("Self-test passed")
