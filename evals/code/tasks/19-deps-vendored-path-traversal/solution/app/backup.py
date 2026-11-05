import json
import os

import app  # noqa: F401  (sets up the vendored path)
import packkit
from app.errors import RejectedArchive


def restore_backup(archive_path, dest):
    """Restore a backup archive into ``dest`` (created if missing) and return its manifest dict."""
    os.makedirs(dest, exist_ok=True)
    try:
        packkit.extract(archive_path, dest)
    except packkit.UnsafeArchiveError as error:
        raise RejectedArchive(str(error)) from error
    with open(os.path.join(dest, "manifest.json"), encoding="utf-8") as handle:
        return json.load(handle)
