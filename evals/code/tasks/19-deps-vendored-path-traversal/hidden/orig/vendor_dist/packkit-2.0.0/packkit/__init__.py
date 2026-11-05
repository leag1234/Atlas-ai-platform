"""packkit: small helpers around tar archives."""

import os
import tarfile

__version__ = "2.0.0"


class UnsafeArchiveError(Exception):
    """The archive contains a member that would be written outside the destination."""


def _inside(root, path):
    return path == root or path.startswith(root + os.sep)


def _check(tar, root):
    members = tar.getmembers()
    for member in members:
        name = member.name
        if os.path.isabs(name) or name.startswith(("/", "\\")):
            raise UnsafeArchiveError(f"absolute path: {name}")
        if member.isdev() or member.isfifo():
            raise UnsafeArchiveError(f"special file: {name}")
        target = os.path.realpath(os.path.join(root, name))
        if not _inside(root, target):
            raise UnsafeArchiveError(f"path escapes destination: {name}")
        if member.issym() or member.islnk():
            if os.path.isabs(member.linkname):
                raise UnsafeArchiveError(f"absolute link: {name}")
            base = os.path.dirname(target) if member.issym() else root
            if not _inside(root, os.path.realpath(os.path.join(base, member.linkname))):
                raise UnsafeArchiveError(f"link escapes destination: {name}")
    return members


def extract(archive_path, dest):
    """Validate, then extract ``archive_path`` into the existing directory ``dest``."""
    if not os.path.isdir(dest):
        raise FileNotFoundError(dest)
    root = os.path.realpath(dest)
    with tarfile.open(archive_path) as tar:
        members = _check(tar, root)
        tar.extractall(root, members=members, filter="data")
    return [m.name for m in members]


def list_members(archive_path):
    with tarfile.open(archive_path) as tar:
        return [m.name for m in tar.getmembers()]
