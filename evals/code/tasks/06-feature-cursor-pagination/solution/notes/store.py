import itertools
from dataclasses import dataclass


@dataclass(frozen=True)
class Note:
    id: int
    created_at: float
    text: str


class NoteStore:
    def __init__(self, clock):
        self._clock = clock
        self._ids = itertools.count(1)
        self._notes = {}

    def add(self, text):
        note = Note(next(self._ids), self._clock(), text)
        self._notes[note.id] = note
        return note

    def delete(self, note_id):
        self._notes.pop(note_id, None)

    def list_notes(self):
        return sorted(self._notes.values(), key=lambda n: (n.created_at, n.id))

    def list_page(self, limit, cursor=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an int from 1 to 100")
        ordered = self.list_notes()
        if cursor is not None:
            after = _decode(cursor)
            ordered = [n for n in ordered if (n.created_at, n.id) > after]
        page = ordered[:limit]
        more = len(ordered) > limit
        return page, (_encode(page[-1]) if more else None)


import base64
import hmac
import json
import hashlib

_KEY = b"notes-cursor-v1"


def _encode(note):
    body = json.dumps([note.created_at, note.id]).encode()
    tag = hmac.new(_KEY, body, hashlib.sha256).hexdigest()[:16]
    return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + tag


def _decode(cursor):
    try:
        raw, tag = cursor.split(".")
        body = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        good = hmac.new(_KEY, body, hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(good, tag):
            raise ValueError
        created_at, note_id = json.loads(body)
        return (float(created_at), int(note_id))
    except Exception:
        raise ValueError("invalid cursor") from None
