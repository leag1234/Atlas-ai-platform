"""Resolve conversation language independently of model output and UI labels."""

from __future__ import annotations

import importlib
import json
import math
from pathlib import Path
import re
from collections.abc import Callable, Sequence
import unicodedata


from typing import Protocol


class LanguageMessage(Protocol):
    @property
    def role(self) -> str: ...

    @property
    def text(self) -> str: ...


LANGUAGES = ("fr", "de", "es", "it", "en")


_CLOSER = {"`": "`", '"': '"', "«": "»"}
_QUOTE_OPENING = re.compile('[`"«]')


def without_quoted(text: str) -> str:
    """The text without `code`, "quoted" and «quoted» spans, like the former
    re.sub(r'`[^`]*`|"[^"]*"|«[^»]*»', "", text), in one pass: an opening
    without a closer after it is kept, and so is every later one of its kind
    (the expression reread the rest of the text from each: quadratic). This
    module is copied alone into the chat UI image, so it uses no other package.
    """
    parts: list[str] = []
    last = position = 0
    closed: set[str] = set()
    while (found := _QUOTE_OPENING.search(text, position)) is not None:
        position = found.start()
        char = found[0]
        closer = _CLOSER[char]
        if char in closed:
            position += 1
            continue
        end = text.find(closer, position + 1)
        if end < 0:
            closed.add(char)
            position += 1
            continue
        parts.append(text[last:position])
        last = position = end + 1
    parts.append(text[last:])
    return "".join(parts)


def status_language(messages: Sequence[LanguageMessage], locale: object) -> str:
    """Interface text follows a supported UI locale, then conversation prose."""
    if locale == "nl":
        return "en"
    if isinstance(locale, str) and locale in LANGUAGES:
        return locale
    return conversation_language(messages, "en") if messages else "en"


def user_prose(text: str) -> str:
    text = re.sub(r"<attached_files>.*?</attached_files>", "", text, flags=re.S)
    return re.sub(r"^\[Attachment: [^\n]*\]$", "", text, flags=re.M).strip()


# Language identification for the answer: the user's language, robustly, for
# short messages without accents, typos and SMS style ("il fera beau demain a
# caen?" came back Welsh, staging 2026-11-16). Three kinds of evidence add up as
# log-scores, one per language:
#
# - character n-grams: langdetect's profiles (55 languages, Apache-2.0, already a
#   dependency and present in the Open WebUI image), scored as a deterministic
#   naive Bayes. langdetect's own detect_langs samples the n-grams of the text
#   at random until one language passes 0.99999, so three words reach near
#   certainty for whichever language their few n-grams lean to;
# - words: frequent words of the five tuned languages and two neighbours,
#   written without accents, with their SMS forms (LEXICON): Wikipedia-trained
#   n-grams know neither. On a few words, n-grams lean hard on content words,
#   so their weight grows with the length of the text;
# - priors: the five tuned languages, the languages the conversation already
#   uses and the interface locale are expected. Any other language must win by
#   RARE_PENALTY nats of evidence, which a sentence in that language gives and a
#   few ambiguous words do not.
#
# Survey (2026-11-16, raw detection of the texts of
# tests/fixtures/language-detection.json, no priors): langdetect 81 %, lingua-py
# 90 % (Apache-2.0, 292 MB installed), py3langid 91 % (BSD-3, 4.5 MB, a new
# dependency of the shared runtime and of the Open WebUI image), fastText
# lid.176 93 % (model CC BY-SA 3.0, downloaded at run time by its Python
# package). No raw detector is enough on short unaccented messages: the priors
# and the words do the rest (tools/language_eval.py measures the whole chain).
# langdetect has no Irish profile: Irish is not identified.

NGRAM_TEXT_LIMIT = 1000
NGRAM_WEIGHT = 0.5
FULL_NGRAM_WORDS = 8.0
RARE_PENALTY = 8.0
WORD_WEIGHT = 8.0
HISTORY_BONUS = 4.0
LOCALE_BONUS = 1.0
MIN_MARGIN = 0.5

