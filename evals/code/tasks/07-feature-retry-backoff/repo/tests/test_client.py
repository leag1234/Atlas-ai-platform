import unittest

from fetcher import Client, HTTPError


class ClientTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(Client(lambda url: (200, b'{"a": 1}')).get_json("u"), {"a": 1})

    def test_404(self):
        with self.assertRaises(HTTPError):
            Client(lambda url: (404, b"")).get_json("u")


if __name__ == "__main__":
    unittest.main()
