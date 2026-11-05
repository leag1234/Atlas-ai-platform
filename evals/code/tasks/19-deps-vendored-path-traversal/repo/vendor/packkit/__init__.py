"""packkit: small helpers around tar archives."""

import os
import tarfile

__version__ = "1.4.0"


def extract_all(archive_path, dest):
    """Extract every member of ``archive_path`` into ``dest`` (created if needed)."""
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(archive_path) as tar:
        tar.extractall(dest)


def list_members(archive_path):
    with tarfile.open(archive_path) as tar:
        return [m.name for m in tar.getmembers()]
