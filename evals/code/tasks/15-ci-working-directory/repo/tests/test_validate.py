import unittest

from orders.validate import errors


class ValidateTest(unittest.TestCase):
    def test_good(self):
        self.assertEqual(errors({"id": "1", "customer": "c", "lines": [{"sku": "s", "quantity": 1, "unit_price": 2}]}), [])

    def test_bad(self):
        self.assertEqual(
            errors({"id": 1, "lines": [{"sku": "s", "quantity": True}]}),
            ["id must be string", "missing customer", "lines[0].quantity must be integer", "lines[0]: missing unit_price"],
        )


if __name__ == "__main__":
    unittest.main()
