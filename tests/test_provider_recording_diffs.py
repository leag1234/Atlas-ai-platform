"""The recording matcher must describe lists that differ by more than one element."""

import unittest

from provider_recording import _normalise, json_diffs


class JsonDiffTests(unittest.TestCase):
    def test_longer_current_list_reports_every_extra_element(self) -> None:
        self.assertEqual(
            json_diffs([1], [1, 2, 3]), ["[1]: missing -> 2", "[2]: missing -> 3"]
        )

    def test_shorter_current_list_reports_every_missing_element(self) -> None:
        self.assertEqual(
            json_diffs([1, 2, 3], [1]), ["[1]: 2 -> missing", "[2]: 3 -> missing"]
        )

    def test_equal_lists_have_no_difference(self) -> None:
        self.assertEqual(json_diffs([1, 2], [1, 2]), [])


class LocalOriginTests(unittest.TestCase):
    def test_local_ports_do_not_change_a_recorded_request(self) -> None:
        recorded = {"content": "![Image](http://localhost:12345/images/abc)"}
        current = {"content": "![Image](http://localhost:23456/images/abc)"}
        self.assertEqual(
            _normalise(recorded, frozenset()), _normalise(current, frozenset())
        )

    def test_public_origins_are_kept(self) -> None:
        self.assertEqual(
            _normalise("https://example.org:8443/x", frozenset()),
            "https://example.org:8443/x",
        )


if __name__ == "__main__":
    unittest.main()
