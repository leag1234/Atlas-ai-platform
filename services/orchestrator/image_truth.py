"""Pipeline-owned image policy facts and checks against unsupported refusals."""

import re
from packages import image_limits
from packages.imagegen import ImagePrompt

_IMAGE_TOPIC = re.compile(
    r"\b(?:images?|pictures?|photos?|drawings?|illustrations?|dessin\w*|"
    r"bild\w*|fotos?|imagen(?:es)?|immagin[ei])\b",
    re.IGNORECASE,
)
_BLOCK = re.compile(
    r"\b(?:filter\w*|sicherheitsfilter\w*|filtr\w*|policy|polic\w*|politique|richtlinie\w*|"
    r"safety|s[eé]curit[eé]|sicherheit|seguridad|sicurezza|technical|technique|"
    r"technisch\w*|t[eé]cnic\w*|tecnic\w*)\b.{0,180}"
    r"\b(?:block\w*|bloqu\w*|prohibit\w*|forbid\w*|prevent\w*|interdi\w*|"
    r"emp[eê]ch\w*|verbiet\w*|verbot\w*|proh[ií]b\w*|impid\w*|viet\w*|impedis\w*)\b|"
    r"\b(?:cannot|can't|unable|ne peux pas|impossible|kann.{0,15}nicht|"
    r"no puedo|non posso)\b.{0,160}"
    r"\b(?:filter\w*|sicherheitsfilter\w*|filtr\w*|policy|polic\w*|politique|richtlinie\w*|"
    r"safety|s[eé]curit[eé]|sicherheit|seguridad|sicurezza|technical|technique|technisch\w*|t[eé]cnic\w*|tecnic\w*)\b",
    re.IGNORECASE,
)
_NEGATED_BLOCK = re.compile(
    r"\b(?:no other|no additional|does not|do not|don't|doesn't|aucun|"
    r"ne bloque pas|ne bloquent pas|keine?|nicht|no hay|no bloquea|nessun\w*|non blocca)\b",
    re.IGNORECASE,
)


def image_topic(text: str) -> bool:
    return bool(_IMAGE_TOPIC.search(text))


STANDARD_SCOPE = {
    "en": "At the standard level, childbirth, artistic or medical nudity and religious scenes such as the Nativity are allowed within the configured limits.",
    "fr": "Au niveau standard, l’accouchement, la nudité artistique ou médicale et les scènes religieuses comme la Nativité sont autorisés dans les limites configurées.",
    "de": "Auf Standardstufe sind Geburt, künstlerische oder medizinische Nacktheit und religiöse Szenen wie die Geburt Christi innerhalb der konfigurierten Grenzen erlaubt.",
    "es": "En el nivel estándar, el parto, la desnudez artística o médica y las escenas religiosas como la Natividad están permitidos dentro de los límites configurados.",
    "it": "Al livello standard, il parto, la nudità artistica o medica e le scene religiose come la Natività sono consentiti entro i limiti configurati.",
}


def policy_statement(language: str = "en") -> str:
    level = image_limits.configured_policy().image_safety_level
    introductions = {
        "en": f"Image safety level: {level}. The image model applies no additional safety filter. The configured limits are:",
        "fr": f"Niveau de sécurité des images : {level}. Le modèle d’image n’applique aucun filtre de sécurité supplémentaire. Les limites configurées sont :",
        "de": f"Bildsicherheitsstufe: {level}. Das Bildmodell wendet keinen zusätzlichen Sicherheitsfilter an. Die konfigurierten Grenzen sind:",
        "es": f"Nivel de seguridad de imágenes: {level}. El modelo de imágenes no aplica ningún filtro de seguridad adicional. Los límites configurados son:",
        "it": f"Livello di sicurezza delle immagini: {level}. Il modello di immagini non applica filtri di sicurezza aggiuntivi. I limiti configurati sono:",
    }
    categories = ["minors", "real_people", "incitement"]
    if level == "strict":
        categories.extend(("strict_nudity", "strict_gore"))
    return (
        introductions.get(language, introductions["en"])
        + "\n"
        + "\n".join(image_limits.refusal(category, language) for category in categories)
        + "\n"
        + STANDARD_SCOPE.get(language, STANDARD_SCOPE["en"])
    )


def short_correction(language: str = "en") -> str:
    """A user-facing correction of an invented block; the full policy stays in system text."""
    sentences = {
        "en": "This subject is not blocked on this instance: I can generate the image if you want.",
        "fr": "Ce sujet n’est pas bloqué sur cette instance : je peux générer l’image si vous le souhaitez.",
        "de": "Dieses Thema ist auf dieser Instanz nicht gesperrt: Ich kann das Bild erzeugen, wenn Sie möchten.",
        "es": "Este tema no está bloqueado en esta instancia: puedo generar la imagen si lo desea.",
        "it": "Questo soggetto non è bloccato su questa istanza: posso generare l’immagine se lo desidera.",
    }
    return sentences.get(language, sentences["en"])


