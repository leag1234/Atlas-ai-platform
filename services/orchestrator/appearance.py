"""Lexical routing and rendering for physical-object illustration requests."""

import logging
import math
import re
import unicodedata
from xml.etree import ElementTree
from packages.imagegen import positive_image_prompt


_PROCESS_OR_COMPARISON = re.compile(
    r"\b(?:process|workflow|sequence|architecture|data flow|compare|comparison|"
    r"processus|flux de donnees|comparer|vergleich|ablauf|prozess|arquitectura|"
    r"comparacion|proceso|confronto|processo|architettura)\b",
    re.IGNORECASE,
)
_APPEARANCE = re.compile(
    r"\b(?:what does .+ look like|how is .+ (?:physically )?built|exploded view|"
    r"cutaway view|parts of|a quoi ca ressemble|les pieces|ce qui tourne|"
    r"vue eclatee|wie sieht .+ aus|teile von|was dreht sich|teile.*drehen|explosions(?:ansicht|zeichnung)|"
    r"como es por dentro|piezas de|que gira|vista explosionada|com[' ]e fatto|"
    r"pezzi di|cosa gira|pezzi.*girano|vista esplosa)\b",
    re.IGNORECASE,
)
_DRAWING_REQUEST = re.compile(
    r"^\s*(?P<verb>dessine(?:-moi)?|dessiner|draw|sketch|zeichne|dibuja|disegna)\b\s+.+",
    re.IGNORECASE,
)
_DRAWING_EXCLUSIONS = re.compile(
    r"\b(?:process|processus|prozess|proceso|processo|schema|sch[eé]ma|diagram|"
    r"chart|graph|flowchart|workflow|cycle|cycle de|ablauf|diagram\w*|"
    r"image|picture|photo|illustration|bild|foto|imagen|ilustracion|immagine|illustrazione)\b",
    re.IGNORECASE,
)


def _normalized(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", value).lower()
        if not unicodedata.combining(char)
    )


def is_appearance_request(question: str) -> bool:
    """Return true only for a request to see a physical object or its mechanics."""
    normalized = _normalized(question)
    return not bool(_PROCESS_OR_COMPARISON.search(normalized)) and bool(
        _APPEARANCE.search(normalized)
    )


def is_simple_drawing_request(question: str) -> bool:
    """Recognize a direct request for a small illustration, never a diagram or process."""
    normalized = _normalized(question)
    return bool(_DRAWING_REQUEST.match(normalized)) and not bool(
        _DRAWING_EXCLUSIONS.search(normalized)
    )


def simple_drawing_language(question: str, fallback: str) -> str:
    """Use the explicit drawing verb when a short request otherwise inherits the UI locale."""
    match = _DRAWING_REQUEST.match(_normalized(question))
    if match is None:
        return fallback
    return {
        "dessine": "fr",
        "dessine-moi": "fr",
        "dessiner": "fr",
        "draw": "en",
        "sketch": "en",
        "zeichne": "de",
        "dibuja": "es",
        "disegna": "it",
    }[match.group("verb")]


def legend_instruction() -> str:
    """Constrain the text-model half of an appearance response without a prompt edit."""
    return (
        "\n\nAppearance route: provide only a concise numbered legend for the physical "
        "object. For every listed part, state its role and whether it is stationary, "
        "rotates, or oscillates. Do not use Mermaid, ASCII art, code blocks, or a "
        "diagram. Do not name parts you cannot support from your own answer."
    )