# Frequent words, without accents. A word listed for several languages counts
# for each of them, split. Single letters are left out: "c" is French SMS for
# "c'est" and the unaccented Italian "c'e", "e" is Italian and Portuguese.
LEXICON = {
    "fr": """
        le les des du un une est sont et mais donc pour par avec sans sur dans chez
        je tu il elle on nous vous ils elles moi toi lui leur mon ton ma ta sa mes
        tes ses notre votre ce cet cette ces ca cela qui quoi quel quelle quels
        quelles comment pourquoi combien quand pas plus tres bien bon bonne beau
        belle fait faire fais peux peut veux veut vais va suis sommes etes ont avez
        ai aussi encore toujours jamais rien tout tous toute demain hier
        aujourd aujourdhui soir matin semaine heure heures pleut neige merci bonjour
        salut coucou svp stp slt bjr cc jsp tkt mdr ptdr pk pq pcq koi kel cb
        jsuis jveux jai jpeux chui voir besoin idee idees recette aide aider
        voudrais faut chose mieux quelque peu trop beaucoup apres avant depuis
        pendant entre vers sous meme autre prochain prochaine dernier gratuit ne
        oui deja ici au aux vrai ecris ecrire explique expliquer dis donne montre
        cherche trouve rappelle cest dessine decris genere francais synthese resume
        temps ou etait qu sa marche mal tete prix cher pres loin gare la de en si ya
        lundi mardi mercredi jeudi vendredi samedi dimanche meteo horaires aller
        venir dire savoir prendre mettre manger trouver acheter
    """,
    "en": """
        the an and or but so for with without on at to of from by about is are was
        were be been am do does did have has had can could will would should may
        might must i you he she it we they me him her us them my your his its our
        their this that these those what which who whom whose how why when where
        there here not no yes very just also only really please pls plz thanks
        thank thx ty u r ur im dont cant wont doesnt isnt whats hows wheres idk tbh
        lol omg btw rn tmrw tomorrow today tonight yesterday weather rain sunny best
        good help need want get make give tell show write explain find any some
        more much many long time near ideas idea name gonna wanna gotta hey hi
        hello like know think cheap free way go going out new old define
        describe monday tuesday wednesday thursday friday saturday sunday weekend
        see come say take buy eat find
    """,
    "de": """
        der die das den dem des ein eine einen einem einer und oder aber nicht kein
        keine ist sind war bin bist hat habe hast haben wird werden kann kannst
        koennen konnen muss musst soll will ich du er sie es wir ihr mir mich dir
        dich uns euch mein meine dein deine sein seine wie was wer wo wann warum
        wieso welche welcher welches wieviel viel viele sehr auch noch schon nur mal
        gibt gibts gehts geht heute morgen gestern abend wetter regnet regen schnee
        bitte danke hallo moin servus tschuss ja nein mit fuer fur von zu zum zur
        bei nach aus auf uber ueber unter vor gegen ohne um im ins vom beim etwas
        nichts alles gut neue neu gross klein machen mach gib zeig schreib erklaer
        erklare erklaere hilf hilfe brauche brauch kostet lange dauert spaet uhr
        naechste nachste zwischen unterschied eigentlich gerade tun ob wenn dann es in
        an am so da denn doch hier jetzt immer darf duerfen durfen moechte mochte
        wuerde wurde montag dienstag mittwoch donnerstag freitag samstag sonntag
        sonntags wochenende gehen kommen sagen wissen sehen essen kaufen finden
        nehmen
    """,
    "es": """
        el los las un una unos unas y o pero porque por para con sin sobre del al
        es son esta estan estoy hay ser estar tengo tiene tienes quiero puedo
        puedes puede hacer hace hago haz dame dime explicame ayudame que como
        cuando donde quien cual cuanto cuanta cuantos cuantas yo mi mis su sus me
        te se lo le les nos muy mas menos tambien ya todavia hoy manana ayer noche
        tiempo llover llueve hola gracias buenas buenos dias vale tio guay q xq tqm
        finde peli movil necesito ayuda bien mal esto eso este aqui algo nada todo
        cosa hora va voy tarda cuesta explica escribe la de en si no tu un lunes
        martes miercoles jueves viernes sabado domingo busco ir ver dar saber decir
        poder tener hablar comer comprar encontrar
    """,
    "it": """
        il lo la gli le uno una di da in con su per tra fra del della dei delle
        degli al alla ai nel nella sul sulla che chi cosa come dove quando perche
        quanto quanta quanti quante quale sono sei siamo ha hai ho abbiamo avete
        hanno fa fai fare faccio posso puoi puo voglio vuoi serve mi ti ci vi si
        mio mia tuo tua suo sua non piu molto anche gia ancora oggi domani ieri
        sera stasera tempo piove pioggia ciao grazie buongiorno buonasera prego
        scusa allora quindi pero questo questa quello quella niente tutto qualcosa
        bene male aiuto aiutami dimmi dammi spiegami scrivi nn xke xche cmq tvb
        qnd qlc ore costa dura spiega de no lunedi martedi mercoledi giovedi venerdi
        sabato domenica meteo cos vedere andare dire sapere venire mangiare trovare
        comprare prendere
    """,
    # Two frequent neighbours, so that their words are not read as Spanish or
    # German ones: Dutch (an interface locale) and Portuguese.
    "nl": """
        de het een en of maar dat die dit deze is zijn was waren ben bent heb hebt
        heeft hebben kan kun kunt kunnen wil wilt willen moet moeten zal zou ik jij
        je hij zij ze wij we jullie mij me mijn jouw jou haar ons onze hun wat wie
        waar wanneer waarom hoe hoeveel welk welke niet geen wel ook nog al heel erg
        veel meer vandaag morgen gisteren avond weer regen mooi goed dank bedankt
        alsjeblieft hallo hoi doei ja nee met voor van naar op in aan uit over bij
        tot om te er hier daar iets niets alles kost maandag dinsdag woensdag
        donderdag vrijdag zaterdag zondag weekend
    """,
    "pt": """
        o os as um uma uns umas de do da dos das no na nos nas em por para com sem
        sobre que quem qual quais quanto quanta como onde quando porque nao sim mais
        muito muita tambem ja ainda hoje amanha ontem noite tempo voce voces eu tu
        ele ela eles elas meu minha teu tua seu sua isso isto esse essa este esta
        aqui ali algo nada tudo coisa obrigado obrigada ola oi bom boa dia tarde
        tchau preciso quero posso pode podes fazer faz faco tenho tem tens ser estar
        estou sao foi vai vou ir ver falar dizer saber ajuda ajudar consigo sei
        favor ao aos pelo pela segunda terca quarta quinta sexta sabado domingo
        comer comprar encontrar
    """,
}