def policy_instruction() -> str:
    return (
        "\n\nRunning instance image policy (application-owned):\n"
        + policy_statement()
        + "\nDo not invent image-tool filters, provider policies, or technical "
        "blocks. Only report a transport failure when the tool actually returns "
        "one. For an allowed image request, call generate_image; a correction "
        "must retain the original scene and change only what the user requests."
    )


def unsupported_image_block(answer: str, request: str) -> bool:
    # Never override a real configured refusal or an invalid image request.
    try:
        ImagePrompt(prompt=request)
    except ValueError:
        return False
    for sentence in re.split(r"[.!?\n]+", answer):
        if _BLOCK.search(sentence) and not _NEGATED_BLOCK.search(sentence):
            return True
    return False


def wants_generation(text: str) -> bool:
    from services.orchestrator.followup import _IMAGE_EXPLANATION

    return bool(
        not _IMAGE_EXPLANATION.search(text)
        and image_topic(text)
        and re.search(
            r"\b(?:paint|render|depict|show|want|would like|peins|montre|voudrais|"
            r"zeige|mal\w*|quiero|muestra|pinta|vorrei|mostra|dipingi)\b",
            text,
            re.IGNORECASE,
        )
    )


def _balanced_end(text: str, start: int, opening: str, closing: str) -> int | None:
    depth, pos = 1, start + 1
    while pos < len(text):
        char = text[pos]
        if char == "\\":
            pos += 2
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return pos + 1
        pos += 1
    return None


def _target(value: str) -> str:
    value = value.strip()
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")]
    return value.split()[0] if value else ""


class ImageVerifier:
    """Buffer incomplete Markdown tokens across chunks; never discard literal text."""

    def __init__(self) -> None:
        self.pending = ""
        self.source = ""
        self.line_prefix = ""

    def feed(self, text: str, references: set[str], *, final: bool = False) -> str:
        self.source += text
        self.pending += text
        text = self.pending
        output: list[str] = []
        pos = 0
        while pos < len(text):
            start, end, keep = pos, pos + 1, True
            if text[pos] == "\\":
                if pos + 1 == len(text) and not final:
                    break
                end = min(pos + 2, len(text))
            elif text[pos] in "`~":
                run = re.match(r"`+|~+", text[pos:])
                assert run is not None
                marker = run[0]
                if pos + len(marker) == len(text) and not final:
                    break
                fenced = len(marker) >= 3 and bool(
                    re.fullmatch(r" {0,3}", self.line_prefix)
                )
                if fenced:
                    close = re.search(
                        r"(?m)^ {0,3}"
                        + re.escape(marker[0])
                        + "{"
                        + str(len(marker))
                        + r",}[ \t]*(?:\n|$)",
                        text[pos + len(marker) :],
                    )
                    if close is None:
                        if not final:
                            break
                        end = len(text)
                    else:
                        end = pos + len(marker) + close.end()
                elif marker[0] == "`":
                    close = re.search(
                        r"(?<!`)" + marker + r"(?!`)", text[pos + len(marker) :]
                    )
                    if close is None and not final:
                        break
                    end = (
                        pos + len(marker) + close.end() if close else pos + len(marker)
                    )
                else:
                    end = pos + len(marker)
            elif text[pos] == "!":
                if pos + 1 == len(text) and not final:
                    break
                if text[pos : pos + 2] == "![":
                    alt_end = _balanced_end(text, pos + 1, "[", "]")
                    if alt_end is None or alt_end == len(text):
                        if not final:
                            break
                    elif text[alt_end] == "(":
                        image_end = _balanced_end(text, alt_end, "(", ")")
                        if image_end is None:
                            if not final:
                                break
                        else:
                            end = image_end
                            keep = _target(text[alt_end + 1 : end - 1]) in references
                    else:
                        # Definitions may follow the image, so resolve at EOF.
                        if not final:
                            break
                        label = text[pos + 2 : alt_end - 1]
                        ref_end = alt_end
                        if text[alt_end : alt_end + 1] == "[":
                            found = _balanced_end(text, alt_end, "[", "]")
                            if found is not None:
                                label = text[alt_end + 1 : found - 1] or label
                                ref_end = found
                        definition = re.search(
                            r"(?im)^ {0,3}\[" + re.escape(label) + r"\]:[ \t]*(.+)$",
                            self.source,
                        )
                        if definition:
                            end, keep = ref_end, _target(definition[1]) in references
            part = text[start:end]
            if keep:
                output.append(part)
            self.line_prefix = (self.line_prefix + part).rsplit("\n", 1)[-1]
            pos = end
        self.pending = text[pos:]
        return "".join(output)


def verified_images(text: str, references: set[str]) -> str:
    """Remove only complete Markdown images without a tool-produced reference."""
    return ImageVerifier().feed(text, references, final=True)
