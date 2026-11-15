"""French communes: mayor, population and codes from official open data, held locally.

Two whole datasets are downloaded and kept for a day, then searched here, so a question
never leaves Atlas (the downloads carry nothing from the conversation):

- the Répertoire national des élus, mayors file (Ministère de l'Intérieur, on
  data.gouv.fr, Licence Ouverte 2.0), located through the data.gouv.fr dataset API so
  that a new monthly file is picked up without a code change;
- the list of communes of the Découpage administratif API (geo.api.gouv.fr, INSEE
  codes and municipal population), and its list of departments.

Only the mayor's name, gender and mandate dates are returned: the file's birth dates and
occupations are not.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from packages.privacy import require_shared
from services.orchestrator import reference_fetch
from services.orchestrator.reference_fetch import SourceError

CAPABILITY = "public_datasets"
RNE_DATASET = "https://www.data.gouv.fr/api/1/datasets/repertoire-national-des-elus-1/"
RNE_PAGE = "https://www.data.gouv.fr/fr/datasets/repertoire-national-des-elus-1/"
COMMUNES_API = "https://geo.api.gouv.fr/communes"
DEPARTMENTS_API = "https://geo.api.gouv.fr/departements"
DAY = 86_400
MAX_CSV_BYTES = 16_000_000
MAX_LIST_BYTES = 12_000_000


@dataclass(frozen=True)
class Mayor:
    first_name: str
    last_name: str
    gender: str
    mandate_start: str
    function_start: str


@dataclass
class Tables:
    loaded: float
    mayors: dict[str, Mayor]
    mayors_file: str
    mayors_updated: str
    communes: list[dict[str, Any]]
    departments: dict[str, str]
    by_name: dict[str, list[int]]


def key(name: str) -> str:
    """A commune name folded for matching: case, accents, hyphens, Saint/St, articles."""
    folded = unicodedata.normalize("NFKD", name.lower().replace("’", "'"))
    text = "".join(c for c in folded if not unicodedata.combining(c))
    text = re.sub(r"[-'\s]+", " ", text).strip()
    text = re.sub(r"\bst(e?)\b", r"saint\1", text)
    return text


def parse_mayors(body: bytes) -> dict[str, Mayor]:
    text = body.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text), delimiter=";")
    header = next(reader, [])
    try:
        code = header.index("Code de la commune")
        last = header.index("Nom de l'élu")
        first = header.index("Prénom de l'élu")
        gender = header.index("Code sexe")
        mandate = header.index("Date de début du mandat")
        function = header.index("Date de début de la fonction")
    except ValueError:
        raise SourceError(
            "dataset_invalid", "the mayors file has unexpected columns"
        ) from None
    mayors: dict[str, Mayor] = {}
    for row in reader:
        if len(row) <= max(code, last, first, gender, mandate, function):
            continue
        commune = row[code].strip()
        if re.fullmatch(r"[0-9AB]{5}", commune):
            mayors[commune] = Mayor(
                row[first].strip(),
                row[last].strip(),
                row[gender].strip(),
                row[mandate].strip(),
                row[function].strip(),
            )
    if len(mayors) < 1000:
        raise SourceError("dataset_invalid", "the mayors file holds too few rows")
    return mayors


def _json(body: bytes, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        raise SourceError("dataset_invalid", f"{what} is not readable JSON") from None


async def _mayors_file(timeout: float) -> tuple[str, str]:
    body = await reference_fetch.get(
        RNE_DATASET,
        capability=CAPABILITY,
        timeout=timeout,
        ttl=DAY,
        max_bytes=2_000_000,
    )
    data = _json(body, "the data.gouv.fr dataset description")
    for resource in data.get("resources", []) if isinstance(data, dict) else []:
        url = str(resource.get("url", ""))
        if re.search(r"elus-maire", url) and url.startswith(
            "https://static.data.gouv.fr/"
        ):
            return url, str(resource.get("last_modified", ""))[:10]
    raise SourceError(
        "dataset_unavailable", "the mayors file is not listed on data.gouv.fr"
    )


class Directory:
    def __init__(self) -> None:
        self.tables: Tables | None = None
        self.lock = asyncio.Lock()

    async def load(self, timeout: float) -> Tables:
        require_shared(CAPABILITY)
        async with self.lock:
            if self.tables is not None and time.monotonic() - self.tables.loaded < DAY:
                return self.tables
            mayors_url, updated = await _mayors_file(timeout)
            csv_body, communes_body, departments_body = await asyncio.gather(
                reference_fetch.get(
                    mayors_url,
                    capability=CAPABILITY,
                    timeout=timeout,
                    ttl=DAY,
                    max_bytes=MAX_CSV_BYTES,
                ),
                reference_fetch.get(
                    COMMUNES_API,
                    capability=CAPABILITY,
                    timeout=timeout,
                    ttl=DAY,
                    max_bytes=MAX_LIST_BYTES,
                    params={
                        "fields": "nom,code,population,codesPostaux,codeDepartement",
                        "format": "json",
                    },
                ),
                reference_fetch.get(
                    DEPARTMENTS_API,
                    capability=CAPABILITY,
                    timeout=timeout,
                    ttl=DAY,
                    max_bytes=200_000,
                ),
            )
            mayors = await asyncio.to_thread(parse_mayors, csv_body)
            communes = _json(communes_body, "the communes list")
            departments = _json(departments_body, "the departments list")
            if not isinstance(communes, list) or len(communes) < 1000:
                raise SourceError("dataset_invalid", "the communes list is too short")
            by_name: dict[str, list[int]] = {}
            for index, row in enumerate(communes):
                if isinstance(row, dict) and isinstance(row.get("nom"), str):
                    by_name.setdefault(key(row["nom"]), []).append(index)
            self.tables = Tables(
                time.monotonic(),
                mayors,
                mayors_url,
                updated,
                communes,
                {
                    str(d.get("code")): str(d.get("nom"))
                    for d in departments
                    if isinstance(d, dict)
                }
                if isinstance(departments, list)
                else {},
                by_name,
            )
            return self.tables


DIRECTORY = Directory()


def _department_code(value: str | None, tables: Tables) -> str | None:
    if not value:
        return None
    text = value.strip().upper()
    if text in tables.departments:
        return text
    folded = key(value)
    for code, name in tables.departments.items():
        if key(name) == folded:
            return code
    return None


def find(
    tables: Tables, name: str, department: str | None, postal_code: str | None
) -> list[dict[str, Any]]:
    wanted = key(name)
    rows = [tables.communes[i] for i in tables.by_name.get(wanted, [])]
    if not rows:
        # "Saint Malo" written without hyphen or with a typo in the article: a prefix
        # match on the folded name, still exact on every word typed.
        rows = [
            tables.communes[i]
            for folded, indexes in tables.by_name.items()
            if folded.startswith(wanted + " ")
            or folded.replace(" ", "") == wanted.replace(" ", "")
            for i in indexes
        ][:20]
    code = _department_code(department, tables)
    if code:
        rows = [r for r in rows if r.get("codeDepartement") == code] or rows
    if postal_code and re.fullmatch(r"\d{5}", postal_code.strip()):
        rows = [
            r for r in rows if postal_code.strip() in (r.get("codesPostaux") or [])
        ] or rows
    return sorted(rows, key=lambda r: int(r.get("population") or 0), reverse=True)


def describe(row: dict[str, Any], tables: Tables) -> dict[str, object]:
    code = str(row.get("code"))
    mayor = tables.mayors.get(code)
    department = str(row.get("codeDepartement", ""))
    result: dict[str, object] = {
        "commune": row.get("nom"),
        "insee_code": code,
        "department": f"{tables.departments.get(department, department)} ({department})",
        "postal_codes": (row.get("codesPostaux") or [])[:12],
        "population": row.get("population"),
        "population_source": "INSEE municipal population (latest legal population "
        "published through geo.api.gouv.fr)",
        "url": f"https://geo.api.gouv.fr/communes/{code}?fields=nom,code,population,codesPostaux",
    }
    if mayor is not None:
        result["mayor"] = {
            "name": f"{mayor.first_name} {mayor.last_name.title()}",
            "gender": {"M": "male", "F": "female"}.get(mayor.gender, mayor.gender),
            "mandate_start": mayor.mandate_start,
            "function_start": mayor.function_start,
            "source": "Répertoire national des élus (Ministère de l'Intérieur)",
            "file_updated": tables.mayors_updated,
            "url": RNE_PAGE,
        }
    else:
        result["mayor"] = None
    return result


async def commune(
    name: str, department: str | None, postal_code: str | None, timeout: float
) -> dict[str, object]:
    name = " ".join(name.split())[:120]
    if not name:
        raise SourceError("invalid_query", "name the commune")
    tables = await DIRECTORY.load(timeout)
    rows = find(tables, name, department, postal_code)
    output: dict[str, object] = {
        "source": "Répertoire national des élus (data.gouv.fr) and geo.api.gouv.fr",
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset_url": RNE_PAGE,
        "mayors_file": tables.mayors_file,
    }
    if not rows:
        output["matches"] = []
        output["note"] = (
            "No French commune has this name. Check the spelling; for a city outside "
            "France use the encyclopedia tool."
        )
        return output
    output["matches"] = [describe(row, tables) for row in rows[:5]]
    output["note"] = (
        "Several communes have this name: ask which one is meant, or name the largest "
        "and say others exist."
        if len(rows) > 1
        else "Give the mayor with the start date of the function and cite the Répertoire "
        "national des élus; give the population as INSEE municipal population."
    )
    return output
