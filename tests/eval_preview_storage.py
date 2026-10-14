"""Explicit live Azure storage check; synthetic media only, cleaned up afterwards."""
import argparse
import io
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
from comment_monitor import load_dotenv
from preview_storage import PreviewStore


def main():
    load_dotenv()
    owner = 'synthetic-storage-test-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory() as folder:
        store = PreviewStore(db_path=Path(folder) / 'index.sqlite3')
        if not store.enabled:
            raise SystemExit('Configure Azure storage first')
        data = io.BytesIO()
        Image.new('RGB', (64, 64), 'blue').save(data, 'PNG')
        video, audio = Path(folder) / 'video.mp4', Path(folder) / 'voice.wav'
        subprocess.run([shutil.which('ffmpeg'), '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'color=c=blue:s=320x240:d=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                        '-movflags', '+faststart', '-y', str(video)], capture_output=True, check=True)
        subprocess.run([shutil.which('ffmpeg'), '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=1', '-y', str(audio)], capture_output=True, check=True)
        for index, (kind, payload) in enumerate([('image', data.getvalue()), ('video', video.read_bytes()), ('audio', audio.read_bytes())]):
            descriptor = None
            try:
                descriptor = store.capture(owner, 'synthetic-event', index, kind, payload)
                signed = store.link(owner, descriptor['id'])
                query = parse_qs(urlsplit(signed['url']).query)
                assert query['sp'] == ['r'] and query['spr'] == ['https']
                with httpx.Client(timeout=30) as http:
                    anonymous = http.get(signed['url'].split('?')[0])
                    code = anonymous.headers.get('x-ms-error-code')
                    if not code:
                        try:
                            code = ET.fromstring(anonymous.content).findtext('Code')
                        except ET.ParseError:
                            pass
                    print(json.dumps({'anonymous_status': anonymous.status_code, 'azure_error_code': code}), flush=True)
                    assert anonymous.status_code in (401, 403, 404) or (
                        anonymous.status_code == 409 and code == 'PublicAccessNotPermitted')
                    response = http.get(signed['url'], headers={'Range': 'bytes=0-15'})
                    assert response.status_code == 206 and response.content == payload[:16]
                try:
                    store.link('different-account', descriptor['id'])
                except KeyError:
                    pass
                else:
                    raise AssertionError('Cross-account access must fail')
                print(json.dumps({'kind': kind, 'upload': 'passed', 'anonymous_access': 'blocked',
                                  'range_playback': 'passed', 'account_isolation': 'passed'}), flush=True)
            finally:
                if descriptor:
                    store.delete(owner, descriptor['id'])
                    assert store.capture(owner, 'synthetic-event', index, kind, payload)['state'] == 'deleted'
                    with httpx.Client(timeout=30) as http:
                        assert http.get(signed['url']).status_code == 404
                    print(json.dumps({'kind': kind, 'synthetic_blob_deleted': True, 'retry_resurrection': 'blocked'}), flush=True)
    print('PASS: live private storage, signed range reads, isolation and deletion. No Instagram events changed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Create and remove three tiny synthetic Azure blobs (billable operations)')
    if not parser.parse_args().live:
        parser.error('--live is required')
    main()