def mechanical_svg_instruction(*, include_svg: bool = True) -> str:
    """Ask the text model for a bounded drawing; prompts are intentionally untouched."""
    if not include_svg:
        return (
            "\n\nMechanical view route: provide a concise explanation, followed by "
            "a numbered legend: part — role — rotates, oscillates, or fixed. "
            "Write the explanation, headings, part names and roles in the requested "
            "answer language. Include one invisible metadata comment "
            "<!-- commons-query: object name --> with only the short English name "
            "of the physical object for an image search. The service retrieves the "
            "drawing; do not generate SVG, Mermaid, ASCII art, or code blocks."
        )
    return (
        "\n\nMechanical drawing route: begin with one or two short explanatory "
        "sentences, then provide exactly one SVG technical side or cutaway drawing. "
        "Draw one side or cutaway view only: use recognisable simple part shapes in "
        "plausible proportions and positions, with no decorative or unexplained "
        'elements. Use viewBox="0 0 800 500" and fill that canvas. Use strokes of at '
        "least 2 and text of at least 16. Every numbered callout must be outside the "
        "part it names, connected by a thin leader line; callouts must not overlap one "
        "another or any part outline. "
        "The SVG must have a viewBox, at most 120 elements and use only rect, circle, "
        "ellipse, line, polyline, path, text, g, title, and desc. "
        "Use only these attributes: " + ", ".join(sorted(_COMMON_ATTRS)) + ". "
        "The root may also have xmlns, viewBox, role and aria-label. No style, "
        "markers, defs, references, events or other attributes. Use simple paths "
        "for arrows. Use numbered callouts in the drawing. After the SVG provide "
        "a numbered legend: part — role — rotates, oscillates, or fixed. "
        "Write all visible text in the requested answer language. Before the SVG include "
        "one invisible metadata comment <!-- commons-query: object name --> with "
        "only the short English name of the physical object, for an image search. "
        "Never use Mermaid, ASCII art, or a fenced code block."
    )


def simple_drawing_instruction() -> str:
    """Constrain the guest drawing response without changing hashed prompt artifacts."""
    return (
        "\n\nSimple drawing route: reply with one short, friendly sentence in the "
        "user's language, then exactly one charming SVG illustration of the requested "
        "subject. Use flat colours, large simple shapes, and a background. Use "
        'viewBox="0 0 400 300" and fill that canvas. Do not include text unless the '
        "user requests it. Do not use Mermaid, ASCII art, code blocks, a legend, "
        "callouts, web research, links, or tool calls. The SVG may use only rect, "
        "circle, ellipse, line, polyline, path, text, g, title, and desc, with only "
        "the allowed SVG attributes already supplied by this service."
    )


_SVG = re.compile(r"<svg\b[^>]*>.*?</svg\s*>", re.I | re.S)
_ALLOWED_TAGS = {
    "svg",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "path",
    "text",
    "g",
    "title",
    "desc",
}
_COMMON_ATTRS = {
    "fill",
    "stroke",
    "stroke-width",
    "opacity",
    "transform",
    "font-size",
    "font-family",
    "text-anchor",
    "x",
    "y",
    "x1",
    "x2",
    "y1",
    "y2",
    "cx",
    "cy",
    "r",
    "rx",
    "ry",
    "width",
    "height",
    "points",
    "d",
}


_LOGGER = logging.getLogger(__name__)
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_COMMONS_QUERY = re.compile(
    r"<!--\s*commons-query:\s*([A-Za-z0-9 -]{2,80})\s*-->", re.I
)
_FIXED_VIEWBOX = (0.0, 0.0, 800.0, 500.0)
_MIN_STROKE = 2.0
_MIN_TEXT = 16.0


def drawing_subject(model_text: str) -> str:
    """Read bounded search metadata, never send a whole conversation to Commons."""
    match = _COMMONS_QUERY.search(model_text)
    return " ".join(match.group(1).split()) if match else ""


def _number(value: str | None, default: float = 0.0) -> float:
    try:
        parsed = float(value if value is not None else default)
    except ValueError:
        return default
    return parsed if math.isfinite(parsed) else default


