"""Plain writing requests: a text to read in the chat, not an app or a file.

A short text to write (a message, an email, a letter, a poem, a post, a reply,
a summary, a list) is answered in the chat (owner decisions 2026-11-06 and
2026-11-11: the chat never switches to an app or a project by itself, and a
document is produced when the person asks for a file or a format). On
staging (2026-11-16) "write a short message (5 sentences) to ..." opened the
app panel: the lexical gate counted any "write" or "make" as an app request,
and the model router's text called a letter to send a document.

The signal is read from the words the person used, in any of the supported
languages: the request names a kind of text, and names nothing that only an
app, a picture, code or a file can be (an interactive word, a game, a page,
a format, a file, a drawing, a program). When anything of that kind is
named, this module says nothing and the other gates decide.
"""

from __future__ import annotations

import re
import unicodedata

# Kinds of text, with the usual typos and plurals (normalized: no accents).
_TEXT_KIND = re.compile(
    r"\b(?:"
    # message, note, letter, email
    r"messages?|messaggi?o?|mensajes?|mensagens?|nachricht(?:en)?|sms|textes?|textos?|texts?"
    r"|e-?mails?|mails?|courriels?|correos?(?: electronicos?)?|mel"
    r"|mots?|notes?|notiz|nota|biglietto|petit mot"
    r"|lettres?|letters?|briefe?s?|cartas?|lettera|lettere|courriers?"
    r"|invitations?|einladung\w*|invitaciones?|invitacion|invito"
    r"|remerciements?|thank[- ]you|dankes\w*|agradecimientos?|ringraziament\w*"
    r"|excuses|apology|apologies|entschuldigung|disculpas?|scuse"
    r"|felicitations|congratulations|gluckwunsch\w*|felicitaciones|auguri|voeux|wishes"
    # poem, song lyrics, story, speech
    r"|poemes?|poems?|poesies?|poetry|gedichte?|poemas?|poesia|poesie|haikus?|sonnets?"
    r"|vers|verses|strophes?|stanzas?|zeilen|versos|versi|rimes?|rhymes?"
    r"|paroles|lyrics|liedtext|letra|chanson|song"
    r"|histoires?|story|stories|conte|contes|tale|geschichte|marchen|cuento|racconto|storia"
    r"|discours|speech|speeches|rede|discurso|discorso|toasts?|brindis|brindisi|trinkspruch"
    # post, reply, review, bio, slogan
    r"|posts?|tweets?|threads?|publication|beitrag|publicacion|statut|status update"
    r"|reponses?|reply|replies|response|antworte?n?|respuestas?|rispost[ae]"
    r"|avis|review|bewertung|resena|recensione"
    r"|bio|biographie|biography|accroche|pitch|slogans?|taglines?|devise|legendes?|captions?"
    r"|paragraphes?|paragraphs?|absatze?|absatz|parrafos?|paragrafo|phrases?|sentences?|satze?"
    r"|resumes?|summary|summaries|zusammenfassung|resumen|riassunto|synthese"
    r"|listes?|lists?|liste|lista|bullet points?|puces"
    r"|idees|ideas|ideen|idee"
    r")\b"
)

# Verbs whose result is a text even without a named kind ("résume ce paragraphe").
_TEXT_VERB = re.compile(
    r"\b(?:resume[sz]?|resumer|summari[sz]e|zusammenfass\w*|fasse\w*|resum[ae]\w*|riassum\w*"
    r"|reponds|repondre|reply|answer|antworte|responde|rispondi"
    r"|reformule\w*|rephrase|paraphrase\w*|umformulier\w*|reescrib\w*|riformul\w*"
    r"|traduis|traduire|translate|ubersetze?|traduce|traduci)\b"
)

# Anything only an app, a picture, code or a file can be: the other gates decide.
_NOT_PLAIN = re.compile(
    r"\b(?:"
    # interactive things and apps
    r"apps?|appli\w*|application|applications|outils?|tools?|werkzeug|herramienta|strumento"
    r"|jeux?|games?|spiel\w*|juegos?|gioc\w*|quiz\w*|flash ?cards?|karteikart\w*"
    r"|interacti\w*|interakti\w*|cliquab\w*|click\w*|klick\w*|clic\w*|jouable|playable"
    r"|anim\w*|bouton\w*|buttons?|knopf\w*|boton\w*|widget\w*|formulaires?|forms?|formular\w*"
    r"|generat(?:eur|or|ore)s?|generador\w*|compteur|counter|zahler|timer|minuteur|chrono\w*"
    r"|calculat\w*|rechner|convert\w*|simulat\w*|dashboard|tableau de bord|todo"
    r"|sites?|website|webseite|page|pages|pagina|seite|landing|html|css|javascript|canvas"
    # code and programs
    r"|code|script|scripts|python|programme?s?|program\w*|fonction|function|funktion|funcion"
    r"|sql|regex|api|bot|plugin|extension"
    # pictures
    r"|dessin\w*|dessine\w*|draw\w*|zeichn\w*|dibuj\w*|disegn\w*|svg|images?|imagen|immagine"
    r"|bild|pictures?|photos?|fotos?|illustr\w*|logos?|icones?|icons?|affiches?|posters?"
    r"|plakat|cartel|cartes?|cards?|karten?|tarjetas?|schema|diagram\w*"
    # files and formats
    r"|fichiers?|files?|datei\w*|archivos?|documents?|dokument\w*|documentos?"
    r"|pdf|docx?|word|odt|rtf|txt|excel|xlsx?|csv|markdown|md|powerpoint|pptx?"
    r"|tableur|spreadsheet|tabelle|hoja de calculo|foglio|slides?|diapo\w*|deck|folien"
    r"|presentation|prasentation|presentacion|presentazione"
    r"|telecharg\w*|download\w*|herunterlad\w*|descarg\w*|scaric\w*"
    r"|imprim\w*|print\w*|druck\w*|stamp\w*"
    # tables and tracking sheets (a spreadsheet when the model says so), a CV
    r"|tableaux?|tables?|tablas?|tabell\w*|tracker|budgets?|calendriers?|calendars?"
    r"|kalender|calendarios?|planning|cv"
    r")\b"
)


# Quoted material is the text to work on ("reply to this review: '...'"), not
# what the request asks for.
_QUOTED = re.compile(r"«[^»]*»|“[^”]*”|„[^“”]*[“”]|\"[^\"]*\"")
# A text that introduces someone ("texte de présentation") is not a slide deck.
_SELF_INTRODUCTION = re.compile(
    r"\b(?:textes?|mots?|lettres?|messages?|e-?mails?|mails?|courriels?|paragraphes?)"
    r" de presentation\b"
)
# What follows a colon or a line break after the request ("summarize this:",
# "reply to this review:", "a message to my landlord:") is pasted material.
_PASTED = re.compile(r"[:\n]")


def plain_writing(text: str) -> bool:
    """The request asks for a text to read in the chat, and for nothing else."""
    normalized = _request(_normalized(text))
    if not normalized.strip():
        return False
    if _NOT_PLAIN.search(_SELF_INTRODUCTION.sub(" texte ", normalized)):
        return False
    return bool(_TEXT_KIND.search(normalized) or _TEXT_VERB.search(normalized))


def _request(normalized: str) -> str:
    """The words of the request, without the material it works on."""
    request = _QUOTED.sub(" ", normalized)
    head = _PASTED.split(request, maxsplit=1)[0]
    if head != request and (_TEXT_VERB.search(head) or _TEXT_KIND.search(head)):
        return head
    return request


def _normalized(text: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(character)
    )
