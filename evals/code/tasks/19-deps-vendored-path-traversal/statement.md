Read `SECURITY-ADVISORY.md`. The vendored copy of `packkit` in `vendor/packkit` is affected. The fixed release
is already available offline in `vendor_dist/packkit-2.0.0/` (with its changelog).

1. Replace the vendored library with release 2.0.0: `vendor/packkit/` must become an exact copy of
   `vendor_dist/packkit-2.0.0/packkit/`.
2. Adapt the application (`app/`) to the 2.0.0 API so that `restore_backup` and `install_plugin` keep their
   current signatures and behaviour for valid archives.
3. An unsafe archive must be refused with `app.errors.RejectedArchive` (never another exception type), and
   nothing may be written outside the destination directory.

Do not modify `vendor_dist/` or `SECURITY-ADVISORY.md`. Existing tests must keep passing.
