import json
import subprocess
import sys
import unittest

def run(*args):
    return subprocess.run(
        [sys.executable, "-m", "inventory.cli", "report", *args],
        capture_output=True, text=True, cwd="/work",
    )


class HiddenJson(unittest.TestCase):
    def test_json_stock2(self):
        r = run("/check/stock2.csv", "--format", "json")
        self.assertEqual(r.returncode, 0, r.stderr)
        expected = {
            "items": [
                {"sku": "K-5", "name": "Kit", "quantity": 7, "value": "7.70"},
                {"sku": "M-1", "name": "Mesh, fine", "quantity": 2, "value": "20.01"},
                {"sku": "Q-0", "name": "Quartz", "quantity": 1, "value": "0.00"},
                {"sku": "Z-9", "name": "Zinc washers", "quantity": 3, "value": "0.38"},
            ],
            "total_value": "28.09",
        }
        self.assertEqual(r.stdout, json.dumps(expected, indent=2) + "\n")

    def test_json_min_quantity(self):
        r = run("data/stock.csv", "--format", "json", "--min-quantity", "300")
        self.assertEqual(r.returncode, 0, r.stderr)
        expected = {
            "items": [
                {"sku": "B-200", "name": "Blue bolts", "quantity": 1200, "value": "42.00"},
                {"sku": "C-300", "name": "Copper wire (m)", "quantity": 340, "value": "338.30"},
            ],
            "total_value": "380.30",
        }
        self.assertEqual(r.stdout, json.dumps(expected, indent=2) + "\n")

    def test_option_order(self):
        r = run("--format=json", "data/stock.csv")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["total_value"], "616.80")

    def test_text_unchanged(self):
        default = run("data/stock.csv", "--min-quantity", "15")
        explicit = run("data/stock.csv", "--min-quantity", "15", "--format", "text")
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(default.stdout, explicit.stdout)
        lines = default.stdout.splitlines()
        self.assertEqual(lines[0], "SKU     NAME                   QTY       VALUE")
        self.assertEqual(lines[1], "A-100   Anchor plates           15      187.50")
        self.assertEqual(lines[-1], "TOTAL                                   567.80")

    def test_bad_format(self):
        r = run("data/stock.csv", "--format", "xml")
        self.assertEqual(r.returncode, 2)
