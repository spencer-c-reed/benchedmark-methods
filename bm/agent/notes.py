from __future__ import annotations
NOTE_KEYS = ('memo', 'private_notes')

def note_key(rec: dict) -> str:
    return 'memo' if 'memo' in rec else 'private_notes'

def note_text(rec: dict) -> str:
    for k in NOTE_KEYS:
        v = rec.get(k)
        if v:
            return str(v)
    return ''

def carry_note(rec: dict, n: int) -> dict:
    return {note_key(rec): note_text(rec)[:n]}
