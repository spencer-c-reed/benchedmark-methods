from __future__ import annotations
import json
import os
from pathlib import Path

def runtime_root(code_root: Path) -> Path:
    override = os.environ.get('BM_RUNTIME_ROOT')
    if override:
        return Path(override).expanduser().resolve()
    root = Path(code_root).resolve()
    release = root / 'RELEASE'
    if release.exists():
        return Path(json.loads(release.read_text())['serving']).resolve()
    marker = root / '.git'
    if marker.is_file():
        text = marker.read_text().strip()
        if not text.startswith('gitdir: '):
            raise ValueError(f'invalid Git worktree marker: {marker}')
        gitdir = (root / text[len('gitdir: '):]).resolve()
        common_file = gitdir / 'commondir'
        if common_file.exists():
            common = (gitdir / common_file.read_text().strip()).resolve()
            if common.name != '.git':
                raise ValueError('Cannot identify shared checkout; set BM_RUNTIME_ROOT explicitly')
            return common.parent
    return root

def gateway_logs(code_root: Path) -> list[Path]:
    paths = [Path(code_root) / 'logs' / 'gateway.jsonl', runtime_root(code_root) / 'logs' / 'gateway.jsonl']
    return list(dict.fromkeys((p.resolve() for p in paths)))
