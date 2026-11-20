"""Mandatory discovery uses the same loop quotas as model-selected research."""

import json
import re
import unicodedata
from urllib.parse import urlsplit

from services.orchestrator.loop import Call
from services.orchestrator.link_requests import (
    own_words,
    page_request,
    product_queries,
)


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


# A self-contained computation: the person gives the numbers (or the dates) and
# asks for a result worked out from them. Staging 2026-11-20: a French question
# giving an old and a new price (84 and 63 euros) and asking for the percentage
# drop ran a required web search because its word for "costs" reads as a price
# question, and its
# unrelated hits (a stadium, a pharmacy) were listed under the answer. The ask
# must target the computed quantity: "why did the price drop by 20 %" is no
# computation and keeps its research.
_COMPUTE_ASK = re.compile(
    r"\b(?:quel(?:le)?s?|what|which|welche\w*|cual|quale?)\s++"
    r"(?:(?:est|sont|is|are|ist|es|e)\s++)?(?:(?:le|la|l|les|the|der|die|das|el|il|lo)\s++)?"
    r"(?:nouveau\s++|new\s++|final\s++)?"
    r"(?:pourcentage|percentage|percent|prozentsatz|porcentaje|percentuale|taux|rate|"
    r"difference|differenz|diferencia|differenza|ecart|total|somme|sum|summe|suma|somma|"
    r"moyenne|average|mean|durchschnitt|promedio|media|montant|amount|betrag|importe|importo|"
    r"prix final|final price|remise|discount|rabatt|descuento|sconto|reduction|marge|margin|"
    r"ratio|resultat|result|ergebnis|resultado|risultato)\b"
    r"|\b(?:calcul\w*|compute|computes|work out|berechne\w*|rechne\w*|calcola\w*|calcula\w*)\b"
    r"|\bcombien\b.{0,40}\b(?:au total|en tout|de baisse|de hausse|de reduction|de remise|"
    r"de difference|de plus|de moins|reste|font|fait)\b"
    r"|\bhow (?:much|many)\b.{0,40}\b(?:in total|altogether|left|remain\w*|more|less|"
    r"difference|saved?)\b"
    r"|\b(?:by how much|de combien|um wie viel|wie viel prozent|en cuanto|di quanto)\b"
    r"|\d\s*+[×x*/÷+−]\s*+\d|=\s*+\?"
)
_CURRENT = re.compile(
    r"\b(?:latest|current\w*|actuel\w*|aujourd.hui|today|heute|hoy|oggi|now|"
    r"maintenant|en ce moment|derniere?s?|neueste\w*|aktuell\w*|cours|"
    r"taux de change|exchange rates?|wechselkurs\w*|tipo de cambio|tasso di cambio)\b"
)


def computation_only(question: str) -> bool:
    """A question answered by computing on the numbers or dates it gives."""
    from services.orchestrator import exact_tools

    question = own_words(question)
    text = "".join(
        c
        for c in unicodedata.normalize("NFD", question.casefold().replace("’", "'"))
        if not unicodedata.combining(c)
    )
    if len(re.findall(r"\d+(?:[.,]\d+)?", text)) < 2:
        return False
    families = exact_tools.relevant(question)
    if "date" in families and (
        len(exact_tools._DATE.findall(text)) >= 2
        or re.search(r"\b(?:combien|how many|wie viele|cuant[oa]s|quant[ie])\b", text)
    ):
        # A count of days between given dates is calendar work (date_time),
        # "today" included: the clock is read by code.
        return True
    if _CURRENT.search(text):
        # A current value (a rate, a price today) is looked up, not computed.
        return False
    return "unit" in families or bool(_COMPUTE_ASK.search(text))


def required_research(
    question: str,
    language: str,
    *,
    device_timings: bool = False,
    history: tuple[str, ...] = (),
) -> tuple[Call, ...]:
    # Words of a pasted document (an article to summarise or check) never make
    # research mandatory, and never become a search query: only the person's own
    # words around it do, also when a single line break or a colon is all that
    # separates them (staging 2026-11-18: "Summarize this text in three
    # sentences:" then a pasted report on the next line sent the report to a web
    # search because it said "current").
    question = own_words(question)
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
    if page_request(question, history):
        return tuple(
            Call(
                "required-links" + (str(index) if index else ""),
                "web_search",
                json.dumps({"query": query, "lang": language}),
            )
            for index, query in enumerate(product_queries(question, history))
        )
    from services.orchestrator.person_lookup import person_name, queries

    if name := person_name(question):
        return queries(name, language)
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
    if not explicit and computation_only(question):
        # The person gave the numbers: code computes the result, nothing is
        # looked up (a price word is part of the statement, not a question).
        return ()
    if temporal or explicit or technical or (existing_things and not hypothetical):
        return (
            Call(
                "required-current",
                "web_search",
                json.dumps({"query": question[:500], "lang": language}),
            ),
        )
    return ()


def requested_page_read(question: str, history: tuple[str, ...] = ()) -> bool:
    """An explicit source-reading request must survive snippet-only discovery."""
    question = own_words(question)
    text = question.casefold()
    return page_request(question, history) or bool(
        re.search(
            r"\b(read|lis|lire|lisez|lese\w*|lies|lee|leer|leggi|leggere)\b"
            r".{0,50}\b(source|sources|page|article|quelle|seite|fuente|pagina|fonte)\b",
            text,
        )
    )