# Greetings said in every language are no evidence: "Hello" with a German
# interface keeps German (its maintenance notice came back English), while
# "Bonjour", "Merci" or "Thanks" still name their language.
INTERNATIONAL_WORDS = frozenset({"hello", "hi", "hey", "ciao", "ok", "okay", "bye"})

_WORD_LANGUAGES: dict[str, tuple[str, ...]] = {}
for _language, _words in LEXICON.items():
    for _word in set(_words.split()) - INTERNATIONAL_WORDS:
        _WORD_LANGUAGES[_word] = (*_WORD_LANGUAGES.get(_word, ()), _language)

_PROFILES: tuple[tuple[str, ...], dict[str, tuple[float, ...]]] | None = None
_LOG_PROBABILITIES: dict[str, tuple[float, ...]] = {}
_SPACELESS_SCRIPT = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]+")


def _unaccented(text: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(c)
    )


def word_count(text: str) -> int:
    """Words of the text; scripts written without spaces count two characters a word."""
    spaceless = _SPACELESS_SCRIPT.findall(text)
    spaced = _SPACELESS_SCRIPT.sub(" ", text)
    return len(re.findall(r"\b[\w'-]+\b", spaced)) + sum(
        max(1, len(run) // 2) for run in spaceless
    )


def _word_scores(text: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for word in re.findall(r"[a-z]+", _unaccented(text)):
        languages = _WORD_LANGUAGES.get(word, ())
        for language in languages:
            scores[language] = scores.get(language, 0.0) + 1 / len(languages)
    return scores


def _ngram_scores(text: str) -> dict[str, float]:
    """Log-likelihood of the text's character n-grams under each language profile."""
    global _PROFILES
    factory = importlib.import_module("langdetect.detector_factory")
    # The module's shared factory, as detect_langs uses it: its profiles load once.
    factory.init_factory()
    detector = factory._factory.create()
    if _PROFILES is None:
        _PROFILES = (tuple(detector.langlist), detector.word_lang_prob_map)
    languages, profiles = _PROFILES
    # Capitalization carries emphasis, not language; langdetect also skips the
    # n-grams of capitalized words, so an all-uppercase message would vanish.
    detector.append(text.lower() if text.isupper() else text)
    detector.cleaning_text()
    totals = [0.0] * len(languages)
    for gram in detector._extract_ngrams():
        logs = _LOG_PROBABILITIES.get(gram)
        if logs is None:
            # langdetect's own smoothing: alpha 0.5 over a base frequency of 10000.
            logs = tuple(math.log(0.5 / 10000 + p) for p in profiles[gram])
            _LOG_PROBABILITIES[gram] = logs
        totals = [total + log for total, log in zip(totals, logs)]
    return dict(zip(languages, totals)) if any(totals) else {}


def language_scores(
    text: str, known: Sequence[str] = (), locale: str | None = None
) -> dict[str, float]:
    """Log-scores of the languages the text may be in; empty without evidence.

    `known` are the languages the conversation already uses and `locale` the
    interface's: both are expected, any language outside them and the five
    tuned ones needs strong evidence.
    """
    text = user_prose(text)[:NGRAM_TEXT_LIMIT]
    if not re.search(r"[^\W\d_]", text):
        return {}
    ngrams = _ngram_scores(text)
    words = _word_scores(text)
    if not ngrams and not words:
        return {}
    # Scripts settle what n-grams of Han characters cannot: Korean is written
    # with Hangul, Japanese with kana, Chinese with neither.
    if not re.search(r"[\uac00-\ud7af\u1100-\u11ff]", text):
        ngrams.pop("ko", None)
    if re.search(r"[\u3040-\u30ff]", text):
        ngrams.pop("zh-cn", None)
        ngrams.pop("zh-tw", None)
    else:
        ngrams.pop("ja", None)
    top = max(ngrams.values(), default=0.0)
    # The n-grams of a few words lean hard on their content words ("Explain
    # quantum entanglement" scores French by 24 nats): their weight grows with
    # the length of the text, up to FULL_NGRAM_WORDS words.
    weight = NGRAM_WEIGHT * min(1.0, word_count(text) / FULL_NGRAM_WORDS)
    scores: dict[str, float] = {}
    for language in {*ngrams, *words, *LANGUAGES}:
        score = weight * (ngrams.get(language, top - 50.0) - top)
        score += WORD_WEIGHT * words.get(language, 0.0)
        if language in known:
            score += HISTORY_BONUS
        elif language not in LANGUAGES:
            score -= RARE_PENALTY
        if language == locale:
            score += LOCALE_BONUS
        scores[language] = score
    return scores


def _best(scores: dict[str, float], among: Sequence[str] | None = None) -> str | None:
    ranked = sorted(
        (
            (score, language)
            for language, score in scores.items()
            if among is None or language in among
        ),
        reverse=True,
    )
    if not ranked or (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < MIN_MARGIN):
        return None
    return ranked[0][1]


def has_language_words(text: str) -> bool:
    """Whether the text holds frequent words of one of the five tuned languages."""
    return any(language in LANGUAGES for language in _word_scores(user_prose(text)))


def detected_language(
    text: str, known: Sequence[str] = (), locale: str | None = None
) -> str | None:
    """The language of the text among the five tuned conversation languages."""
    return _best(language_scores(text, known, locale), LANGUAGES)


def identified_language(
    text: str, known: Sequence[str] = (), locale: str | None = None
) -> str | None:
    """The language of the user's prose: any language the detector identifies.

    Codes are the detector's (ISO 639-1, plus zh-cn and zh-tw).
    """
    return _best(language_scores(text, known, locale))


def question_language(text: str, fallback: str) -> str:
    return detected_language(text) or fallback


def requested_language(text: str) -> str | None:
    """Resolve explicit output-language instructions, independently of prose locale."""
    normalized = "".join(
        c
        for c in unicodedata.normalize("NFKD", user_prose(text).lower())
        if not unicodedata.combining(c)
    )
    normalized = without_quoted(normalized)
    names = {
        "en": "english|anglais|englisch|ingles|inglese",
        "fr": "french|francais|franzosisch|frances|francese",
        "de": "german|allemand|deutsch(?:e[nmrs]?)?|aleman|tedesco",
        "es": "spanish|espagnol|spanisch|espanol|spagnolo",
        "it": "italian|italien|italienisch|italiano",
    }
    directive = r"\b(?:translat\w*|tradui\w*|tradu[cz]\w*|traduc\w*|tradurr\w*|uberset\w*|answer|reply|respond\w*|repond\w*|antworte\w*|rispondi)\b"
    chosen: str | None = None
    for clause in re.split(r"[.!?;\n]", normalized):
        if re.search(r"\b(?:not|don't|never|pas|ne|nicht|no|non)\b", clause):
            continue
        if not re.search(directive, clause) and not re.match(
            r"^\s*(?:(?:please|bitte)\s+)?(?:in|en|auf)\b", clause
        ):
            continue
        matches = [
            (match.start(), language)
            for language, aliases in names.items()
            for match in re.finditer(
                r"\b(?:into|to|in|en|al|auf|ins|vers le)\s+(?:" + aliases + r")\b",
                clause,
            )
        ]
        if matches:
            chosen = max(matches)[1]
    return chosen


def _previous_language(
    messages: Sequence[LanguageMessage],
    detect: Callable[[str, Sequence[str], str | None], str | None],
    locale: str | None,
) -> str | None:
    """The language of the conversation before its last message: the user's
    turns decide, the assistant's only when the user's carry no evidence."""
    for role in ("user", "assistant"):
        found: str | None = None
        for message in messages[:-1]:
            if message.role == role:
                found = detect(message.text, (found,) if found else (), locale) or found
        if found is not None:
            return found
    return None


def _carries_evidence(text: str, history: str | None) -> bool:
    # A message of a few words keeps the conversation's language ("Describe
    # this image" in a French conversation); a first one without one frequent
    # word ("Tesla stock price", "Caen") keeps the interface's.
    return word_count(text) >= 5 or (history is None and has_language_words(text))


def conversation_language(messages: Sequence[LanguageMessage], locale: str) -> str:
    # Dutch is supported by search; text UI/prompt translations remain unchanged.
    if locale == "nl":
        locale = "en"
    if locale not in LANGUAGES:
        raise ValueError("unsupported_language")
    history = _previous_language(messages, detected_language, locale)
    current = user_prose(messages[-1].text)
    fallback = history or locale
    if not _carries_evidence(current, history):
        return fallback
    scores = language_scores(current, (history,) if history else (), locale)
    # A message in another language keeps the interface in the conversation's.
    if _best(scores) not in (None, *LANGUAGES):
        return fallback
    return _best(scores, LANGUAGES) or fallback


def language_name(language: str) -> str | None:
    """The English name of a language code as the detector reports it."""
    tag = {"zh-cn": "zh_Hans", "zh-tw": "zh_Hant"}.get(language, language)
    if not re.fullmatch(r"[a-z]{2,3}(?:_Han[st])?", tag):
        return None
    from babel import Locale

    # CLDR names (Babel, already a dependency) cover every code the detector reports.
    name = Locale("en").languages.get(tag)
    return str(name) if name else None


def prose_language(messages: Sequence[LanguageMessage], language: str) -> str:
    """The language of the answer's prose.

    `language` is the conversation language among the five tuned ones, which also
    drives interface and status texts. A conversation written in another language
    the detector identifies is answered in that language: a Portuguese question
    was answered in English (corpus 2026-11-11). Any other language must win by a
    clear margin over the five, the conversation's and the interface's languages:
    unaccented French came back Welsh (staging 2026-11-16). A message of a few
    words without frequent words keeps the language of the conversation so far.
    """
    if not messages:
        return language
    current = user_prose(messages[-1].text)
    if requested_language(current) is not None:
        return language
    history = _previous_language(messages, identified_language, language)
    found = history
    if _carries_evidence(current, history):
        known = (history,) if history else ()
        found = identified_language(current, known, language) or history
    if found is None or found in LANGUAGES or language_name(found) is None:
        return language
    return found


def language_instruction(language: str) -> str:
    """The prose language instruction: tuned for the five, generic for the others."""
    instructions = json.loads(Path("prompts/language.json").read_text())
    if language in LANGUAGES:
        return str(instructions[language])
    name = language_name(language)
    template = str(instructions["en"])
    stated = "Answer in English."
    if name is None or stated not in template:
        raise ValueError("unsupported_language")
    # The other languages share the English instruction, code rules included; the
    # detector can confuse close languages (Portuguese without accents came back
    # Catalan), so the user's own message decides when it plainly differs.
    return template.replace(
        stated,
        f"Answer in {name}, the language the user writes in; if the user's message "
        "is plainly in another language, answer in that language.",
        1,
    )
