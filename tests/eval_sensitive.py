"""Opt-in, billable synthetic smoke evaluation; never reads Instagram events or alters the feed."""
import argparse
import asyncio
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
from comment_monitor import load_dotenv
from azure_moderation import AzureModerator, GeminiVideoModerator, combine_results


CASES = [
    ('english-neutral', 'You did great today. Thanks for helping!', 'clear'),
    ('mild-exclusion', 'Nobody wants you here. Leave our group, loser.', 'attention'),
    ('hindi', 'तू बेकार है। कोई तुझे अपने साथ नहीं चाहता।', 'attention'),
    ('hinglish', 'tu bilkul bekaar hai, group se nikal ja', 'attention'),
    ('tamil', 'நீ ஒரு முட்டாள். எங்கள் குழுவை விட்டு வெளியே போ.', 'attention'),
    ('spanish', 'Eres inútil. Nadie te quiere en este grupo.', 'attention'),
    ('arabic', 'أنت عديم الفائدة، لا أحد يريدك في مجموعتنا.', 'attention'),
    ('emoji-targeted', '@sam 🐷🤮 nobody wants your ugly face here.', 'attention'),
    ('emoji-neutral', 'That joke was hilarious 😂💀', 'clear'),
    ('reporting-abuse', 'Someone called me "worthless". Please help me report the bullying.', 'clear'),
    ('obfuscated', 'y0u are w0rthless, n0body wants y0u here.', 'attention'),
    ('ambiguous-sarcasm', '@sam nice job 🙄', 'review'),
]


def picture(text):
    image = Image.new('RGB', (800, 260), 'white')
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 28) if sys.platform == 'win32' else ImageFont.load_default()
    ImageDraw.Draw(image).multiline_text((25, 60), text, font=font, fill='black', spacing=12)
    data = io.BytesIO()
    image.save(data, 'PNG')
    return data.getvalue()


async def main(media=False):
    load_dotenv()
    azure = AzureModerator()
    gemini = GeminiVideoModerator(azure.http)
    scores = []

    def report(name, expected, result):
        status = result['status']
        passed = status in ('bullying', 'review') if expected == 'attention' else status == expected
        scores.append(passed)
        print(json.dumps({'case': name, 'expected': expected, 'status': status, 'bullying': result['bullying'],
                          'pass': passed, 'languages': result.get('languages', [])}), flush=True)

    async def evaluate(name, expected, action):
        try:
            report(name, expected, await action)
        except Exception as error:
            scores.append(False)
            # Do not print provider bodies, URLs or credentials.
            print(json.dumps({'case': name, 'pass': False, 'error': type(error).__name__}), flush=True)

    async def text_check(text):
        return combine_results(await asyncio.gather(azure.analyze(text), gemini.analyze_text(text)), second_opinion=True)

    try:
        for name, text, expected in CASES:
            await evaluate(name, expected, text_check(text))
        for name, text, expected in [
            ('image-neutral', 'Have a good day, everyone!', 'clear'),
            ('image-targeted', 'SAM IS A LOSER.\nNobody wants you in our group.', 'attention'),
        ]:
            await evaluate(name, expected, gemini.analyze_images([picture(text)]))
        if media:
            # Synthesize English speech locally; no user recordings or paid TTS.
            if sys.platform != 'win32':
                raise RuntimeError('--media currently requires Windows speech synthesis for its test fixtures')
            with tempfile.TemporaryDirectory() as folder:
                audio = Path(folder) / 'speech.wav'
                command = ("Add-Type -AssemblyName System.Speech; "
                           "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                           f"$synth.SetOutputToWaveFile('{audio}'); "
                           "$synth.Speak('Sam, you are worthless. Nobody wants you in our group.'); $synth.Dispose()")
                subprocess.run(['powershell', '-NoProfile', '-Command', command], capture_output=True, check=True, timeout=60)
                await evaluate('native-voice-targeted', 'attention', gemini.analyze_audio(audio.read_bytes()))
                # The abusive speech appears AFTER the old 60-second cutoff.
                video = Path(folder) / 'late-abuse.mp4'
                subprocess.run([shutil.which('ffmpeg'), '-loglevel', 'error', '-f', 'lavfi', '-i',
                                'color=c=blue:s=320x240:d=72', '-i', str(audio), '-filter_complex',
                                '[1:a]adelay=62000|62000,apad[a]', '-map', '0:v', '-map', '[a]',
                                '-t', '72', '-c:v', 'libx264', '-c:a', 'aac', '-pix_fmt', 'yuv420p',
                                '-y', str(video)], capture_output=True, check=True, timeout=90)
                await evaluate('video-abuse-after-60s', 'attention', gemini.analyze(video.read_bytes()))
    finally:
        await azure.http.aclose()
    print(json.dumps({'passed': sum(scores), 'total': len(scores),
                      'note': 'Synthetic smoke cases only; not a measured real-world or all-language accuracy score.'}))
    return all(scores)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Allow billable API calls using .env')
    parser.add_argument('--media', action='store_true', help='Also generate and test native voice and a 72-second video')
    options = parser.parse_args()
    if not options.live:
        parser.error('Explicit --live is required; this evaluation makes billable API calls')
    sys.exit(0 if asyncio.run(main(options.media)) else 1)
