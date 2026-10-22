"""OpenAI-compatible, offline speech synthesis using locally stored Piper voices."""

from __future__ import annotations

import io
import importlib
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
import re
from typing import Protocol, cast
import wave

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from packages.language import detected_language

VOICE_TABLE = Path(__file__).with_name("voices.json")
LEXICON = Path(__file__).with_name("lexicon.json")
MODEL_DIR = Path(
    os.environ.get("ATLAS_TTS_MODEL_DIR", "/opt/atlas-src/models/tts/piper")
)
MAX_INPUT_CHARS = 12_000
MAX_SPOKEN_NUMBER = 999_999_999_999

LOGGER = logging.getLogger(__name__)

app = FastAPI(title="ATLAS TTS", docs_url=None, redoc_url=None)


class _Encoder(Protocol):
    def set_bit_rate(self, value: int) -> None: ...
    def set_in_sample_rate(self, value: int) -> None: ...
    def set_channels(self, value: int) -> None: ...
    def set_quality(self, value: int) -> None: ...
    def encode(self, pcm: bytes) -> bytes: ...
    def flush(self) -> bytes: ...


class _LameModule(Protocol):
    Encoder: type[_Encoder]


class SpeechRequest(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    input: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    voice: str | None = Field(default=None, max_length=128)
    response_format: str = "mp3"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)


def _number(value: int, language: str) -> str:
    """Spell a non-negative integer in one of the offline Piper languages."""
    if language not in {"en", "fr", "de", "es", "it"}:
        raise ValueError("tts_number_language_invalid")
    if not 0 <= value <= MAX_SPOKEN_NUMBER:
        raise ValueError("tts_number_out_of_range")
    converters = {
        "en": _number_en,
        "fr": _number_fr,
        "de": _number_de,
        "es": _number_es,
        "it": _number_it,
    }
    return converters[language](value)


def _join(parts: list[str]) -> str:
    return " ".join(part for part in parts if part)


