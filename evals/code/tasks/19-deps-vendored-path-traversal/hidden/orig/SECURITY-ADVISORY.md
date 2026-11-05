# PKK-2026-0001: path traversal in packkit < 2.0.0

`packkit.extract_all()` extracts every archive member without checking its path. A crafted archive with
members such as `../../etc/cron.d/job`, absolute paths, or symbolic links pointing outside the destination can
write files anywhere the process can write (CWE-22). This is the same class of defect as CVE-2007-4559 in
Python's `tarfile` module.

Affected: all releases before 2.0.0. Fixed in 2.0.0, which validates every member before writing anything and
raises `packkit.UnsafeArchiveError`. The 2.0.0 API is not backward compatible; see its changelog.
