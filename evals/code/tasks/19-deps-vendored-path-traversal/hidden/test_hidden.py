import io
import json
import os
import tarfile
import tempfile
import unittest

from app.backup import restore_backup
from app.errors import RejectedArchive
from app.plugins import install_plugin


def add(tar, name, data=b"x", kind=tarfile.REGTYPE, link=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = link
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)


class HiddenTraversal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(self.outside)

    def archive(self, members):
        path = os.path.join(self.tmp, f"a{len(os.listdir(self.tmp))}.tar")
        with tarfile.open(path, "w") as tar:
            for m in members:
                add(tar, *m)
        return path

    def evil_sets(self):
        return [
            [("manifest.json", b"{}"), ("../outside/evil.txt", b"pwned")],
            [("manifest.json", b"{}"), (os.path.join(self.outside, "abs.txt"), b"pwned")],
            [("manifest.json", b"{}"), ("ln", b"", tarfile.SYMTYPE, self.outside), ("ln/through.txt", b"pwned")],
            [("manifest.json", b"{}"), ("ln2", b"", tarfile.SYMTYPE, "../outside")],
            [("plugin.json", b'{"name": "p"}'), ("a/../../outside/x.txt", b"pwned")],
        ]

    def test_restore_rejects(self):
        for i, members in enumerate(self.evil_sets()):
            dest = os.path.join(self.tmp, f"restore{i}")
            with self.assertRaises(RejectedArchive, msg=str(members)):
                restore_backup(self.archive(members), dest)
            self.assertEqual(os.listdir(self.outside), [], members)

    def test_plugin_rejects(self):
        plugins = os.path.join(self.tmp, "plugins")
        os.makedirs(plugins)
        for members in self.evil_sets():
            members = [("plugin.json", b'{"name": "p"}')] + members[1:]
            with self.assertRaises(RejectedArchive, msg=str(members)):
                install_plugin(self.archive(members), plugins)
            self.assertEqual(os.listdir(self.outside), [])
            self.assertFalse(os.path.exists(os.path.join(plugins, "p")))

    def test_valid_still_work(self):
        good = self.archive([("manifest.json", json.dumps({"v": 1}).encode()), ("d/e/f.txt", b"ok"), ("d/link", b"", tarfile.SYMTYPE, "e/f.txt")])
        dest = os.path.join(self.tmp, "deep", "new", "dest")
        self.assertEqual(restore_backup(good, dest), {"v": 1})
        with open(os.path.join(dest, "d", "link")) as f:
            self.assertEqual(f.read(), "ok")
        plugins = os.path.join(self.tmp, "plugins2")
        os.makedirs(plugins)
        self.assertEqual(install_plugin(self.archive([("plugin.json", b'{"name": "q"}'), ("q.py", b"1")]), plugins), "q")
        self.assertTrue(os.path.exists(os.path.join(plugins, "q", "q.py")))

    def test_vendored_version(self):
        import packkit
        self.assertEqual(packkit.__version__, "2.0.0")
        self.assertTrue(packkit.__file__.startswith("/work/vendor/packkit"))
        self.assertFalse(hasattr(packkit, "extract_all"))
