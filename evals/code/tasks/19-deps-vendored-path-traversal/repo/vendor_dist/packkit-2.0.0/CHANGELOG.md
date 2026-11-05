# packkit changelog

## 2.0.0

Security release for PKK-2026-0001.

Breaking changes:
- `extract_all(archive_path, dest)` is removed. Use `extract(archive_path, dest)`.
- `extract()` validates every member first and raises `UnsafeArchiveError` before writing anything if a member
  has an absolute path, escapes `dest` with `..`, is a link pointing outside `dest`, or is a device or FIFO.
- `extract()` no longer creates `dest`: it raises `FileNotFoundError` when `dest` does not exist.
- `extract()` returns the list of extracted member names.

Unchanged: `list_members(archive_path)`.
