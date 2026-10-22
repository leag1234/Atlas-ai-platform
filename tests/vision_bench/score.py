"""Score identified string/fret pairs; unknown positions earn no credit."""

import json
import re
from pathlib import Path
import sys
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field

Fret = Annotated[int, Field(strict=True, ge=0, le=36)]


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    positions: list[Fret | None] = Field(min_length=6, max_length=6)
    full_barre_fret: Fret | None
    chord_family: str = Field(min_length=1, max_length=100)


def score(text: str, annotation: dict[str, object]) -> dict[str, object]:
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text.strip(), re.S)
    reading = Reading.model_validate_json(fenced[1] if fenced else text)
    expected = annotation["positions"]
    families = annotation["compatible_families"]
    if (
        not isinstance(expected, list)
        or len(expected) != 6
        or not isinstance(families, list)
    ):
        raise ValueError("invalid_annotation")
    return {
        "positions_correct": sum(
            a == b for a, b in zip(reading.positions, expected, strict=True)
        ),
        "positions_total": len(expected),
        "structure_recognised": reading.full_barre_fret
        == annotation["full_barre_fret"],
        "chord_family_compatible": reading.chord_family.casefold()
        in [str(f).casefold() for f in families],
    }


if __name__ == "__main__":
    print(
        json.dumps(
            score(
                Path(sys.argv[1]).read_text(), json.loads(Path(sys.argv[2]).read_text())
            )
        )
    )
