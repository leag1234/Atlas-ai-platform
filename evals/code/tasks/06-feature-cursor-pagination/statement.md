Add cursor pagination to `notes.store.NoteStore`:

```python
def list_page(self, limit, cursor=None) -> tuple[list[Note], str | None]:
```

- Notes are returned oldest first, ordered by `(created_at, id)`.
- `limit` must be an int from 1 to 100, otherwise raise `ValueError`.
- The second element is an opaque string cursor for the next page, or `None` when there are no more notes
  after this page.
- Paging must be stable: notes added or deleted while a client pages must never cause a remaining note to
  be skipped or returned twice (notes created during paging may or may not appear, depending on their
  position).
- A cursor that was not produced by `list_page` (malformed or tampered string) raises `ValueError`.
- The existing `list_notes()` method keeps working.
