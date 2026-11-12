"""A workbook asked to calculate holds the formulas that do it.

A person asked for "pace calculated automatically" and got a Pace column with
its header and no formula. Another asked for totals and got a Difference
column left empty beside correct totals, whose own total added up empty cells.
Both answers said the file calculated them (staging 2026-11-12).

The check reads the content to publish and the words of the request, never
what is said about the file. It finds:

formulas_requested_but_missing: the request asks for calculated values and no
table of the workbook holds a formula;

calculated_column_empty: a column whose header names a derived quantity
holds nothing but its own total. A header that relates other columns
(Difference, Average, Pace, Remaining) is found in a table that has formulas
elsewhere, or for a request that asks for calculation. A header that names an
amount (Total, Sum, Balance, Percentage) is as often a value the person types
(the total of an invoice, the balance of an account): it is found only for a
request that asks for calculation;

calculated_row_empty: a row whose label names a derived quantity (a Total row)
left empty under the rows it should add up, in a table that has formulas
elsewhere or for a request that asks for calculation.

A plain list, or a template for a request that asks for no calculation, is
never found: without a formula in the table and without a calculation word in
the request there is nothing to compare. A header such as Total may also name
a value the person types, so the caller refuses a finding once, says how to
keep the file as it is, and then publishes it.

The word lists are data in English, French, German, Spanish and Italian,
written without accents (words are compared without theirs). Words that mean
something else in one of these languages are left out: "media" (an average in
Spanish and Italian), "sommes" ("we are" in French), "rest", "tempo", "auto".
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from openpyxl.utils import get_column_letter

from .cells import plain
from .schema import Content, Table

MISSING = "formulas_requested_but_missing"
COLUMN = "calculated_column_empty"
ROW = "calculated_row_empty"

# A request that asks for calculated values.
CALCULATION = frozenset(
    (
        # English
        "calculate calculates calculated calculating calculation calculations "
        "compute computes computed computing automatic automatically "
        "formula formulas formulae total totals subtotal subtotals sum sums "
        "average averages "
        # French
        "calcul calculs calcule calculee calcules calculees calculer calculent "
        "calculant automatique automatiques automatiquement formule formules "
        "totaux somme moyenne moyennes "
        # German
        "berechnen berechnet berechne berechnete berechneten berechnung "
        "berechnungen automatisch automatische automatischen automatischer "
        "automatisches formel formeln summe summen gesamt gesamtsumme "
        "gesamtbetrag insgesamt zwischensumme durchschnitt mittelwert "
        # Spanish
        "calcular calcula calculado calculada calculados calculadas calculo "
        "calculos automatico automatica automaticos automaticas "
        "automaticamente totales suma sumas subtotales promedio promedios "
        # Italian
        "calcolare calcola calcoli calcolato calcolata calcolati calcolate "
        "calcolo automatici automatiche formule totale totali somma subtotale "
        "subtotali"
    ).split()
)

# A header that relates other cells of its row: never a value to type.
RELATION = frozenset(
    (
        # English
        "average avg mean difference diff variance pace remaining "
        # French
        "moyenne ecart ecarts allure reste restant cumul "
        # German
        "durchschnitt mittelwert differenz abweichung restbetrag "
        # Spanish
        "promedio diferencia ritmo restante "
        # Italian
        "differenza andatura rimanente residuo"
    ).split()
)
# A header that names an amount: calculated, or typed by the person.
AMOUNT = frozenset(
    (
        # English
        "total totals subtotal subtotals sum percentage percent pct balance "
        # French
        "totaux somme pourcentage solde "
        # German
        "summe gesamt gesamtsumme gesamtbetrag zwischensumme prozent "
        "prozentsatz saldo "
        # Spanish
        "totales suma porcentaje "
        # Italian
        "totale totali subtotale somma percentuale"
    ).split()
)
# A header or a row label that names a quantity derived from other cells.
DERIVED = RELATION | AMOUNT

# "without formulas", "no totals", "pas de formule", "ohne Summe", "senza
# totale": the calculation word that follows is not a request for one. Only
# a partitive may stand in between ("n'oublie pas les totaux" asks for them),
# and a conjunction carries the negation on ("without formulas or totals").
NEGATION = frozenset(
    (
        "no not never without sans pas aucun aucune ni ohne kein keine keinen "
        "sin ningun ninguna senza nessun nessuna"
    ).split()
)
CARRIED = frozenset("de d des du any of di or nor ou oder o and et und y e".split())

REFERENCE = re.compile(r"\$?([A-Z]{1,3})\$?[0-9]{1,5}", re.I)


def words(text: str) -> list[str]:
    """The words of a text, lower case and without accents."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    return re.findall(
        r"[^\W\d_]+", "".join(c for c in folded if not unicodedata.combining(c))
    )


