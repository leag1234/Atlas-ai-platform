"""Classification of plain-chat turns that need Work (build/run code) behind them."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

_ATTACHMENT_TAG = re.compile(
    r"\[attachment: [^\n]*\]|<attached_files>.*?</attached_files>", re.S | re.M
)

# Requests to build, run, test or process with code, or to use a machine.
_WORK_SIGNALS = re.compile(
    r"\b(?:"
    # run / execute code or tests
    r"run|execute|executes?|lance|lancer|execute|lanzar|ausfuehren|ausfuehren|esegui"
    r"|test|tests|testing|pytest|unittest|unit ?tests?|testes? unitaires?"
    r"|debug|debugge|depane|depannage|arregla|corrige le bug|bugfix"
    r"|compile|compiler|build|builde|construis|construire|builds?"
    # write code / scripts / apps beyond a small single-file artifact
    r"|code|codage|programme|program|programmer|programmi|programa|programar"
    r"|script|scripts|schreibe|erstelle|escribe|scrivi"
    r"|function|fonction|fonctionne|def |class |api|endpoint"
    r"|app|application|website|site web|web app|webapp|backend|frontend|fullstack"
    r"|game|jeu|spiel|juego|gioco|snake|tetris|pong|demineur|morpion|sudoku|2048"
    r"|drum ?machine|boite a rythmes|step sequencer|synthetiseur|synthesizer|synthétiseur"
    r"|simulateur|simulator|simulador|simulatore"
    r"|algorithm|algorithme|algo|algoritmo|algoritmo"
    r"|scrape|scraper|scraping|web ?scraping|crawl|crawler"
    r"|automate|automation|automatise|automatiza|automatisier"
    r"|refactor|refactorise|refactoriser|refactored"
    r"|migrate|migration|migra"
    r"|deploy|deploie|deploiement|deployement|despliega|bereitstellen"
    r"|install|installe|installieren|instalar|installa"
    r"|pip3?|npm|npx|yarn|pnpm|nodejs|node |cargo|rustc|go build|javac|mvn|gradle"
    r"|python\d*(?:\.\d+)?|bash|shell|zsh|powershell|terminal|command line|ligne de commande"
    r"|sql|query|requete|abfrage"
    r"|data ?process|processe|traite|analys[ei]|analyse|analyser|analisis|analyse de donnees"
    r"|dataset|dataframe|pandas|numpy|machine learning|ml|model training|entraîne|entraine"
    r"|docker|container|kubernetes|k8s|vm|virtual machine|machine virtuelle"
    r"|server|serveur|host|heberger|hoste"
    r"|cli|tool|outil|plugin|extension|add-on|addon"
    r"|bot|chatbot|discord|telegram|slack|whatsapp"
    r"|api rest|rest api|graphql|websocket"
    r"|git|repo|repository|commit|pull request|merge"
    r"|ci|cd|pipeline|github actions|gitlab ci"
    r"|database|base de donnees|datenbank|banco de datos"
    r"|registered machine|machine enregistrée|enregistree|build server|serveur de build"
    r"|remote|target|runner|machine distante"
    r")\b"
)

# Questions, explanations, writing, translation: never Work.
_EXPLAIN_SIGNALS = re.compile(
    r"\b(?:"
    r"explique|explain|expliquer|erklare|erklären|explica|spiega|spiegare"
    r"|comment (?:faire|marche|fonctionne)|how (?:does|do|to)|pourquoi|why|was ist|was sind"
    r"|quelle est|quel est|what is|what are|defin[ei]|definition"
    r"|compare|comparer|vergleich|compara|confronta|vs\.?|versus"
    r"|résume|resume|résumer|summarize|resumir|zusammenfassen|riassumi"
    r"|traduis|translate|tradurre|übersetze|traduce|traduci"
    r"|écris un (?:texte|essai|poème|histoire|article|mail|email|lettre)"
    r"|write a (?:text|essay|poem|story|article|email|letter|blog)"
    r"|rédige|rédiger|redige|verfasse|scrivi un|escribe un"
    r")\b"
)

_QUESTION = re.compile(
    r"^\s*(?:can you|could you|please|peux[- ]tu|pouvez[- ]vous|kannst du|puedes|potresti|können sie"
    r"|why|pourquoi|warum|porque|perche|what|quoi|was|que|wie|comment|how)\b|\?\s*$"
)

# A question about code is an explanation, not a build request.
_CODE_EXPLAIN = re.compile(
    r"\b(?:explique|explain|erkläre|explica|spiega|comment|how|pourquoi|why|was|difference|compare|comparer|vergleich|compara|confronta|vs|versus)\b"
    r".{0,60}\b(?:python|javascript|java|rust|go|sql|code|function|fonction|class|api|git|html|css)\b"
    r"|\b(?:python|javascript|java|rust|sql|code|function|fonction|class|api|git|html|css)\b"
    r".{0,60}\b(?:explique|explain|erkläre|explica|spiega|comment|how|pourquoi|why|was|difference|compare|comparer|vergleich|compara|confronta|vs|versus)\b"
)

# Small single-file web artifacts already handled by the artifact panel.
_SMALL_ARTIFACT = re.compile(
    r"\b(?:html|css|javascript|js|svg|canvas|page web|web page|landing page|single file"
    r"|fichier unique|eine datei|un seul fichier|una pagina|eine seite)\b"
)

# Apps beyond a small single HTML artifact: games, machines, bots, servers.
_APP_BEYOND_ARTIFACT = re.compile(
    r"\b(?:app|application|game|jeu|spiel|juego|gioco|snake|tetris|pong|demineur"
    r"|morpion|sudoku|2048|drum ?machine|boite a rythmes|step sequencer"
    r"|synthetiseur|synthesizer|bot|chatbot|discord|telegram|server|serveur"
    r"|api|backend|website|site web|web app|webapp|fullstack|cli|tool|outil"
    r"|plugin|extension|simulateur|simulator|automate|automation|scrape|scraper"
    r"|crawl|crawler|dashboard|tableau de bord)\b"
)

_NON_WEB = re.compile(
    r"\b(?:python|pygame|tkinter|java|kotlin|swift|c\+\+|c#|rust|golang|go"
    r"|unity|godot|android|ios|electron|flask|django|node server|backend)\b"
)

_OFFICE = re.compile(
    r"\b(?:presentation|prasentation|presentacion|presentazione"
    r"|slides?|diapositives?|folien|diapositivas?|diapositive"
    r"|powerpoint|pptx|word|docx|excel|xlsx|spreadsheet|tableur"
    r"|tabellenkalkulation|hoja de calculo|foglio di calcolo|pdf)\b"
)


def _normalized(text: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(character)
    )


def needs_work(text: str, history: Sequence[Mapping[str, object]] = ()) -> bool:
    """True when a plain-chat turn needs Work: build/run code, an app beyond a
    small single HTML artifact, tests, scripts, data processing with code, or a
    registered machine. False for questions, explanations, writing, translation
    and small SVG/HTML apps already handled by the artifact panel."""
    plain = _ATTACHMENT_TAG.sub(" ", text)
    normalized = _normalized(plain)
    if not normalized.strip():
        return False
    if _EXPLAIN_SIGNALS.search(normalized) and not _WORK_SIGNALS.search(normalized):
        return False
    if _CODE_EXPLAIN.search(normalized):
        return False
    if _QUESTION.search(normalized) and not _WORK_SIGNALS.search(normalized):
        return False
    if _OFFICE.search(normalized) and not _WORK_SIGNALS.search(normalized):
        return False
    if not _WORK_SIGNALS.search(normalized):
        return False
    # A small single-file HTML/SVG app stays in the artifact panel, unless the
    # request is for a real app (game, drum machine, bot, server) or names a
    # non-web runtime.
    if _SMALL_ARTIFACT.search(normalized) and not _NON_WEB.search(normalized):
        if not _APP_BEYOND_ARTIFACT.search(normalized):
            return False
    return True
