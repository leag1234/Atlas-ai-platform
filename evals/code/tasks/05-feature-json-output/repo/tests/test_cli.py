import io
import unittest
from contextlib import redirect_stdout

from inventory.cli import main


class CliTest(unittest.TestCase):
    def test_text_total(self):
        out = io.StringIO()
        with redirect_stdout(out):
            main(["report", "data/stock.csv"])
        self.assertIn("TOTAL", out.getvalue())
        self.assertTrue(out.getvalue().splitlines()[-1].endswith("616.80"))


if __name__ == "__main__":
    unittest.main()