def asks_calculation(request: str) -> bool:
    """Whether the request asks for calculated values.

    A calculation word right after a negation is not a request, nor is the
    one that follows it ("sans calcul automatique").
    """
    denied = -2
    for index, word in enumerate(words(request)):
        if word in NEGATION or (word in CARRIED and index - 1 == denied):
            denied = index
        elif word in CALCULATION:
            if index - 1 != denied:
                return True
            denied = index
    return False


def names_derived(text: str, among: frozenset[str] = DERIVED) -> bool:
    return any(word in among for word in words(plain(text)))


@dataclass(frozen=True)
class Finding:
    code: str
    sheet: str = ""
    # The column letter or the row number, and the header or label written there.
    where: str = ""
    name: str = ""
    # For missing formulas: the empty columns to calculate (sheet, letter, header).
    columns: tuple[tuple[str, str, str], ...] = ()

    @property
    def reason(self) -> str:
        """What a turn is refused once for: no formula, or cells left empty."""
        return MISSING if self.code == MISSING else "calculated_cells_empty"


def own_total(cell: str, letter: str) -> bool:
    """A formula over its own column only: the total under a column."""
    if not cell.startswith("=") or "!" in cell:
        return False
    return {column.upper() for column in REFERENCE.findall(cell)} == {letter}


def empty_columns(
    table: Table, among: frozenset[str] = DERIVED
) -> list[tuple[str, str]]:
    """Columns named as a derived quantity that hold at most their own total.

    The first column holds the labels of the rows and is never one.
    """
    found: list[tuple[str, str]] = []
    for index in range(1, len(table.headers)):
        if not names_derived(table.headers[index], among):
            continue
        letter = get_column_letter(index + 1)
        cells = [row[index] for row in table.rows]
        if any(not cell for cell in cells) and all(
            not cell or own_total(cell, letter) for cell in cells
        ):
            found.append((letter, plain(table.headers[index])))
    return found


def empty_rows(table: Table) -> list[tuple[str, str]]:
    """Rows labelled as a derived quantity, left empty under labelled rows."""
    found: list[tuple[str, str]] = []
    labelled = False
    for number, row in enumerate(table.rows, 2):
        if labelled and len(row) > 1 and names_derived(row[0]) and not any(row[1:]):
            found.append((str(number), plain(row[0])))
        labelled = labelled or bool(row[0])
    return found


def finding(content: Content, request: str) -> Finding | None:
    """What a workbook does not calculate although it should, or None.

    Only the first table of a section holds formulas (see table_areas in
    spreadsheet.py), on the sheet numbered like its section.
    """
    asks = asks_calculation(request)
    tables = [
        (str(index), section.tables[0])
        for index, section in enumerate(content.sections, 1)
        if section.tables
    ]
    if not tables:
        return None
    if asks and not any(table.holds_formulas() for _, table in tables):
        return Finding(
            MISSING,
            columns=tuple(
                (sheet, letter, name)
                for sheet, table in tables
                for letter, name in empty_columns(table)
            )[:5],
        )
    for sheet, table in tables:
        if not (asks or table.holds_formulas()):
            continue
        for letter, name in empty_columns(table, DERIVED if asks else RELATION):
            return Finding(COLUMN, sheet, letter, name)
        for number, name in empty_rows(table):
            return Finding(ROW, sheet, number, name)
    return None
