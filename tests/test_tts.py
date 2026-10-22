import json
import unittest
from typing import cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from packages.ports import atlas_ports
from services.orchestrator import serving
from services.tts.service import (
    LEXICON,
    VOICE_TABLE,
    _number,
    app,
    spoken_text,
    voice_for,
)


EXPECTED_NUMBERS = {
    "fr": "0=zéro|1=un|2=deux|8=huit|10=dix|11=onze|16=seize|17=dix-sept|19=dix-neuf|20=vingt|21=vingt et un|22=vingt-deux|29=vingt-neuf|30=trente|31=trente et un|40=quarante|58=cinquante-huit|69=soixante-neuf|70=soixante-dix|71=soixante et onze|72=soixante-douze|79=soixante-dix-neuf|80=quatre-vingts|81=quatre-vingt-un|89=quatre-vingt-neuf|90=quatre-vingt-dix|91=quatre-vingt-onze|99=quatre-vingt-dix-neuf|100=cent|101=cent un|110=cent dix|121=cent vingt et un|200=deux cents|201=deux cent un|999=neuf cent quatre-vingt-dix-neuf|1000=mille|1001=mille un|1010=mille dix|1100=mille cent|2026=deux mille vingt-six|1250000=un million deux cent cinquante mille|999999999999=neuf cent quatre-vingt-dix-neuf milliards neuf cent quatre-vingt-dix-neuf millions neuf cent quatre-vingt-dix-neuf mille neuf cent quatre-vingt-dix-neuf",
    "en": "0=zero|1=one|2=two|8=eight|10=ten|11=eleven|16=sixteen|17=seventeen|19=nineteen|20=twenty|21=twenty one|22=twenty two|29=twenty nine|30=thirty|31=thirty one|40=forty|58=fifty eight|69=sixty nine|70=seventy|71=seventy one|72=seventy two|79=seventy nine|80=eighty|81=eighty one|89=eighty nine|90=ninety|91=ninety one|99=ninety nine|100=one hundred|101=one hundred one|110=one hundred ten|121=one hundred twenty one|200=two hundred|201=two hundred one|999=nine hundred ninety nine|1000=one thousand|1001=one thousand one|1010=one thousand ten|1100=one thousand one hundred|2026=two thousand twenty six|1250000=one million two hundred fifty thousand|999999999999=nine hundred ninety nine billion nine hundred ninety nine million nine hundred ninety nine thousand nine hundred ninety nine",
    "de": "0=null|1=eins|2=zwei|8=acht|10=zehn|11=elf|16=sechzehn|17=siebzehn|19=neunzehn|20=zwanzig|21=einundzwanzig|22=zweiundzwanzig|29=neunundzwanzig|30=dreißig|31=einunddreißig|40=vierzig|58=achtundfünfzig|69=neunundsechzig|70=siebzig|71=einundsiebzig|72=zweiundsiebzig|79=neunundsiebzig|80=achtzig|81=einundachtzig|89=neunundachtzig|90=neunzig|91=einundneunzig|99=neunundneunzig|100=einhundert|101=einhunderteins|110=einhundertzehn|121=einhunderteinundzwanzig|200=zweihundert|201=zweihunderteins|999=neunhundertneunundneunzig|1000=eintausend|1001=eintausend eins|1010=eintausend zehn|1100=eintausend einhundert|2026=zweitausend sechsundzwanzig|1250000=eine Million zweihundertfünfzigtausend|999999999999=neunhundertneunundneunzig Milliarden neunhundertneunundneunzig Millionen neunhundertneunundneunzigtausend neunhundertneunundneunzig",
    "es": "0=cero|1=uno|2=dos|8=ocho|10=diez|11=once|16=dieciséis|17=diecisiete|19=diecinueve|20=veinte|21=veintiuno|22=veintidós|29=veintinueve|30=treinta|31=treinta y uno|40=cuarenta|58=cincuenta y ocho|69=sesenta y nueve|70=setenta|71=setenta y uno|72=setenta y dos|79=setenta y nueve|80=ochenta|81=ochenta y uno|89=ochenta y nueve|90=noventa|91=noventa y uno|99=noventa y nueve|100=cien|101=ciento uno|110=ciento diez|121=ciento veintiuno|200=doscientos|201=doscientos uno|999=novecientos noventa y nueve|1000=mil|1001=mil uno|1010=mil diez|1100=mil cien|2026=dos mil veintiséis|1250000=un millón doscientos cincuenta mil|999999999999=novecientos noventa y nueve mil millones novecientos noventa y nueve millones novecientos noventa y nueve mil novecientos noventa y nueve",
    "it": "0=zero|1=uno|2=due|8=otto|10=dieci|11=undici|16=sedici|17=diciassette|19=diciannove|20=venti|21=ventuno|22=ventidue|29=ventinove|30=trenta|31=trentuno|40=quaranta|58=cinquantotto|69=sessantanove|70=settanta|71=settantuno|72=settantadue|79=settantanove|80=ottanta|81=ottantuno|89=ottantanove|90=novanta|91=novantuno|99=novantanove|100=cento|101=centouno|110=centodieci|121=centoventuno|200=duecento|201=duecentouno|999=novecentonovantanove|1000=mille|1001=mille uno|1010=mille dieci|1100=mille cento|2026=duemila ventisei|1250000=un milione duecentocinquantamila|999999999999=novecentonovantanove miliardi novecentonovantanove milioni novecentonovantanovemila novecentonovantanove",
}


