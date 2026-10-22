"""POC minimal input filter; content is never copied to observability logs."""

import logging
import unicodedata


def validate_input(text: str) -> str:
    if any(
        (unicodedata.category(char) in {"Cc", "Cs"} and char not in "\n\r\t")
        or char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        for char in text
    ):
        logging.getLogger(__name__).warning(
            '{"event":"input_rejected","reason":"control_character"}'
        )
        raise ValueError("unsafe_control_character")
    return text
