import io
import json
import os
import tarfile
import tempfile
import unittest

from app.backup import restore_backup
from app.plugins import install_plugin


def make_tar(path, files):
    with tarfile.open(path, "w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


class AppTest(unittest.TestCase):
    def test_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "b.tar")
            make_tar(archive, {"manifest.json": json.dumps({"version": 3}).encode(), "data/a.txt": b"a"})
            self.assertEqual(restore_backup(archive, os.path.join(tmp, "out")), {"version": 3})
            self.assertTrue(os.path.exists(os.path.join(tmp, "out", "data", "a.txt")))

    def test_plugin(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "p.tar")
            make_tar(archive, {"plugin.json": b'{"name": "hello"}', "main.py": b"print(1)"})
            os.makedirs(os.path.join(tmp, "plugins"))
            self.assertEqual(install_plugin(archive, os.path.join(tmp, "plugins")), "hello")
            self.assertTrue(os.path.exists(os.path.join(tmp, "plugins", "hello", "main.py")))


if __name__ == "__main__":
    unittest.main()
