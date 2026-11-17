"""Trip answers on staging -110 (2026-11-17, seven trips on a phone, one by API).

Search-engine redirect links were shown as sources, and after timed trains
were left out of a Tokyo-Kyoto answer a ranking item and a pointer to "the
window above" stayed with nothing to refer to.
"""

import unittest

from services.orchestrator import travel_claims
from services.orchestrator.redirects import resolved


class RedirectTests(unittest.TestCase):
    def test_redirects_resolve_or_go(self) -> None:
        page = "https://rail.example.test/horarios"
        self.assertEqual(resolved(page), page)
        self.assertEqual(
            resolved(
                "https://www.google.es/url?q=https%3A%2F%2Frail.example.test%2Fhorarios&sa=U"
            ),
            page,
        )
        self.assertEqual(
            resolved(
                "https://duckduckgo.com/l/?uddg=https%3A%2F%2Frail.example.test%2Fhorarios"
            ),
            page,
        )
        self.assertIsNone(resolved("https://www.google.es/goto?url=CAESjgEBxCJF1"))
        # A site's own page named "goto" is not a search redirect.
        self.assertEqual(
            resolved("https://rail.example.test/goto?url=x"),
            "https://rail.example.test/goto?url=x",
        )


class OrphanTests(unittest.TestCase):
    def test_what_referred_to_removed_trains_goes(self) -> None:
        turn = travel_claims.Turn(asked=["kyoto station tomorow before 10 am"])
        turn.activate()
        turn.collect(
            {
                "link": "https://rail.example.test/t",
                "snippet": "Tokaido Shinkansen timetable.",
            }
        )
        answer = (
            "To reach Kyoto Station before 10:00, the rule of thumb is:\n\n"
            "- Nozomi: leave Tokyo by 7:45 AM, about 2 h 10 min.\n"
            "- Hikari: leave by 7:00 AM.\n"
            "- Kodama: too slow, skip it for this deadline.\n\n"
            "Confirm on the operator's site before boarding, but the departure-time "
            "window above is the reliable filter. Have a good trip."
        )
        kept = turn.kept(answer, turn.findings(answer))
        self.assertNotIn("Kodama", kept)
        self.assertNotIn("window above", kept)
        self.assertNotIn("rule of thumb", kept)
        self.assertIn("Confirm on the operator's site before boarding.", kept)
        self.assertIn("Have a good trip.", kept)


if __name__ == "__main__":
    unittest.main()
