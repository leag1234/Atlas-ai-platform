import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from taskq.cli import main


class CliTest(unittest.TestCase):
    def test_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "t.json")
            with redirect_stdout(io.StringIO()):
                main(["--file", f, "add", "write docs", "--priority", "high", "--tag", "doc"])
                main(["--file", f, "add", "fix bug"])
                main(["--file", f, "done", "2"])
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--file", f, "list", "--all"])
            self.assertEqual(out.getvalue(), "   1 [ ] high   write docs\n   2 [x] normal fix bug\n")


if __name__ == "__main__":
    unittest.main()
