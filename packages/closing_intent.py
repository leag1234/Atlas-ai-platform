"""Polite closings: a thanks or a compliment that asks for nothing.

After an app, a drawing, a file or a script was delivered, "merci, c'est
parfait !", "thanks, looks great" or "super merci" sometimes went to the app
path again (staging 2026-11-16): the model router read a follow-up on the
delivered thing, and the turn rebuilt or reopened it. A closing is answered
with a short text in the chat.

The test is deliberately narrow, so that a request is never lost: the message
is short, says thanks or praises, and every word belongs to the vocabulary of
closings (thanks, praise, "it works", the delivered thing, small words), in
French, English, German, Spanish and Italian, with emojis, repeated letters
and one-letter typos on the most common thanks and praise words. Any other word (a change
verb such as "ajoute" or "add", "but", "now", "again", a yes, a "done", a
negation, a question) means it is not a closing, and the other rules decide:
"merci, mais mets le fond en bleu" and "thanks, now add a timer" stay changes.
"""

from __future__ import annotations

import re
import unicodedata

# At most this many words: a longer message says something more than thanks.
MAX_WORDS = 14

# Thanks and praise: at least one of them makes a closing.
_MARKERS = frozenset(
    """
    merci mercis remerciements thanks thank thx thnx thanx thks tks ty tyvm cheers
    danke dank dankeschon dankesehr gracias grazie obrigado
    parfait parfaite perfect perfekt perfecto perfetto
    super genial geniale great awesome amazing excellent excellente excelente
    eccellente wonderful wunderbar magnifique magnifico fantastic fantastique
    fantastico fantastisch brilliant bravo bravissimo chouette nickel impeccable
    top cool nice sympa toll klasse prima spitze ottimo estupendo stupendo
    bellissimo bellissima precioso preciosa formidable incroyable incredible
    increible incredibile adore love encanta adoro gorgeous lovely beautiful
    sublime superbe superb splendide splendid wow waouh ouah youpi yay
    good gut bien bene bueno classe
    """.split()
)

# Words that may surround the thanks: degree, the delivered thing, small words.
_FILLER = frozenset(
    """
    beaucoup bcp mille fois infiniment vraiment trop tres bien vachement grave
    hyper ultra franchement
    c est cest ca sa ce cette cet cela ceci il elle ils tout exactement pile poil
    que qu je j me m moi t te toi tu vous v ton ta tes votre vos mon ma mes u
    bonne journee soiree nuit weekend have day night buen dia buena tarde
    schonen tag abend buona giornata serata
    le la les l un une des du de d pour au aux et a
    marche fonctionne nickel joli jolie jolis beau belle beaux sympa reussi
    voulais voulait fallait cherchais attendais besoin
    so much very many lot lots a the for it its this that thats is s
    looks look looked works worked working job work just what i needed wanted
    exactly you all really truly totally absolutely indeed
    vielen viel vielmals herzlichen herzlich sehr das ist es sieht gut aus fur
    die der den dem
    funktioniert genau passt wirklich echt
    muchas muchisimas mil esta es muy bien funciona justo lo que necesitaba
    queria todo genial de verdad me
    tante mille molto e funziona proprio quello che volevo davvero tutto
    va benissimo bene
    jeu game spiel juego gioco app appli application applis apps outil tool
    dessin drawing dibujo disegno zeichnung image images picture bild immagine
    imagen illustration logo fichier file datei archivo document documento
    dokument script code page site tableau quiz timer minuteur calculatrice
    calculator version resultat result rendu travail boulot
    """.split()
)

# A file format thanked for ("merci pour le pdf", "the excel file"), only after
# a determiner or a file noun: "perfect, pdf" asks for a format.
_FORMATS = frozenset(
    """
    pdf word excel powerpoint docx xlsx pptx csv presentation slides diaporama
    tableur spreadsheet prasentation presentacion presentazione
    """.split()
)
_DETERMINERS = frozenset(
    """
    le la les l ce cette ton ta tes votre mon the this that your my for pour
    das die den dem der dein deine el los las tu este esta il lo gli questo
    fichier file document datei dokument archivo documento
    """.split()
)

# Emojis that thank or praise; a message of emojis alone is a closing when one
# of them is here.
_EMOJI = frozenset("👍👌🙏❤🧡💛💚💙💜🤍😍🥰😊🙂😀😃😄😁🤩👏🎉✨💯🔥😘🤗🥳💪✅⭐🌟")

# The thanks and praise words most often mistyped, checked for one typo; real
# words one letter away from them are not thanks.
_TYPO_MARKERS = (
    "merci",
    "thanks",
    "gracias",
    "grazie",
    "parfait",
    "perfect",
    "perfekt",
    "perfecto",
    "perfetto",
    "excellent",
    "awesome",
    "amazing",
    "magnifique",
    "fantastic",
    "fantastique",
    "wunderbar",
    "dankeschon",
)
_NOT_TYPOS = frozenset({"think", "thinks", "thanos", "perfects"})

_WORD = re.compile(r"[a-z0-9]+")
_REPEATS = re.compile(r"(.)\1+")


def polite_closing(text: str) -> bool:
    """A short thanks or compliment that asks for nothing more."""
    if not isinstance(text, str) or "?" in text:
        return False
    normalized = _normalized(text)
    words = _WORD.findall(normalized.replace("'", " ").replace("’", " "))
    if not words:
        return any(character in _EMOJI for character in text)
    if len(words) > MAX_WORDS:
        return False
    marker = False
    for index, word in enumerate(words):
        squeezed = _REPEATS.sub(r"\1", word)
        if squeezed in _SQUEEZED_MARKERS or _near_marker(squeezed):
            marker = True
        elif word in _FORMATS:
            if index == 0 or words[index - 1] not in _DETERMINERS:
                return False
        elif squeezed not in _SQUEEZED_FILLER:
            return False
    return marker


def _near_marker(word: str) -> bool:
    """One typo (a letter added, missing, changed, or two swapped) on a long marker."""
    if len(word) < 4 or word in _NOT_TYPOS:
        return False
    return any(
        abs(len(word) - len(marker)) <= 1 and _one_edit(word, marker)
        for marker in _TYPO_SQUEEZED
    )


def _one_edit(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return (
            len(diff) == 2
            and diff[1] == diff[0] + 1
            and a[diff[0]] == b[diff[1]]
            and a[diff[1]] == b[diff[0]]
        )
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(longer)):
        if longer[:i] + longer[i + 1 :] == shorter:
            return True
    return False


def _normalized(text: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(character)
    )


def _squeeze(words: frozenset[str]) -> frozenset[str]:
    return frozenset(_REPEATS.sub(r"\1", word) for word in words)


_SQUEEZED_MARKERS = _squeeze(_MARKERS)
_SQUEEZED_FILLER = _squeeze(_FILLER) - _SQUEEZED_MARKERS
_TYPO_SQUEEZED = tuple(_REPEATS.sub(r"\1", word) for word in _TYPO_MARKERS)