def _points_bounds(points: str) -> tuple[float, float, float, float] | None:
    values = re.findall(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", points)
    if len(values) < 2:
        return None
    coords = [float(value) for value in values]
    xs, ys = coords[::2], coords[1::2]
    if not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _element_bounds(
    element: ElementTree.Element,
) -> tuple[float, float, float, float] | None:
    """Return conservative primitive bounds; unsupported geometry is left intact."""
    tag = element.tag
    if tag == "rect":
        x, y = _number(element.get("x")), _number(element.get("y"))
        return (
            x,
            y,
            x + _number(element.get("width")),
            y + _number(element.get("height")),
        )
    if tag == "circle":
        x, y, radius = (
            _number(element.get("cx")),
            _number(element.get("cy")),
            _number(element.get("r")),
        )
        return x - radius, y - radius, x + radius, y + radius
    if tag == "ellipse":
        x, y = _number(element.get("cx")), _number(element.get("cy"))
        return (
            x - _number(element.get("rx")),
            y - _number(element.get("ry")),
            x + _number(element.get("rx")),
            y + _number(element.get("ry")),
        )
    if tag == "line":
        xs = (_number(element.get("x1")), _number(element.get("x2")))
        ys = (_number(element.get("y1")), _number(element.get("y2")))
        return min(xs), min(ys), max(xs), max(ys)
    if tag in {"polyline", "path"}:
        return _points_bounds(
            element.get("points", "") if tag == "polyline" else element.get("d", "")
        )
    if tag == "text":
        x, y = _number(element.get("x")), _number(element.get("y"))
        size = max(_MIN_TEXT, _number(element.get("font-size"), _MIN_TEXT))
        width = max(size, len((element.text or "").strip()) * size * 0.6)
        if element.get("text-anchor") == "middle":
            return x - width / 2, y - size, x + width / 2, y
        return x, y - size, x + width, y
    return None


def _outside(
    bounds: tuple[float, float, float, float], canvas: tuple[float, float, float, float]
) -> bool:
    left, top, right, bottom = bounds
    _, _, width, height = canvas
    return right < 0 or bottom < 0 or left > width or top > height


def _translate_callout(element: ElementTree.Element, dx: float, dy: float) -> None:
    for x, y in (("x", "y"), ("cx", "cy")):
        if x in element.attrib:
            element.set(x, str(round(_number(element.get(x)) + dx, 2)))
        if y in element.attrib:
            element.set(y, str(round(_number(element.get(y)) + dy, 2)))


def _callout_numbers(root: ElementTree.Element) -> set[str]:
    return {
        (element.text or "").strip()
        for element in root.iter("text")
        if re.fullmatch(r"\d+", (element.text or "").strip())
    }


def _legend_numbers(model_text: str) -> set[str]:
    _, body = mechanical_prose(model_text)
    return {
        match.group(1)
        for line in body.splitlines()
        if (match := re.match(r"^\s*(\d+)[.)]\s+", line))
    }


def _nudge_callouts(root: ElementTree.Element) -> None:
    """Separate numeric callout labels deterministically without adding SVG syntax."""
    labels = [
        element
        for element in root.iter("text")
        if re.fullmatch(r"\d+", (element.text or "").strip())
    ]
    circles = list(root.iter("circle"))

    def paired_circle(label: ElementTree.Element) -> ElementTree.Element | None:
        x, y = _number(label.get("x")), _number(label.get("y"))
        candidates = [
            circle
            for circle in circles
            if math.hypot(_number(circle.get("cx")) - x, _number(circle.get("cy")) - y)
            <= _number(circle.get("r")) + _MIN_TEXT
        ]
        return min(
            candidates,
            key=lambda circle: math.hypot(
                _number(circle.get("cx")) - x, _number(circle.get("cy")) - y
            ),
            default=None,
        )

    for index, label in enumerate(labels):
        bounds = _element_bounds(label)
        if bounds is None:
            continue
        for earlier in labels[:index]:
            previous = _element_bounds(earlier)
            if (
                previous is None
                or bounds[2] < previous[0]
                or previous[2] < bounds[0]
                or bounds[3] < previous[1]
                or previous[3] < bounds[1]
            ):
                continue
            # The vertical offset is deterministic and follows the usual leader-line
            # direction for a side drawing. Move a matching callout circle with text.
            dy = max(_MIN_TEXT + 4, previous[3] - bounds[1] + _MIN_TEXT + 4)
            dx = _MIN_TEXT + 4 if index % 2 else -(_MIN_TEXT + 4)
            _translate_callout(label, dx, dy)
            if circle := paired_circle(label):
                _translate_callout(circle, dx, dy)
            bounds = _element_bounds(label) or bounds
    # A callout disk can overlap even when its numeral's text boxes do not.
    paired = [(label, paired_circle(label)) for label in labels]
    for index, (label, circle) in enumerate(paired):
        if circle is None:
            continue
        current = _element_bounds(circle)
        if current is None:
            continue
        for earlier_label, earlier_circle in paired[:index]:
            if earlier_circle is None:
                continue
            previous = _element_bounds(earlier_circle)
            if (
                previous is None
                or current[2] < previous[0]
                or previous[2] < current[0]
                or current[3] < previous[1]
                or previous[3] < current[1]
            ):
                continue
            dy = max(_MIN_TEXT + 4, previous[3] - current[1] + _MIN_TEXT + 4)
            _translate_callout(label, 0, dy)
            _translate_callout(circle, 0, dy)
            current = _element_bounds(circle) or current


def _normalise_mechanical_svg(
    root: ElementTree.Element, canvas: tuple[float, float, float, float]
) -> None:
    """Make accepted model geometry readable in the fixed passive SVG canvas."""
    for parent in list(root.iter()):
        for child in list(parent):
            bounds = _element_bounds(child)
            if bounds is not None and _outside(bounds, canvas):
                parent.remove(child)
    _nudge_callouts(root)
    geometry_bounds = [
        bound for element in root.iter() if (bound := _element_bounds(element))
    ]
    if geometry_bounds:
        left, top = (
            min(bound[0] for bound in geometry_bounds),
            min(bound[1] for bound in geometry_bounds),
        )
        right, bottom = (
            max(bound[2] for bound in geometry_bounds),
            max(bound[3] for bound in geometry_bounds),
        )
        used_width, used_height = max(1.0, right - left), max(1.0, bottom - top)
        # A uniform scale keeps mechanical proportions truthful; the fixed rendered
        # width makes even a tall narrow object readable in the UI and viewer.
        scale = min(canvas[2] / used_width, canvas[3] / used_height)
        if used_width * used_height < (canvas[2] * canvas[3] / 2):
            group = ElementTree.Element("g")
            group.set(
                "transform",
                "translate(%s %s) scale(%s) translate(%s %s)"
                % (
                    round((canvas[2] - used_width * scale) / 2, 2),
                    round((canvas[3] - used_height * scale) / 2, 2),
                    round(scale, 4),
                    round(-left, 2),
                    round(-top, 2),
                ),
            )
            group.extend(list(root))
            root[:] = [group]
    for element in root.iter():
        if "stroke-width" in element.attrib:
            element.set(
                "stroke-width",
                str(max(_MIN_STROKE, _number(element.get("stroke-width")))),
            )
        if element.tag == "text":
            element.set(
                "font-size",
                str(max(_MIN_TEXT, _number(element.get("font-size"), _MIN_TEXT))),
            )
    root.set("viewBox", " ".join(str(int(value)) for value in canvas))
    root.set("width", str(int(canvas[2])))
    root.set("height", str(int(canvas[3])))


def extract_svg(
    model_text: str,
    *,
    require_legend: bool = False,
    canvas: tuple[float, float, float, float] = _FIXED_VIEWBOX,
) -> str | None:
    """Validate primitives; normalize only the standard SVG namespace."""

    def reject(reason: str) -> None:
        # Reasons are fixed codes, never user/model content or parser exceptions.
        _LOGGER.warning("mechanical_svg_rejected reason=%s", reason)

    match = _SVG.search(model_text)
    if not match:
        reject("missing_svg")
        return None
    raw = match.group(0)
    if len(raw.encode()) > 96_000:
        reject("byte_limit")
        return None
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        reject("invalid_xml")
        return None
    elements = list(root.iter())
    if len(elements) > 120:
        reject("element_limit")
        return None
    for element in elements:
        if element.tag.startswith("{" + _SVG_NAMESPACE + "}"):
            element.tag = element.tag.split("}", 1)[1]
        if element.tag == "polygon":
            # Models use polygons for arrowheads; lower inert numeric geometry
            # to an existing allowed primitive, never broaden the output tags.
            points = element.attrib.get("points", "")
            number = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
            if not re.fullmatch(rf"\s*{number}(?:[\s,]+{number})+\s*", points):
                reject("invalid_polygon_points")
                return None
            coordinates = re.findall(number, points)
            if (
                len(coordinates) < 6
                or len(coordinates) % 2
                or not all(math.isfinite(float(v)) for v in coordinates)
                or "d" in element.attrib
            ):
                reject("invalid_polygon_points")
                return None
            del element.attrib["points"]
            element.tag = "path"
            element.set(
                "d",
                "M "
                + " L ".join(
                    " ".join(coordinates[i : i + 2])
                    for i in range(0, len(coordinates), 2)
                )
                + " Z",
            )
            _LOGGER.info("mechanical_svg_normalized reason=polygon_to_path")
        if element.tag not in _ALLOWED_TAGS:
            reject("forbidden_element")
            return None
    if root.tag != "svg" or "viewBox" not in root.attrib:
        reject("missing_viewbox")
        return None
    try:
        viewbox = [
            float(value) for value in root.attrib["viewBox"].replace(",", " ").split()
        ]
    except ValueError:
        reject("invalid_viewbox")
        return None
    if (
        len(viewbox) != 4
        or not all(math.isfinite(v) for v in viewbox)
        or viewbox[2] <= 0
        or viewbox[3] <= 0
        or max(viewbox[2:]) > 10000
    ):
        reject("invalid_viewbox")
        return None
    for element in elements:
        for name, value in list(element.attrib.items()):
            if re.search(r"url\s*\(|https?:|\\", value, re.I):
                reject("external_reference")
                return None
            # Cosmetic extras are discarded, never added to the output allow-list.
            if name in {
                "font-weight",
                "stroke-linecap",
                "stroke-linejoin",
                "stroke-dasharray",
                "dominant-baseline",
                "id",
                "class",
                "version",
                "preserveAspectRatio",
                "fill-opacity",
                "stroke-opacity",
                "stroke-miterlimit",
                "stroke-dashoffset",
                "font-style",
                "letter-spacing",
            }:
                del element.attrib[name]
                continue
            if name.startswith("on") or name in {"href", "xlink:href", "style"}:
                reject("forbidden_attribute")
                return None
            if name not in _COMMON_ATTRS and not (
                element is root and name in {"viewBox", "role", "aria-label"}
            ):
                reject("unknown_attribute")
                return None
        if element.text and len(element.text) > 300:
            reject("text_limit")
            return None
    if require_legend:
        callouts, legend = _callout_numbers(root), _legend_numbers(model_text)
        if len(callouts) < 3 or callouts != legend:
            reject("callout_legend_mismatch")
            return None
    _normalise_mechanical_svg(root, canvas)
    root.set("xmlns", _SVG_NAMESPACE)
    return ElementTree.tostring(root, encoding="unicode")


def without_svg_or_code(model_text: str) -> str:
    clean = re.sub(
        r"`{3,}.*?`{3,}|~{3,}.*?~{3,}|<svg\b[^>]*>.*?</svg\s*>|<!--.*?-->",
        "",
        model_text,
        flags=re.I | re.S,
    )
    # Splitting around an SVG can leave one half of its fence in each section.
    # Never let those delimiters fence the service-owned image and source line.
    return re.sub(r"(?m)^[ \t]*(?:`{3,}|~{3,})[^\n]*$", "", clean).strip()


def mechanical_prose(model_text: str) -> tuple[str, str]:
    """Separate introductory prose without treating it as numbered parts."""
    drawing = _SVG.search(model_text)
    if drawing:
        return (
            without_svg_or_code(model_text[: drawing.start()]),
            without_svg_or_code(model_text[drawing.end() :]),
        )
    clean = without_svg_or_code(model_text)
    first_item = re.search(r"^\s*\d+[.)]\s+", clean, re.M)
    if not first_item:
        return clean, ""
    return clean[: first_item.start()].strip(), clean[first_item.start() :].strip()


def mechanical_legend(model_text: str, lang: str) -> str:
    """Preserve model numbering and prose; only numbered lines are legend items."""
    _, heading, _, absent = _COPY.get(lang, _COPY["en"])
    _, body = mechanical_prose(model_text)
    if not body:
        return f"{heading}\n\n{absent}"
    lines: list[str] = []
    previous_item = False
    for line in body.splitlines():
        item = bool(re.match(r"^\s*\d+[.)]\s+", line))
        if previous_item and line.strip() and not item and not line.startswith("  "):
            lines.append("")
        lines.append(line)
        previous_item = item
    return f"{heading}\n\n" + "\n".join(lines)


_MECHANICAL_COPY = {
    "en": (
        "Here is a simplified mechanical view.",
        "Simplified mechanical drawing",
        "The model drawing could not be validated; the textual explanation follows.",
        "This SVG is a simplified illustration drawn by a model.",
        "Source",
    ),
    "fr": (
        "Voici une vue mécanique simplifiée.",
        "Dessin mécanique simplifié",
        "Le dessin du modèle n’a pas pu être validé ; voici l’explication textuelle.",
        "Ce SVG est une illustration simplifiée dessinée par un modèle.",
        "Source",
    ),
    "de": (
        "Hier ist eine vereinfachte mechanische Ansicht.",
        "Vereinfachte mechanische Zeichnung",
        "Die Modellzeichnung konnte nicht validiert werden; es folgt die textliche Erklärung.",
        "Dieses SVG ist eine vereinfachte, von einem Modell gezeichnete Illustration.",
        "Quelle",
    ),
    "es": (
        "Aquí se muestra una vista mecánica simplificada.",
        "Dibujo mecánico simplificado",
        "No se pudo validar el dibujo del modelo; a continuación se ofrece la explicación textual.",
        "Este SVG es una ilustración simplificada dibujada por un modelo.",
        "Fuente",
    ),
    "it": (
        "Ecco una vista meccanica semplificata.",
        "Disegno meccanico semplificato",
        "Non è stato possibile convalidare il disegno del modello; segue la spiegazione testuale.",
        "Questo SVG è un’illustrazione semplificata disegnata da un modello.",
        "Fonte",
    ),
}


def mechanical_copy(lang: str) -> tuple[str, str, str, str, str]:
    return _MECHANICAL_COPY.get(lang, _MECHANICAL_COPY["en"])


def technical_prompt(question: str) -> str:
    """Describe physical structure positively for the image model."""
    exploded = bool(
        re.search(
            r"exploded|cutaway|parts|pieces|tourne|eclatee|teile|dreht|"
            r"explosions|por dentro|piezas|gira|fatto|pezzi|esplosa",
            _normalized(question),
        )
    )
    view = "an exploded or cutaway view" if exploded else "an exterior mechanical view"
    return (
        f"{positive_image_prompt(question)}\n\nCreate a clean technical illustration of the physical object: "
        f"{view}, white background, clear geometry, metallic materials, soft studio lighting."
    )


_COPY = {
    "en": (
        "This is a model-generated technical illustration and details may be inexact.",
        "Legend",
        "Numbers indicate the main parts; check the legend.",
        "The text model did not provide a part legend.",
    ),
    "fr": (
        "Cette illustration technique est générée par un modèle et certains détails peuvent être inexacts.",
        "Légende",
        "Les numéros indiquent les pièces principales ; vérifiez la légende.",
        "Le modèle de texte n'a pas fourni de légende des pièces.",
    ),
    "de": (
        "Dies ist eine von einem Modell erzeugte technische Illustration; Details können ungenau sein.",
        "Legende",
        "Die Nummern markieren die wichtigsten Teile; bitte die Legende prüfen.",
        "Das Textmodell hat keine Teilelegende geliefert.",
    ),
    "es": (
        "Esta es una ilustración técnica generada por un modelo y algunos detalles pueden ser inexactos.",
        "Leyenda",
        "Los números indican las piezas principales; consulte la leyenda.",
        "El modelo de texto no proporcionó una leyenda de las piezas.",
    ),
    "it": (
        "Questa è un'illustrazione tecnica generata da un modello e alcuni dettagli potrebbero essere inesatti.",
        "Legenda",
        "I numeri indicano le parti principali; verificare la legenda.",
        "Il modello di testo non ha fornito una legenda delle parti.",
    ),
}


def numbered_legend(model_text: str, lang: str) -> str:
    """Format model-provided part descriptions without adding part names ourselves."""
    honesty, heading, footer, absent = _COPY.get(lang, _COPY["en"])
    without_code = re.sub(r"`{3,}.*?`{3,}", "", model_text, flags=re.DOTALL)
    lines = [
        re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
        for line in without_code.splitlines()
        if line.strip() and "mermaid" not in line.lower()
    ]
    if not lines:
        lines = [absent]
    entries = "\n".join(f"{index}. {line}" for index, line in enumerate(lines, 1))
    return f"\n\n{honesty}\n\n{heading}\n{entries}\n\n_{footer}_"
