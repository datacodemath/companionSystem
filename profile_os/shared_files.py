"""Globally shared, create-only UTF-8 files, published atomically on disk."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from .errors import FileNotFoundInStore, MalformedRecord


class SharedFileExists(Exception):
    pass


class SharedFiles:
    def __init__(self, store):
        self.store = store
        self.directory = store.data_dir / 'shared_files'
        self.directory.mkdir(exist_ok=True)

    def create(self, profile_id, filename, content):
        self.store._require_profile(profile_id)
        self.store._validate_filename(filename)
        if not self.store._FILENAME_RE.fullmatch(filename):
            raise MalformedRecord("invalid shared filename")
        if not isinstance(content, str):
            raise MalformedRecord('content must be a string')
        data = content.encode('utf-8')
        if len(data) > self.store.MAX_FILE_BYTES:
            raise MalformedRecord('shared file too large (max 262144 bytes)')
        entry = dict(filename=filename, created_by=profile_id, created_at=time.time(),
                     size=len(data), sha256=hashlib.sha256(data).hexdigest(), content=content)
        # Build privately, fsync, then link without replacement. Readers never
        # see partial content; even concurrent creators have only one winner.
        fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(entry, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o444)
            try:
                os.link(temporary, self.directory / (filename + '.json'))
            except FileExistsError as error:
                raise SharedFileExists('shared filename already exists; use a new filename') from error
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {k: v for k, v in entry.items() if k != 'content'}

    def read(self, profile_id, filename):
        self.store._require_profile(profile_id)
        self.store._validate_filename(filename)
        if not self.store._FILENAME_RE.fullmatch(filename):
            raise MalformedRecord("invalid shared filename")
        path = self.directory / (filename + '.json')
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError as error:
            raise FileNotFoundInStore('shared', filename) from error
        with os.fdopen(fd, encoding='utf-8') as handle:
            return json.load(handle)

    def list(self, profile_id, limit=100, after=None):
        self.store._require_profile(profile_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise MalformedRecord('limit must be between 1 and 200')
        if after is not None:
            self.store._validate_filename(after)
        names = sorted(p.name[:-5] for p in self.directory.glob('*.json')
                       if p.is_file() and not p.is_symlink())
        names = [name for name in names if after is None or name > after][:limit+1]
        entries = [self.read(profile_id, name) for name in names[:limit]]
        return {'items': [{k:v for k,v in entry.items() if k != 'content'} for entry in entries],
                'next_after': names[limit-1] if len(names) > limit else None}