class TTSRoutingTests(unittest.TestCase):
    def test_numbers_are_hand_checked_in_every_supported_language(self) -> None:
        for language, cases in EXPECTED_NUMBERS.items():
            for item in cases.split("|"):
                value, expected = item.split("=", 1)
                with self.subTest(language=language, value=value):
                    self.assertEqual(_number(int(value), language), expected)

    def test_every_small_number_and_large_samples_convert_without_zero_tails(
        self,
    ) -> None:
        zero_words = {
            "fr": "zéro",
            "en": "zero",
            "de": "null",
            "es": "cero",
            "it": "zero",
        }
        samples = (10_000, 1_000_000, 2_026_000_000, 999_999_999_999)
        for language, zero_word in zero_words.items():
            for value in (*range(10_000), *samples):
                with self.subTest(language=language, value=value):
                    words = _number(value, language)
                    self.assertTrue(words)
                    if value:
                        self.assertNotIn(zero_word, words.split())

    def test_grouped_numbers_and_decimals_are_one_number(self) -> None:
        for language in EXPECTED_NUMBERS:
            with self.subTest(language=language):
                spoken = spoken_text("1 250 000 and 3,5", language)
                self.assertIn(_number(1_250_000, language), spoken)
                self.assertIn(
                    {
                        "en": "point",
                        "fr": "virgule",
                        "de": "komma",
                        "es": "coma",
                        "it": "virgola",
                    }[language],
                    spoken,
                )

    def test_routes_each_supported_language_to_data_table_voice(self) -> None:
        examples = {
            "fr": "Bonjour, voici une phrase française suffisamment longue.",
            "en": "This is a sufficiently long English sentence for routing.",
            "de": "Dies ist ein ausreichend langer deutscher Beispielsatz.",
            "es": "Esta es una frase española suficientemente larga para enrutar.",
            "it": "Questa è una frase italiana abbastanza lunga per il percorso.",
        }
        for language, text in examples.items():
            with self.subTest(language=language):
                engine, voice = voice_for(text)
                self.assertEqual(engine, "piper")
                self.assertTrue(voice.startswith(language + "_"))

    def test_markdown_and_urls_are_not_spoken(self) -> None:
        self.assertEqual(
            spoken_text(
                "Read [Murena](https://murena.com) and `Atlas`; https://example.test"
            ),
            "Read Murena and Atlas;",
        )

    def test_lexicon_is_whole_word_case_insensitive_and_complete(self) -> None:
        self.assertEqual(spoken_text("atlas Atlasian", "fr"), "Atlass Atlasian")
        voices = json.loads(VOICE_TABLE.read_text())
        lexicon = json.loads(LEXICON.read_text())
        self.assertEqual(set(voices), set(lexicon))
        for language, entries in lexicon.items():
            with self.subTest(language=language):
                self.assertIn("Atlas", entries)
                self.assertIn("Murena", entries)
                self.assertIn("/e/OS", entries)

    def test_normalizes_read_aloud_forms_in_every_supported_language(self) -> None:
        expected = {
            "fr": (
                "14 octobre 2026 à 14h30, version 3.2, 50%, 20 EUR, 1er",
                "quatorze octobre deux mille vingt six à quatorze heures trente, version trois point deux, cinquante pour cent, vingt euros, premier",
            ),
            "en": (
                "October 14 2026 at 2:30 pm, version 3.2, 50%, $20, 1st",
                "October fourteen two thousand twenty six at two o clock thirty pm, version three point two, fifty percent, twenty euros, first",
            ),
            "de": (
                "14 Oktober 2026 um 14h30, Version 3.2, 50%, 20 EUR, 1.",
                "vierzehn Oktober zwei tausend zwanzig sechs um vierzehn Uhr dreißig, Version drei komma zwei, fünfzig prozent, zwanzig Euro, eins.",
            ),
            "es": (
                "14 octubre 2026 a las 14h30, versión 3.2, 50%, 20 EUR, 1º",
                "catorce octubre dos mil veinte seis a las catorce horas treinta, versión tres coma dos, cincuenta por ciento, veinte euros, primero",
            ),
            "it": (
                "14 ottobre 2026 alle 14h30, versione 3.2, 50%, 20 EUR, 1°",
                "quattordici ottobre due mille venti sei alle quattordici ore trenta, versione tre virgola due, cinquanta per cento, venti euro, primo",
            ),
        }
        for language, (source, normalized) in expected.items():
            with self.subTest(language=language):
                self.assertEqual(spoken_text(source, language), normalized)

    def test_strips_code_tables_urls_and_emoji(self) -> None:
        self.assertEqual(
            spoken_text(
                "```secret()```\n| Name | URL |\n| --- | --- |\n| Atlas | https://atlas.example/path | 😀",
                "en",
            ),
            "code Name, U R L, Atlas",
        )

    def test_port_registry_has_an_offsettable_tts_port(self) -> None:
        with patch.dict("os.environ", {"ATLAS_PORT_BASE": "1100"}, clear=True):
            self.assertEqual(atlas_ports().port("tts"), 9121)


