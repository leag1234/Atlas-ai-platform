import importlib
import os
import subprocess
import sys
import unittest

NAMES = {
    "models": ["Entry", "Account", "LedgerError"],
    "parse": ["parse_amount", "parse_line", "parse_file"],
    "report": ["balances", "format_report", "monthly_totals"],
}

EXPECTED = 'assets:bank               400.00\nassets:cash                57.65\nexpenses:food              42.35\nexpenses:rent            1900.00\nincome:salary           -2400.00\n\nexpenses 2026-01          950.00\nexpenses 2026-02          992.35\n'


class HiddenSplit(unittest.TestCase):
    def test_layout(self):
        self.assertFalse(os.path.exists("/work/ledger.py"))
        self.assertTrue(os.path.isdir("/work/ledger"))

    def test_definitions(self):
        for mod, names in NAMES.items():
            module = importlib.import_module("ledger." + mod)
            for name in names:
                obj = getattr(module, name)
                self.assertEqual(obj.__module__, "ledger." + mod, name)

    def test_package_exports(self):
        import ledger

        expected = sorted(n for names in NAMES.values() for n in names)
        self.assertEqual(sorted(ledger.__all__), expected)
        for name in expected:
            self.assertTrue(hasattr(ledger, name))

    def test_each_module_imports_alone(self):
        for mod in NAMES:
            r = subprocess.run([sys.executable, "-c", f"import ledger.{mod}"], cwd="/work", capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_cli(self):
        r = subprocess.run([sys.executable, "-m", "ledger.report", "sample.ledger"], cwd="/work", capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, EXPECTED), r.stderr)
        r = subprocess.run([sys.executable, "-m", "ledger.report"], cwd="/work", capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        with open("/tmp/bad.ledger", "w") as f:
            f.write("2026-01-01 | a | b | zero\n")
        r = subprocess.run([sys.executable, "-m", "ledger.report", "/tmp/bad.ledger"], cwd="/work", capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("error:", r.stderr)
