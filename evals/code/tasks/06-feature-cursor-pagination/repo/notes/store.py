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
