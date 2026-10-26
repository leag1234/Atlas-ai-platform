"""Code-side link discovery and bounded shopping answer policy (D7)."""

import re
import unicodedata
from collections.abc import Sequence


def link_request(question: str, *, prices: bool = False) -> bool:
    # Upload transport metadata is not a request for URLs or product prices.
    question = re.sub(
        r"<attached_files>.*?</attached_files>", "", question, flags=re.S | re.I
    )
    text = "".join(
        c
        for c in unicodedata.normalize("NFD", question.casefold())
        if not unicodedata.combining(c)
    )
    # "Cost the ..." asks us to calculate a budget, not retrieve a market price.
    if re.match(r"^(?:please\s+)?cost\s+(?:the|this|these|my|our|a|an)\b", text):
        return False
    if re.search(
        r"\b(without|no|sans|pas de|ohne|keine?)\s+(?:web\s+)?(?:links?|liens?|recherche|search|suche)\b"
        r"|\b(do not|don't|ne|nicht)\b.{0,40}\b(search|cherch\w*|such\w*)\b"
        r"|\b(internal|interne|confidential|confidentiel|vertraulich)\b",
        text,
    ):
        return False
    price_request = prices and bool(
        re.search(
            r"\b(prix|prices?|preis\w*|tarifs?|costs?|coute|coutent|kostet|kosten)\b"
            r"|\b(how much (?:is|does|for)|combien (?:ca )?(?:vaut|pour)|wie teuer)\b",
            text,
        )
    )
    return price_request or bool(
        re.search(
            r"\b(liens?|links?|urls?|shopping|acheter|buy|kaufen)\b"
            r"|\b(ou|where|wo)\b.{0,35}\b(achet\w*|trouv\w*|buy|get|purchase|kauf\w*|bekomm\w*)\b"
            r"|\b(product|produit|produkt)\s*(pages?|seiten?)\b"
            r"|\b(pages?|fiches?)\s+(?:de\s+)?produit\w*\b"
            r"|\b(show|montre\w*|affiche\w*|zeig\w*)\b.{0,35}\b(pages?|seiten?)\b",
            text,
        )
    )


def page_request(question: str, history: Sequence[str] = ()) -> bool:
    if link_request(question):
        return True
    # A price-only question inherits shopping intent only from the active thread.
    return (
        bool(history)
        and any(link_request(turn) for turn in history[-2:])
        and (
            link_request(question, prices=True)
            or bool(
                re.search(r"(?i)\b(et chez|what about|and those|und bei)\b", question)
            )
        )
    )


def product_queries(question: str, history: Sequence[str] = ()) -> tuple[str, ...]:
    # Identifiers come from the user's request or the latest recommendation, never
    # from a product catalogue embedded in routing code.
    context = question + "\n" + (history[-1] if history else "")
    context = re.sub(r"https?://\S+", "", context)
    products = list(
        dict.fromkeys(
            re.findall(
                r"\b[A-Z][a-zA-Z]+[ \t]+[A-Z]{1,4}(?:-[A-Z]{1,4})?[- ]?\d{1,4}[A-Za-z0-9-]*\b",
                context,
            )
        )
    )
    shopping = re.search(
        r"(?i)\b(thomann|woodbrass|boulanger|shop\w*|achet\w*|buy|kauf\w*)\b", question
    )
    if not products or not shopping:
        return (link_query(question, history),)
    # One query per model, targeted at multiple shops, stays within the existing
    # three-search quota even when three models were recommended.
    shops = "(site:thomann.de OR site:woodbrass.com OR site:boulanger.com)"
    return tuple(product + " " + shops for product in products[:3])


def link_query(question: str, history: Sequence[str] = ()) -> str:
    # History supplies the referent, never provenance. Keep user wording first.
    context = re.sub(r"https?://\S+", "", " ".join(history[-2:]))
    return (question + (" " + context if context else ""))[:500]


def fallback_terms(question: str, history: Sequence[str] = ()) -> str:
    """Give a concise query hint without echoing the complete request."""
    context = re.sub(r"https?://\S+", "", question + "\n" + "\n".join(history[-1:]))
    products = list(
        dict.fromkeys(
            re.findall(
                r"\b[A-Z][a-zA-Z]+[ \t]+[A-Z]{1,4}(?:-[A-Z]{1,4})?[- ]?\d{1,4}[A-Za-z0-9-]*\b",
                context,
            )
        )
    )
    return ", ".join(products[:3]) if products else "requested product or page"


def instruction() -> str:
    return (
        "\nThis request requires verified links: use web_search and web_fetch to "
        "find and read the requested pages, using previous recommendations as context. "
        "For shopping, group by product and give the shop name and product page link, "
        "2 to 3 shops per recommended model. Search each product with shop site filters. "
        "A product URL returned by web_search is retrieved and can be linked even "
        "when the shop blocks web_fetch. Include a price only when explicitly read "
        "on that fetched page or in that URL's search snippet, naming the evidence "
        "source and 'price at the time of reading' in the user's language. "
        "Otherwise omit the price and say it could not be verified. Never invent "
        "links or prices. Never transfer a price between URLs. "
        "Use ordinary bullets, not shop headings or setext underlines. If retrieval "
        "fails, state that verified links could not be retrieved and give search terms.\n"
    )


def unavailable(question: str, language: str) -> str:
    labels = {
        "fr": "Je n’ai pas pu récupérer de liens vérifiés. Termes à rechercher :",
        "de": "Ich konnte keine verifizierten Links abrufen. Suchbegriffe:",
        "en": "I could not retrieve verified links. Search terms:",
        "es": "No pude obtener enlaces verificados. Términos de búsqueda:",
        "it": "Non ho potuto recuperare link verificati. Termini di ricerca:",
    }
    terms = re.sub(r"[\n\r`<>#*\[\]]", " ", question)
    terms = re.sub(r"https?://\S+", "", terms)
    return labels.get(language, labels["en"]) + "\n\n" + terms[:500].strip()