def _number_en(value: int) -> str:
    ones = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
    tens = "twenty thirty forty fifty sixty seventy eighty ninety".split()
    if value < 20:
        return ones[value]
    if value < 100:
        return _join([tens[value // 10 - 2], ones[value % 10] if value % 10 else ""])
    if value < 1_000:
        return _join(
            [
                ones[value // 100],
                "hundred",
                _number_en(value % 100) if value % 100 else "",
            ]
        )
    return _scale(
        value,
        _number_en,
        (
            (1_000_000_000, "billion", "billion"),
            (1_000_000, "million", "million"),
            (1_000, "thousand", "thousand"),
        ),
    )


def _number_fr(value: int) -> str:
    ones = "zéro un deux trois quatre cinq six sept huit neuf dix onze douze treize quatorze quinze seize dix-sept dix-huit dix-neuf".split()
    tens = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}
    if value < 20:
        return ones[value]
    if value < 70:
        unit = value % 10
        root = tens[value // 10]
        return (
            root
            if not unit
            else (root + " et un" if unit == 1 else root + "-" + ones[unit])
        )
    if value < 80:
        return (
            "soixante et onze" if value == 71 else "soixante-" + _number_fr(value - 60)
        )
    if value < 100:
        return (
            "quatre-vingts" if value == 80 else "quatre-vingt-" + _number_fr(value - 80)
        )
    if value < 1_000:
        hundreds, rest = divmod(value, 100)
        prefix = (
            "cent"
            if hundreds == 1
            else ones[hundreds] + " cent" + ("s" if rest == 0 else "")
        )
        return _join([prefix, _number_fr(rest) if rest else ""])
    return _scale(
        value,
        _number_fr,
        (
            (1_000_000_000, "milliard", "milliards"),
            (1_000_000, "million", "millions"),
            (1_000, "mille", "mille"),
        ),
        omit_one=True,
    )


def _number_de(value: int) -> str:
    ones = "null eins zwei drei vier fünf sechs sieben acht neun zehn elf zwölf dreizehn vierzehn fünfzehn sechzehn siebzehn achtzehn neunzehn".split()
    tens = "zwanzig dreißig vierzig fünfzig sechzig siebzig achtzig neunzig".split()
    if value < 20:
        return ones[value]
    if value < 100:
        unit = value % 10
        return (
            tens[value // 10 - 2]
            if not unit
            else ("ein" if unit == 1 else ones[unit]) + "und" + tens[value // 10 - 2]
        )
    if value < 1_000:
        hundreds, rest = divmod(value, 100)
        return (
            ("ein" if hundreds == 1 else ones[hundreds])
            + "hundert"
            + (_number_de(rest) if rest else "")
        )
    return _scale(
        value,
        _number_de,
        (
            (1_000_000_000, "Milliarde", "Milliarden"),
            (1_000_000, "Million", "Millionen"),
            (1_000, "tausend", "tausend"),
        ),
        feminine_one=True,
        compound_thousand=True,
    )


def _number_es(value: int) -> str:
    ones = "cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince dieciséis diecisiete dieciocho diecinueve".split()
    twenties = "veinte veintiuno veintidós veintitrés veinticuatro veinticinco veintiséis veintisiete veintiocho veintinueve".split()
    tens = "treinta cuarenta cincuenta sesenta setenta ochenta noventa".split()
    hundreds = {
        1: "ciento",
        2: "doscientos",
        3: "trescientos",
        4: "cuatrocientos",
        5: "quinientos",
        6: "seiscientos",
        7: "setecientos",
        8: "ochocientos",
        9: "novecientos",
    }
    if value < 20:
        return ones[value]
    if value < 30:
        return twenties[value - 20]
    if value < 100:
        return _join(
            [
                tens[value // 10 - 3],
                "y" if value % 10 else "",
                ones[value % 10] if value % 10 else "",
            ]
        )
    if value == 100:
        return "cien"
    if value < 1_000:
        return _join(
            [hundreds[value // 100], _number_es(value % 100) if value % 100 else ""]
        )
    if value >= 1_000_000_000:
        group, rest = divmod(value, 1_000_000_000)
        return _join(
            [
                "mil millones" if group == 1 else _number_es(group) + " mil millones",
                _number_es(rest) if rest else "",
            ]
        )
    return _scale(
        value,
        _number_es,
        (
            (1_000_000_000, "mil millones", "mil millones"),
            (1_000_000, "millón", "millones"),
            (1_000, "mil", "mil"),
        ),
        omit_one=True,
        spanish_one=True,
    )


def _number_it(value: int) -> str:
    ones = "zero uno due tre quattro cinque sei sette otto nove dieci undici dodici tredici quattordici quindici sedici diciassette diciotto diciannove".split()
    tens = "venti trenta quaranta cinquanta sessanta settanta ottanta novanta".split()
    if value < 20:
        return ones[value]
    if value < 100:
        base, unit = tens[value // 10 - 2], value % 10
        if unit in {1, 8}:
            base = base[:-1]
        return base + (ones[unit] if unit else "")
    if value < 1_000:
        hundreds, rest = divmod(value, 100)
        stem = "cento" if hundreds == 1 else ones[hundreds] + "cento"
        if 80 <= rest < 90:
            stem = stem[:-1]
        return stem + (_number_it(rest) if rest else "")
    for divisor, singular, plural in (
        (1_000_000_000, "miliardo", "miliardi"),
        (1_000_000, "milione", "milioni"),
        (1_000, "mille", "mila"),
    ):
        if value >= divisor:
            group, rest = divmod(value, divisor)
            if divisor == 1_000:
                head = "mille" if group == 1 else _number_it(group) + "mila"
            else:
                head = (
                    ("un" if group == 1 else _number_it(group))
                    + " "
                    + (singular if group == 1 else plural)
                )
            return _join([head, _number_it(rest) if rest else ""])
    raise ValueError("tts_number_scale_invalid")


def _scale(
    value: int,
    convert: Callable[[int], str],
    scales: tuple[tuple[int, str, str], ...],
    *,
    omit_one: bool = False,
    feminine_one: bool = False,
    spanish_one: bool = False,
    italian_one: bool = False,
    compound_thousand: bool = False,
) -> str:
    """Compose groups of thousands; language-specific unit forms are explicit flags."""
    for divisor, singular, plural in scales:
        if value >= divisor:
            group, rest = divmod(value, divisor)
            if omit_one and group == 1 and divisor == 1_000:
                lead = ""
            else:
                lead = convert(group)
                if compound_thousand and group == 1 and divisor == 1_000:
                    lead = "ein"
                if feminine_one and group == 1 and divisor > 1_000:
                    lead = "eine"
                if spanish_one:
                    lead = lead.removesuffix("veintiuno").replace("uno", "un") + (
                        "veintiún" if lead.endswith("veintiuno") else ""
                    )
                if italian_one and group == 1 and divisor > 1_000:
                    lead = "un"
            unit = singular if group == 1 else plural
            head = (
                (lead + unit)
                if compound_thousand and divisor == 1_000
                else _join([lead, unit])
            )
            return _join([head, convert(rest) if rest else ""])
    raise ValueError("tts_number_scale_invalid")


def _lexicon(language: str) -> dict[str, str]:
    try:
        data = json.loads(LEXICON.read_text())
        entries = data[language]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("tts_lexicon_invalid") from exc
    if not isinstance(entries, dict) or not all(
        isinstance(word, str) and isinstance(value, str)
        for word, value in entries.items()
    ):
        raise RuntimeError("tts_lexicon_invalid")
    return entries


def _replace_lexicon(text: str, language: str) -> str:
    for word, spoken in sorted(
        _lexicon(language).items(), key=lambda item: len(item[0]), reverse=True
    ):
        text = re.sub(
            r"(?<!\w)" + re.escape(word) + r"(?!\w)", spoken, text, flags=re.I
        )
    return text


def _normalize_numbers(text: str, language: str) -> str:
    decimal = {
        "en": "point",
        "fr": "virgule",
        "de": "komma",
        "es": "coma",
        "it": "virgola",
    }[language]
    percent = {
        "en": "percent",
        "fr": "pour cent",
        "de": "prozent",
        "es": "por ciento",
        "it": "per cento",
    }[language]
    currency = {
        "en": "euros",
        "fr": "euros",
        "de": "Euro",
        "es": "euros",
        "it": "euro",
    }[language]
    ordinal = {
        "en": "first",
        "fr": "premier",
        "de": "erste",
        "es": "primero",
        "it": "primo",
    }[language]

    def say(raw: str) -> str:
        try:
            return _number(int(raw), language)
        except Exception:
            # Do not expose user input in logs; Piper can still pronounce digits.
            LOGGER.warning("tts_number_conversion_failed")
            return raw

    def say_year(raw: str) -> str:
        value = int(raw)
        if not 1_000 <= value <= 9_999:
            return say(raw)
        thousand, rest = divmod(value, 1_000)
        names = {
            "en": "thousand",
            "fr": "mille",
            "de": "tausend",
            "es": "mil",
            "it": "mille",
        }
        if not rest:
            return _join([say(str(thousand)), names[language]])
        old_tens = {
            "fr": ("vingt", "trente", "quarante", "cinquante", "soixante"),
            "en": (
                "twenty",
                "thirty",
                "forty",
                "fifty",
                "sixty",
                "seventy",
                "eighty",
                "ninety",
            ),
            "de": (
                "zwanzig",
                "dreißig",
                "vierzig",
                "fünfzig",
                "sechzig",
                "siebzig",
                "achtzig",
                "neunzig",
            ),
            "es": (
                "veinte",
                "treinta",
                "cuarenta",
                "cincuenta",
                "sesenta",
                "setenta",
                "ochenta",
                "noventa",
            ),
            "it": (
                "venti",
                "trenta",
                "quaranta",
                "cinquanta",
                "sessanta",
                "settanta",
                "ottanta",
                "novanta",
            ),
        }
        tail = (
            say(str(rest))
            if rest < 20
            else _join(
                [
                    old_tens[language][rest // 10 - 2],
                    say(str(rest % 10)) if rest % 10 else "",
                ]
            )
        )
        return _join([say(str(thousand)), names[language], tail])

    text = re.sub(r"\b1(?:er|st|e|º|°)(?!\w)", ordinal, text, flags=re.I)
    text = re.sub(
        r"\b(\d+)(?:e|nd|rd|th|º|°)(?!\w)",
        lambda match: say(match[1]),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"(?<!\w)(\d{1,2})[h:](\d{2})\s*(am|pm)?\b",
        lambda match: f"{say(match[1])} { ({'en': 'o clock', 'fr': 'heures', 'de': 'Uhr', 'es': 'horas', 'it': 'ore'}[language]) } {say(match[2])}"
        + (f" {match[3]}" if match[3] else ""),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(version|Version)\s+(\d+)\.(\d+)(?!\w)",
        lambda match: f"{match[1]} {say(match[2])} {('point' if language == 'fr' else decimal)} {say(match[3])}",
        text,
    )
    group_separator = r"," if language == "en" else r"\."
    grouped = re.compile(
        rf"(?<![\w.,])([0-9]{{1,3}}(?:[ {group_separator}\u00a0\u202f][0-9]{{3}})+)(?![\w.,])"
    )
    text = grouped.sub(lambda match: say(re.sub(r"\D", "", match[1])), text)
    text = re.sub(
        r"(?<![\w.,])(\d+)[,.](\d+)(?!\w|[.,]\d)",
        lambda match: f"{say(match[1])} {decimal} {say(match[2])}",
        text,
    )
    text = re.sub(
        r"(?<!\w)(\d+)\s*(?:%|percent|pour cent|prozent|por ciento|per cento)(?!\w)",
        lambda match: f"{say(match[1])} {percent}",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"(?:€|EUR\b|\beuros?\b|\$)\s*(\d+)|(?<!\w)(\d+)\s*(?:€|EUR\b|\beuros?\b|\$)",
        lambda match: f"{say(match[1] or match[2])} {currency}",
        text,
        flags=re.I,
    )
    return re.sub(r"\b\d+\b", lambda match: say_year(match[0]), text)


def spoken_text(value: str, language: str | None = None) -> str:
    """Turn chat presentation into deterministic, language-aware speech input."""
    language = language or detected_language(value) or "en"
    marker = {
        "en": " code ",
        "fr": " code ",
        "de": " Code ",
        "es": " código ",
        "it": " codice ",
    }[language]
    text = re.sub(r"```.*?```", marker, value, flags=re.S)
    text = re.sub(r"!?(\[([^\]]*)\]\([^)]*\))", r"\2", text)
    text = re.sub(r"https?://[^\s|]+", " ", text)
    text = re.sub(
        r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", "", text, flags=re.M
    )
    text = re.sub(r"(?m)^[ \t]*\|[ \t]*", "", text)
    text = re.sub(r"\|", ", ", text)
    text = re.sub(r"[`*_>#~]", "", text)
    text = re.sub(r"[\U00010000-\U0010ffff]", " ", text)
    text = _replace_lexicon(text, language)
    text = re.sub(r"(?<!\d)\s*,\s*|\s*,\s*(?!\d)", ", ", text)
    return re.sub(r"\s+", " ", _normalize_numbers(text, language)).strip(" ,")


def voice_for(text: str) -> tuple[str, str]:
    """Return a configured engine/voice pair; short or unknown prose falls back to English."""
    language = detected_language(text) or "en"
    try:
        table = json.loads(VOICE_TABLE.read_text())
        row = table[language]
        engine, voice = row["engine"], row["voice"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("tts_voice_table_invalid") from exc
    if not isinstance(engine, str) or not isinstance(voice, str):
        raise RuntimeError("tts_voice_table_invalid")
    return engine, voice


def _mp3(wav_bytes: bytes) -> bytes:
    """Encode PCM WAV without invoking a networked or external transcoder."""
    lameenc = cast(_LameModule, importlib.import_module("lameenc"))

    with wave.open(io.BytesIO(wav_bytes), "rb") as source:
        if source.getsampwidth() != 2:
            raise RuntimeError("tts_unsupported_sample_width")
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(128)
        encoder.set_in_sample_rate(source.getframerate())
        encoder.set_channels(source.getnchannels())
        encoder.set_quality(2)
        # The encoder returns bytearrays; the HTTP layer needs bytes.
        return bytes(
            encoder.encode(source.readframes(source.getnframes())) + encoder.flush()
        )


def synthesize(engine: str, voice: str, text: str, response_format: str) -> bytes:
    """Synthesize from a local model only. Network model fetches are intentionally absent."""
    if engine != "piper":
        raise RuntimeError("tts_engine_unsupported")
    model = MODEL_DIR / f"{voice}.onnx"
    if not model.is_file():
        raise FileNotFoundError("tts_voice_unavailable")
    from piper import PiperVoice

    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        PiperVoice.load(model).synthesize_wav(text, wav)
    wav_bytes = output.getvalue()
    return wav_bytes if response_format == "wav" else _mp3(wav_bytes)


@app.get("/v1/audio/models")
def models() -> dict[str, object]:
    return {"models": [{"id": "atlas-tts"}]}


@app.post("/v1/audio/speech")
def speech(payload: SpeechRequest) -> Response:
    if payload.response_format not in {"mp3", "wav"}:
        raise HTTPException(status_code=400, detail="unsupported_response_format")
    language = detected_language(payload.input) or "en"
    text = spoken_text(payload.input, language)
    if not text:
        raise HTTPException(status_code=400, detail="empty_spoken_text")
    try:
        engine, voice = voice_for(payload.input)
        audio = synthesize(engine, voice, text, payload.response_format)
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="tts_voice_unavailable") from None
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return Response(
        audio,
        media_type="audio/wav" if payload.response_format == "wav" else "audio/mpeg",
        headers={"X-Atlas-TTS-Voice": voice},
    )
