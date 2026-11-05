import json
import os

import app  # noqa: F401  (sets up the vendored path)
import packkit


def restore_backup(archive_path, dest):
    """Restore a backup archive into ``dest`` (created if missing) and return its manifest dict."""
    packkit.extract_all(archive_path, dest)
    with open(os.path.join(dest, "manifest.json"), encoding="utf-8") as handle:
        return json.load(handle)
