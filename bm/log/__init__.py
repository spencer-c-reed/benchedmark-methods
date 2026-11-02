from __future__ import annotations
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
from typing import Iterator
ROOT = Path(__file__).resolve().parents[2]
from bm.paths import runtime_root
BLOBS = runtime_root(ROOT) / 'blobs'
GENESIS_PREV = '0' * 64

def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()

def put_blob(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    h = hashlib.sha256(data).hexdigest()
    p = BLOBS / h[:2] / f'{h}.gz'
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp%d' % os.getpid())
        with gzip.open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, p)
    return h

def get_blob(h: str) -> bytes:
    path = BLOBS / h[:2] / f'{h}.gz'
    if not path.exists():
        path = ROOT / 'blobs' / h[:2] / f'{h}.gz'
    with gzip.open(path, 'rb') as f:
        return f.read()

class ChainLog:

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def rows(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with open(self.path, encoding='utf-8') as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    if raw.endswith('\n'):
                        raise
                    import sys
                    print(f'ChainLog: skipped a torn final line in {self.path} ({len(raw)} bytes)', file=sys.stderr)
                    return

    def _archive_torn_tail(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with open(self.path, 'rb+') as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) == b'\n':
                return
            f.seek(0)
            data = f.read()
            cut = data.rfind(b'\n') + 1
            n = 1
            while self.path.with_name(f'{self.path.name}.torn-{n}').exists():
                n += 1
            side = self.path.with_name(f'{self.path.name}.torn-{n}')
            side.write_bytes(data[cut:])
            f.truncate(cut)
            f.flush()
            os.fsync(f.fileno())

    def payloads(self) -> list[dict]:
        return [dict(r['payload'], seq=r['seq']) for r in self.rows()]

    def head(self) -> tuple[int, str]:
        if not self.path.exists():
            return (0, GENESIS_PREV)
        with open(self.path, 'rb') as f:
            pos = f.seek(0, os.SEEK_END)
            buf, block = (b'', 1 << 16)
            while pos > 0:
                n = min(block, pos)
                pos -= n
                f.seek(pos)
                buf = f.read(n) + buf
                lines = buf.split(b'\n')
                first = 1 if pos > 0 else 0
                for i in range(len(lines) - 1, first - 1, -1):
                    raw = lines[i].strip()
                    if not raw:
                        continue
                    try:
                        r = json.loads(raw)
                    except ValueError:
                        if i == len(lines) - 1:
                            continue
                        raise
                    return (r['seq'], r['hash'])
                block *= 2
        return (0, GENESIS_PREV)

    def append(self, payload: dict) -> dict:
        lock = self.path.with_suffix(self.path.suffix + '.lock')
        with open(lock, 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            self._archive_torn_tail()
            seq, prev = self.head()
            body = canonical(payload)
            ph = hashlib.sha256(body).hexdigest()
            h = hashlib.sha256(prev.encode() + body).hexdigest()
            row = {'seq': seq + 1, 'ts': payload.get('ts'), 'type': payload.get('type'), 'payload_hash': ph, 'payload': payload, 'prev_hash': prev, 'hash': h}
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(row, ensure_ascii=False, separators=(',', ':'), default=str) + '\n')
                f.flush()
                os.fsync(f.fileno())
            return row

    def append_many(self, payloads: list[dict]) -> list[dict]:
        if not payloads:
            return []
        lock = self.path.with_suffix(self.path.suffix + '.lock')
        with open(lock, 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            self._archive_torn_tail()
            seq, prev = self.head()
            rows, lines = ([], [])
            for payload in payloads:
                body = canonical(payload)
                h = hashlib.sha256(prev.encode() + body).hexdigest()
                seq += 1
                row = {'seq': seq, 'ts': payload.get('ts'), 'type': payload.get('type'), 'payload_hash': hashlib.sha256(body).hexdigest(), 'payload': payload, 'prev_hash': prev, 'hash': h}
                rows.append(row)
                lines.append(json.dumps(row, ensure_ascii=False, separators=(',', ':'), default=str) + '\n')
                prev = h
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(''.join(lines))
                f.flush()
                os.fsync(f.fileno())
            return rows

    def verify(self) -> tuple[bool, int, str]:
        prev = GENESIS_PREV
        n = 0
        for r in self.rows():
            body = canonical(r['payload'])
            if hashlib.sha256(body).hexdigest() != r['payload_hash']:
                return (False, r['seq'], 'payload_hash mismatch')
            if r['prev_hash'] != prev or hashlib.sha256(prev.encode() + body).hexdigest() != r['hash']:
                return (False, r['seq'], 'chain mismatch')
            prev = r['hash']
            n += 1
        return (True, n, prev)