class TTSRequestTests(unittest.TestCase):
    def test_valid_request_returns_selected_audio_type(self) -> None:
        with (
            TestClient(app) as client,
            patch("services.tts.service.synthesize", return_value=b"audio") as synth,
        ):
            response = client.post(
                "/v1/audio/speech",
                json={"model": "atlas-tts", "input": "Bonjour, Murena et Atlas."},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "audio/mpeg")
        self.assertEqual(response.headers["content-length"], "5")
        self.assertEqual(synth.call_args.args[2], "Bonjour, Muréna et Atlass.")

    def test_number_conversion_failure_keeps_digits_and_does_not_make_a_500(
        self,
    ) -> None:
        with (
            TestClient(app) as client,
            patch("services.tts.service._number", side_effect=RuntimeError("broken")),
            patch("services.tts.service.synthesize", return_value=b"audio") as synth,
            self.assertLogs("services.tts.service", "WARNING") as logs,
        ):
            response = client.post(
                "/v1/audio/speech",
                json={"model": "atlas-tts", "input": "The result is 70."},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(synth.call_args.args[2], "The result is 70.")
        self.assertEqual(
            logs.output, ["WARNING:services.tts.service:tts_number_conversion_failed"]
        )

    def test_speech_endpoint_handles_numbers_in_every_language(self) -> None:
        inputs = {
            "fr": "Bonjour, 71, année 2026, total 1 250 000.",
            "en": "Hello, 79, year 2026, total 1,250,000.",
            "de": "Hallo, 81, Jahr 2026, Summe 1.250.000.",
            "es": "Hola, 91, año 2026, total 1.250.000.",
            "it": "Ciao, 99, anno 2026, totale 1.250.000.",
        }
        with (
            TestClient(app) as client,
            patch("services.tts.service.synthesize", return_value=b"audio"),
        ):
            for language, value in inputs.items():
                with self.subTest(language=language):
                    response = client.post(
                        "/v1/audio/speech",
                        json={"model": "atlas-tts", "input": value},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, b"audio")

    def test_rejects_invalid_format_and_empty_spoken_text(self) -> None:
        with TestClient(app) as client:
            unsupported = client.post(
                "/v1/audio/speech",
                json={"model": "atlas-tts", "input": "Hello", "response_format": "ogg"},
            )
            empty = client.post(
                "/v1/audio/speech",
                json={"model": "atlas-tts", "input": "https://example.test"},
            )
        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(empty.status_code, 400)


class TTSWebUIConfigurationTests(unittest.TestCase):
    def _webui_options(self, tts: str | None) -> list[str]:
        environment = {
            "ATLAS_PORT_BASE": "1100",
            "OPEN_TERMINAL_API_KEY": "test-terminal-key",
        }
        if tts is not None:
            environment["ATLAS_TTS"] = tts
        with (
            patch.dict("os.environ", environment, clear=True),
            patch(
                "services.orchestrator.terminal_stack.network_gateway",
                return_value="172.18.0.1",
            ),
            patch("services.orchestrator.serving.docker_run") as docker_run,
        ):
            serving.webui("atlas-chat-ui", True)
        return cast(list[str], docker_run.call_args.args[2])

    def test_webui_gets_tts_variables_only_when_opted_in(self) -> None:
        self.assertNotIn("AUDIO_TTS_ENGINE=openai", self._webui_options(None))
        options = self._webui_options("1")
        self.assertIn("AUDIO_TTS_ENGINE=openai", options)
        self.assertIn("AUDIO_TTS_OPENAI_API_KEY=atlas-local", options)
        self.assertTrue(any(value.endswith(":9121/v1") for value in options))


class Mp3EncodingTests(unittest.TestCase):
    def test_mp3_is_bytes_so_the_http_response_can_carry_it(self) -> None:
        import io
        import wave

        from services.tts.service import _mp3

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(22050)
            wav.writeframes(b"\x00\x00" * 2205)
        audio = _mp3(buffer.getvalue())
        self.assertIs(type(audio), bytes)
        self.assertGreater(len(audio), 100)
