import dataclasses
import json
import pathlib
import subprocess
import sys
import unittest
from decimal import Decimal

from shipping.config import Settings, load_settings
from shipping.invoice import render
from shipping.labels import make_label
from shipping.rates import quote


class HiddenSettings(unittest.TestCase):
    def test_defaults(self):
        s = Settings()
        self.assertEqual((s.currency, s.base_fee, s.per_kg, s.free_above, s.label_prefix),
                         ("EUR", Decimal("4.90"), Decimal("1.20"), Decimal("60.00"), "SHP"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            s.currency = "USD"

    def test_two_carriers_same_process(self):
        a = Settings()
        b = Settings(currency="CHF", base_fee=Decimal("9.00"), per_kg=Decimal("0.50"), free_above=None, label_prefix="ALP")
        self.assertEqual(quote(2.5, "10", a), Decimal("7.90"))
        self.assertEqual(quote(2.5, "100", b), Decimal("10.25"))
        self.assertEqual(quote(2.5, "100", a), Decimal("0.00"))
        self.assertEqual(make_label(42, b), "ALP-00000042")
        self.assertEqual(render(7, 1, "5", a), "SHP-00000007 shipping 6.10 EUR")
        self.assertEqual(render(7, 1, "5", b), "ALP-00000007 shipping 9.50 CHF")

    def test_load_settings(self):
        p = pathlib.Path("/tmp/s.json")
        p.write_text(json.dumps({"currency": "GBP", "per_kg": "2.00", "free_above": None}))
        s = load_settings(p)
        self.assertEqual((s.currency, s.per_kg, s.free_above, s.base_fee), ("GBP", Decimal("2.00"), None, Decimal("4.90")))
        p.write_text(json.dumps({"colour": "red"}))
        with self.assertRaises(ValueError):
            load_settings(p)

    def test_cli(self):
        r = subprocess.run([sys.executable, "-m", "shipping.cli", "7", "1", "5"], cwd="/work", capture_output=True, text=True)
        self.assertEqual(r.stdout, "SHP-00000007 shipping 6.10 EUR\n", r.stderr)
        p = pathlib.Path("/tmp/c.json")
        p.write_text(json.dumps({"label_prefix": "ZZ", "base_fee": "1.00", "currency": "SEK"}))
        r = subprocess.run([sys.executable, "-m", "shipping.cli", "3", "2", "5", "--settings", str(p)], cwd="/work", capture_output=True, text=True)
        self.assertEqual(r.stdout, "ZZ-00000003 shipping 3.40 SEK\n", r.stderr)

    def test_no_global(self):
        for path in pathlib.Path("/work/shipping").rglob("*.py"):
            self.assertNotIn("CONFIG", path.read_text(), str(path))
        import shipping.config as config
        self.assertFalse(any(isinstance(v, (dict, Settings)) for k, v in vars(config).items() if not k.startswith("__")))
