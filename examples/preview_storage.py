"""Private Azure media; SQLite stores only ownership, expiry and deletion metadata."""
import hashlib
import io
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, BlobSasPermissions, ContentSettings, generate_blob_sas
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 50 * 1024 * 1024


def media_type(data, kind):
    if not data or len(data) > MAX_BYTES:
        raise ValueError('Preview is empty or exceeds 50 MB')
    if kind == 'image':
        with Image.open(io.BytesIO(data)) as img:
            mime = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'GIF': 'image/gif', 'WEBP': 'image/webp'}.get(img.format)
            img.verify()
            if mime:
                return mime
    elif kind in {'audio', 'video'}:
        if data[4:8] == b'ftyp':
            return 'video/mp4' if kind == 'video' else 'audio/mp4'
        if data.startswith(b'\x1aE\xdf\xa3'):
            return 'video/webm' if kind == 'video' else 'audio/webm'
        if kind == 'audio':
            if data.startswith(b'OggS'):
                return 'audio/ogg'
            if data.startswith(b'RIFF') and data[8:12] == b'WAVE':
                return 'audio/wav'
            if data.startswith(b'fLaC'):
                return 'audio/flac'
            if data.startswith(b'ID3') or (len(data) > 2 and data[0] == 255 and data[1] & 0xE0 == 0xE0):
                return 'audio/mpeg'
    raise ValueError('Unsupported preview media format')


class PreviewStore:
    def __init__(self, getter=None, db_path=None, client=None, clock=time.time):
        get = getter or os.environ.get
        self.account = get('AZURE_STORAGE_ACCOUNT', '')
        self.key = get('AZURE_STORAGE_KEY', '')
        self.container = get('AZURE_STORAGE_CONTAINER', 'instagram-previews')
        self.enabled = bool(self.account and self.key)
        self.clock = clock
        self.db_path = Path(db_path or ROOT / 'tools/accounts/previews.sqlite3')
        self.client = client
        if self.enabled:
            if not re.fullmatch(r'[a-z0-9]{3,24}', self.account) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', self.container):
                raise ValueError('Invalid storage account or container name')
            self.client = client or BlobServiceClient(
                f'https://{self.account}.blob.core.windows.net', credential=self.key,
                connection_timeout=10, read_timeout=30, retry_total=2)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self.db() as db:
                db.execute('''CREATE TABLE IF NOT EXISTS previews (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, blob TEXT NOT NULL, kind TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL, mime TEXT)''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def owner(account_id):
        return hashlib.sha256(str(account_id).encode()).hexdigest()

    def row(self, account_id, preview_id):
        if not self.enabled or not re.fullmatch(r'[a-f0-9]{64}', preview_id):
            return None
        with self.db() as db:
            return db.execute('SELECT * FROM previews WHERE id=? AND owner=?',
                              (preview_id, self.owner(account_id))).fetchone()

    def blob(self, row):
        return self.client.get_blob_client(self.container, row['blob'])

    def descriptor(self, row):
        state = row['state']
        if state not in {'deleted', 'deleting'} and row['expires'] <= self.clock():
            state = 'expired'
        return {'id': row['id'], 'kind': row['kind'], 'state': state, 'expires_at': row['expires']}

    def capture(self, account_id, event_id, index, kind, data):
        if not self.enabled:
            return {'kind': kind, 'state': 'disabled'}
        if not account_id or not event_id:
            raise ValueError('Preview must belong to an account and event')
        owner = self.owner(account_id)
        preview_id = hashlib.sha256(f'{owner}\0{event_id}\0{index}'.encode()).hexdigest()
        created = self.clock()
        # Seven days matches the provisioned lifecycle policy. Retries do not reset expiry.
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO previews VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (preview_id, owner, f'{owner}/{preview_id}', kind, created, created + 7 * 86400, 'pending', None))
        row = self.row(account_id, preview_id)
        if self.descriptor(row)['state'] != 'pending':
            return self.descriptor(row)
        mime = media_type(data, kind)
        blob = self.blob(row)
        try:
            blob.upload_blob(data, overwrite=False, content_settings=ContentSettings(
                content_type=mime, content_disposition='inline', cache_control='private, no-store'),
                metadata={'expires_at': str(int(row['expires']))})
        except ResourceExistsError:
            props = blob.get_blob_properties()
            # A second installation must not extend the original cloud retention period.
            if isinstance(props.metadata, dict) and props.metadata.get('expires_at'):
                expiry = float(props.metadata['expires_at'])
                with self.db() as db:
                    db.execute('UPDATE previews SET expires=MIN(expires, ?) WHERE id=?', (expiry, preview_id))
        with self.db() as db:
            changed = db.execute("UPDATE previews SET state='available', mime=? WHERE id=? AND state='pending'",
                                 (mime, preview_id)).rowcount
        current = self.row(account_id, preview_id)
        if not changed and current['state'] in {'deleted', 'deleting'}:
            self._delete_blob(current)  # A user deleted the preview while upload was running.
        return self.descriptor(current)

    def link(self, account_id, preview_id):
        row = self.row(account_id, preview_id)
        if row is None:
            raise KeyError('Preview not found')
        result = self.descriptor(row)
        if result['state'] != 'available':
            return result
        try:
            self.blob(row).get_blob_properties()
        except ResourceNotFoundError:
            result['state'] = 'expired'
            return result
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        expiry = min(now + timedelta(minutes=5), datetime.fromtimestamp(row['expires'], timezone.utc))
        sas = generate_blob_sas(self.account, self.container, row['blob'], account_key=self.key,
                                permission=BlobSasPermissions(read=True), start=now - timedelta(minutes=1),
                                expiry=expiry, protocol='https', cache_control='private, no-store',
                                content_type=row['mime'], content_disposition='inline')
        result.update(url=f'https://{self.account}.blob.core.windows.net/{self.container}/{row["blob"]}?{sas}',
                      link_expires_at=expiry.timestamp())
        return result

    def _delete_blob(self, row):
        try:
            self.blob(row).delete_blob(delete_snapshots='include')
        except ResourceNotFoundError:
            pass

    def delete(self, account_id, preview_id):
        row = self.row(account_id, preview_id)
        if row is None:
            raise KeyError('Preview not found')
        # Tombstone before networking so retries cannot resurrect deleted media.
        with self.db() as db:
            db.execute("UPDATE previews SET state='deleting' WHERE id=? AND owner=?", (preview_id, row['owner']))
        self._delete_blob(row)
        with self.db() as db:
            db.execute("UPDATE previews SET state='deleted' WHERE id=?", (preview_id,))
        return {'id': preview_id, 'state': 'deleted'}

    def close(self):
        if self.client is not None:
            self.client.close()
