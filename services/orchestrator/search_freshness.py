"""Search cache identity and lifetime, decided by code from the query alone.

Two searches that differ only in case, accents, punctuation, stop words or
word order share one cache entry and one in-flight request. A query whose
answer changes with time keeps the short lifetime; other answers are kept
for days, since asking upstream engines again returns the same pages and
spends the server's allowance with them.
"""

import re
import time
import unicodedata

# Lifetimes in seconds. Time-bound answers keep the previous hour; a result
# degraded by an engine failure is retried soon, since a later call may get
# the missing engine.
FRESH_TTL = 3 * 86400
TIME_BOUND_TTL = 3600
DEGRADED_TTL = 3600
DEGRADED_TIME_BOUND_TTL = 60

# Function words only. Negations ("sans", "without", "ohne", "not"),
# question words ("when" and "where" ask different things) and direction
# words are never removed.
STOP_WORDS = {
    "en": "a an and are as at be by for in is it of on or the with does do did "
    "i me my you your this that",
    "fr": "a au aux avec ce ces c d de des du en est et il la le les l leur "
    "ma mes mon ou par pour qu que sa se ses son sur ta te tes ton un une y",
    "de": "am an auf aus bei das dem den der des die ein eine einem einen "
    "einer eines es fur im in ist mit oder und vom zum zur",
    "es": "al con de del el en es la las lo los o para por que se su sus un una y",
    "it": "al alla alle con dal dalla de dei del della delle di e gli i il in "
    "la le lo o per che su un una uno",
}
# Word order carries the meaning of a route or a direction ("from Paris to
# Lyon", "von Ulm nach Bern"). The French, Spanish and Italian "de X a Y"
# form needs proper nouns: a price "in Lyon" is not a route to Lyon.
DIRECTION = re.compile(
    r"\bfrom\s+\S+(?:\s+\S+)?\s+to\b|\bvon\s+\S+(?:\s+\S+)?\s+(?:nach|bis|zu)\b"
    r"|\b(?:towards?|into|vers|jusqu|nach|hacia|desde|verso)\b",
    re.IGNORECASE,
)
ROUTE = re.compile(
    r"\b(?:de|da|del)\s+[A-ZÀ-Ý][\w'-]*\s+(?:à|a|au|al|hasta)\s+[A-ZÀ-Ý]"
)
# Search operators: exact phrases, "site:"-style filters and bangs.
OPERATORS = re.compile(r"[\"“”«»]|\b\w+:\S|(?:^|\s)[!:<]\w")
STOP = {lang: frozenset(words.split()) for lang, words in STOP_WORDS.items()}

# Words that mark an answer as bound to the present. Aligned with the
# research router (search_policy.required_research) and the news flag.
TIME_BOUND = re.compile(
    r"\b(today|tonight|yesterday|tomorrow|now|currently|current|latest|recent\w*"
    r"|news|breaking|live|this (?:week|month|year)|weather|forecast|score\w*"
    r"|price\w*|cost\w*|stock\w*|exchange rate|rates?|election\w*"
    r"|schedule\w*|opening hours|open now|release date|ceo|available|availability"
    r"|aujourd.hui|ce soir|hier|demain|maintenant|actuel\w*|dernier\w*|derniere\w*"
    r"|recent\w*|actualite\w*|nouvelles?|en direct|cette (?:semaine|annee)|ce mois"
    r"|en ce moment|right now|im moment|ahora mismo|al momento|in questo momento"
    r"|meteo|previsions?|prix|tarif\w*|cout\w*|cours|bourse|taux|elections?"
    r"|horaires?|pdg|disponib\w*|sortie"
    r"|heute|gestern|morgen|jetzt|aktuell\w*|neueste\w*|nachrichten|wetter"
    r"|preis\w*|kosten|kurs|borse|wahl\w*|verfugbar\w*"
    r"|hoy|ayer|manana|ahora|ultim\w*|noticias|tiempo|precio\w*|cotizacion"
    r"|elecciones|disponible"
    r"|oggi|ieri|domani|adesso|notizie|meteo|prezz\w*|quotazion\w*|elezioni"
    r"|disponibil\w*)\b"
)


def fold(text: str) -> str:
    """Case and accent folding shared by the cache key and the date words."""
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(c)
    )


def normalized_query(query: str, language: str) -> str:
    """Cache identity of a query within one search language.

    Takes the query as the model wrote it, before operator quoting. Exact
    phrases and operators keep their word order and stop words. A query
    with a direction keeps its order without stop words; otherwise the
    identity is the sorted set of content words.
    """
    text = fold(query)
    tokens = re.findall(r"[\w+#]+", text.replace("'", " ").replace("’", " "))
    if not tokens:
        return text.strip()
    if OPERATORS.search(query):
        return " ".join(tokens)
    content = [token for token in tokens if token not in STOP.get(language, ())]
    content = content or tokens
    if DIRECTION.search(text) or ROUTE.search(query):
        return " ".join(content)
    return " ".join(sorted(set(content)))


def time_bound(query: str, news: bool, now: float | None = None) -> bool:
    """Whether the answer depends on the present (deterministic)."""
    if news:
        return True
    text = fold(query)
    if TIME_BOUND.search(text):
        return True
    year = time.gmtime(now if now is not None else time.time()).tm_year
    # The current or the previous year asks for recent facts; an older year
    # names a settled past.
    return any(
        year - 1 <= int(found) <= year + 1
        for found in re.findall(r"\b(20\d\d)\b", text)
    )


def cache_ttl(*, bound: bool, degraded: bool) -> int:
    """Seconds a search result stays in the cache."""
    if degraded:
        return DEGRADED_TIME_BOUND_TTL if bound else DEGRADED_TTL
    return TIME_BOUND_TTL if bound else FRESH_TTL
