"""Mandatory discovery uses the same loop quotas as model-selected research."""

import json
import re
import unicodedata
from urllib.parse import urlsplit

from services.orchestrator.loop import Call


def corpus_allowed(question: str) -> bool:
    text = "".join(
        c
        for c in unicodedata.normalize("NFD", question.casefold())
        if not unicodedata.combining(c)
    )
    scope = r"\b(corpus|memoire|memory|intern\w*|retrieval)\b"
    return not re.search(
        r"\b(sans|without|ohne|sin|senza)\b[^.!?\n]{0,80}"
        + scope
        + r"|\bne\b[^.!?\n]{0,50}\bpas\b[^.!?\n]{0,80}"
        + scope
        + r"|\bn['’][^.!?\n]{0,30}\b(aucun|pas)\b[^.!?\n]{0,80}"
        + scope
        + r"|\b(do not|don't|never|keine?)\b[^.!?\n]{0,80}"
        + scope,
        text,
    )


def required_research(
    question: str, language: str, *, device_timings: bool = False
) -> tuple[Call, ...]:
    normalized = "".join(
        c
        for c in unicodedata.normalize("NFD", question.casefold())
        if not unicodedata.combining(c)
    )
    # Internal evidence must be retrieved in scope before any public research;
    # the model handles mixed requests under the existing internal-first policy.
    if re.search(
        r"\b(internal|interne|internen?|interna|interno|confidentiel|confidential|confidencial|vertraulich)\b",
        normalized,
    ):
        return ()
    urls = re.findall(r'https?://[^\s<>"\']+', question)
    practical = re.search(
        r"\b(comment|how|wie|como|come)\b.{0,80}\b(fabriqu\w*|constru\w*|build|make|bau\w*|camera|appareil)\b|\b(compar\w*|recommend\w*|recommand\w*|empfehl\w*|consigli\w*)\b",
        normalized,
    )
    temporal = re.search(
        r"\b(ipo|pdg|ceo|prix|price|costs?|coute|tarif\w*|prezzo|precio|preis|disponib\w*|available|verfugbar|latest|current|actuel\w*|derniere? version|neueste\w*|aujourd.hui|today|heute)\b|\b(qui|who|wer)\b.{0,60}\b(dirige|runs|leads|chef|leitet|fuhrt|president)\b|\b(is|est|ist)\b.{0,80}\b(public|cotee?|still|encore|noch)\b",
        normalized,
    )
    if urls and practical:
        url = urls[0].rstrip(".,;!?")
        topic = urlsplit(url).path.rsplit("/", 1)[-1].replace("-", " ")
        query = topic + " practical how to tutorial"
        return (
            Call(
                "required-source",
                "web_fetch",
                json.dumps({"url": url, "query": question}),
            ),
            Call(
                "required-subject",
                "web_search",
                json.dumps({"query": query[:500], "lang": language}),
            ),
        )
    explicit = re.search(
        r"\b(recherch\w*|search|suche|busca|cerca)\b.{0,60}\b(web|internet|sources?)\b",
        normalized,
    )
    technical = device_timings and (
        re.search(
            r"\b(timing\w*|microsecond\w*|mikrosekund\w*|cycle\w*|zykl\w*|duree\w*|duration\w*|zeit\w*|throughput|debit|specification\w*)\b|\b(how long|combien de temps|wie lange)\b",
            normalized,
        )
        # An explicit technical identifier deserves discovery even in a short
        # question; preserve the user's wording, never insert a benchmark answer.
        or (
            len(question.split()) <= 4
            and re.search(r"\b[A-Z][A-Z0-9_]{2,}\b", question)
        )
    )
    existing_things = re.search(
        r"\b(products?|produits?|devices?|appareils?|versions?|logiciels?|software|"
        r"firmware|preamp\w*|preampli\w*|monnaies?|currenc\w*|wahrung\w*|"
        r"dispositifs?|frameworks?|libraries|bibliotheques?|features?|fonctionnalites?|"
        r"caracteristiques?|compatibil\w*)\b",
        normalized,
    )
    hypothetical = re.search(
        r"\b(ficti\w*|imaginary|hypothetical|erfunden\w*)\b", normalized
    )
    if temporal or explicit or technical or (existing_things and not hypothetical):
        return (
            Call(
                "required-current",
                "web_search",
                json.dumps({"query": question[:500], "lang": language}),
            ),
        )
    return ()


def requested_page_read(question: str) -> bool:
    """An explicit source-reading request must survive snippet-only discovery."""
    text = question.casefold()
    return bool(
        re.search(
            r"\b(read|lis|lire|lisez|lese\w*|lies|lee|leer|leggi|leggere)\b"
            r".{0,50}\b(source|sources|page|article|quelle|seite|fuente|pagina|fonte)\b",
            text,
        )
    )
