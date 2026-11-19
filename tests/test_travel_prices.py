"""Prices in trip answers are checked like other travel values."""

import unittest

from services.orchestrator import travel_claims


def turn_with(*sources: str, asked: str = "") -> travel_claims.Turn:
    turn = travel_claims.Turn(asked=[asked] if asked else [])
    turn.activate()
    for text in sources:
        turn.collect({"title": "", "snippet": text})
    return turn


class PriceTests(unittest.TestCase):
    def test_prices_without_a_page_are_left_out(self) -> None:
        claims = (
            "The fare is 17 AUD.",
            "The access fee costs €15.",
            "Tickets cost 6 € on the AVE.",
            "The fare is 15–17 AUD.",
            "Tickets cost from 6 €.",
            "Tickets cost ab 19,90 €.",
            "Los billetes cuestan desde 6 €.",
            "I biglietti costano da 9,90 €.",
            "Le billet coûte à partir de 25 €.",
            "The fare is aproximately 17 AUD.",
            "Le billet coûte envrion 15 €.",
        )
        turn = turn_with("The train takes 20 minutes.", asked="how do I travel?")
        for claim in claims:
            with self.subTest(claim=claim):
                found = turn.findings(claim)
                self.assertEqual(len(found.values), 1)
                self.assertIn(found.values[0].text, claim)

    def test_a_price_the_page_gives_for_this_trip_stays(self) -> None:
        answer = "The Airport Line fare is 17 AUD, including the 15 AUD access fee."
        turn = turn_with(
            "The Airport Line fare is 17 AUD. The airport access fee is 15 AUD.",
            asked="how do I get to the airport?",
        )
        self.assertEqual(turn.check(answer, "en"), answer)

    def test_a_price_range_the_page_gives_for_this_trip_stays(self) -> None:
        answer = "The fare is 15–17 AUD."
        turn = turn_with("The fare is 15–17 AUD.", asked="how do I travel?")
        self.assertEqual(turn.check(answer, "en"), answer)

    def test_a_price_of_another_currency_is_left_out(self) -> None:
        answer = "The fare is 17 AUD."
        turn = turn_with("The fare is 17 EUR.", asked="how do I travel?")
        found = turn.findings(answer)
        self.assertEqual(len(found.values), 1)
        self.assertIn(found.values[0].text, answer)


class CurrencyTests(unittest.TestCase):
    def test_one_currency_written_two_ways_is_the_same(self) -> None:
        turn = turn_with("Le billet coûte 25 euros.", asked="demain je vais à Lyon")
        self.assertEqual(
            turn.check("Le billet coûte 25 €.", "fr"), "Le billet coûte 25 €."
        )

    def test_the_code_wins_over_the_dollar_sign(self) -> None:
        turn = turn_with("The access fee is 17 AUD.", asked="how do I travel?")
        answer = "The access fee is $17 AUD."
        self.assertEqual(turn.check(answer, "en"), answer)
        self.assertTrue(turn_with("The fee is 17 USD.").findings(answer).values)


class WorldCurrencyTests(unittest.TestCase):
    def test_prices_in_other_currencies_are_checked(self) -> None:
        turn = turn_with("The Shinkansen takes 2 hours.", asked="tokyo to kyoto tmrw")
        for claim in (
            "A ticket costs ¥14,170.",
            "Biljetten kostar 95 kr.",
            "The fare is 3.50 SGD.",
        ):
            with self.subTest(claim=claim):
                self.assertTrue(turn.findings(claim).values)

    def test_ten_feet_is_no_price(self) -> None:
        turn = turn_with("Walk to the platform.", asked="tokyo to kyoto tmrw")
        self.assertFalse(turn.findings("The platform is 10 ft away.").values)


if __name__ == "__main__":
    unittest.main()
