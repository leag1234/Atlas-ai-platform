"""Server-side eligibility for the owner's file-production request allowance."""

import re
import unicodedata


def requests_file_delivery(text: str, *, include_images: bool = True) -> bool:
    """Check requested output separately from the legacy budget eligibility."""
    # Upload normalization appends input filenames to the user text. Their
    # extensions must not turn "write a summary" into a request for a PDF.
    text = re.sub(r"^\[Attachment: [^\n]*\]$", "", text, flags=re.M)
    text = re.sub(r"<attached_files>.*?</attached_files>", "", text, flags=re.S)
    return produces_file(text, include_images=include_images)


def produces_file(text: str, *, include_images: bool = True) -> bool:
    text = "".join(
        c
        for c in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(c)
    )
    if include_images and re.search(r"\b(dessine\w*|zeichne\w*|dibuja\w*)\b", text):
        return True
    artifact = re.search(
        r"\b(pdf|docx|xlsx|pptx|csv|document|presentation|diaporama|slides?"
        r"|spreadsheet|tableur|fichier|file|datei|dokument|prasentation"
        r"|tabellenkalkulation|archivo|documento|presentacion|foglio|presentazione"
        + (r"|image|imagen|immagine|bild" if include_images else "")
        + r")\b",
        text,
    )
    action = re.search(
        r"\b(cre\w*|gener\w*|produ\w*|export\w*|convert\w*|transform\w*"
        r"|make|build|write|prepare|fais|fait\w*|redig\w*|prepar\w*"
        r"|erstell\w*|schreib\w*|erstelle|haz|scrivi|disegn\w*|draw"
        r"|telecharge\w*|download\w*|herunterlad\w*|descarg\w*|scaric\w*)\b",
        text,
    )
    return artifact is not None and (
        action is not None
        or (
            len(text.split()) <= 4
            and not re.search(
                r"\b(what|why|how|explain|expliqu\w*|explica\w*|erklar\w*|spieg\w*|means|was|ist|comment|pourquoi|que)\b",
                text,
            )
        )
    )


def edits_attachment(text: str) -> bool:
    """A request to change an attached file expects that file back (J43: "corrige ce fichier word")."""
    plain = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return bool(
        re.search(
            r"\b(cre[ea]\w*|ajout\w*|insere\w*|add\w*|erganz\w*|anad\w*|aggiung\w*|corrig\w*|nettoi\w*|nettoy\w*|modifi\w*|revis\w*|reformat\w*|restructur\w*"
            r"|mets? a jour|met a jour|complete\w*|traduis\w*|translate\w*|edit\w*|fix\w*|correct\w*"
            r"|update\w*|clean\w*|rewrite\w*|korrigier\w*|bearbeit\w*|uberarbeit\w*|aktualisier\w*"
            r"|corrig\w*|edita\w*|actualiz\w*|modific\w*|aggiorn\w*)\b",
            plain,
        )
    )
