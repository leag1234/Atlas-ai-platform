"""Usage-derived, dated M20 regressions through public chat HTTP responses."""

import base64
import io
import json
from pathlib import Path
from typing import Callable, Any

from PIL import Image

# Verbatim user cases (2026-09-15; edit question originally 2026-09-13).
VERBATIM = (
    "dessine-moi un mouton",
    "décris cette image",
    "intègre ces deux images",
    "tu saurais modifier cette image ?",
    "fais-moi un portrait",
    "je voudrais voir",
    "decris cette image",
    "DESSINE UN",
    "draw me a",
    "analyse cette image",
    "décris l'image ci-jointe",
)


def attachment(color: str) -> dict[str, object]:
    data = io.BytesIO()
    Image.new("RGB", (1024, 1024), color).save(data, format="PNG")
    return {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()
        },
    }


def run_m20(
    ask: Callable[..., Any], report: dict[str, object], cold_start: Callable[[], None]
) -> None:
    cases = json.loads(Path("tests/journeys/m20-cases.json").read_text())
    # Intent: learn how supplied pictures differ; send two images, not copies.
    images = [attachment("red"), attachment("blue")]
    for prompt in cases["editing"]:
        text, _ = ask(
            prompt, images, lang="en" if prompt.startswith("combine") else "fr"
        )
        lowered = text.lower()
        assert ("pas prise en charge" in lowered and "générer" in lowered) or (
            "not supported" in lowered and "generate" in lowered
        ), text
        assert "context_exceeded" not in text and "![" not in text
    report["J21_two_images_honest"] = True
    text, _ = ask(cases["J23"], images[:1])
    assert "pas prise en charge" in text and "générer" in text, text
    report["J23_edit_intent_honest"] = True
    for prompt in cases["comparison"]:
        text, _ = ask(
            prompt, images, lang="en" if prompt.startswith("compare these") else "fr"
        )
        lowered = text.lower()
        assert ("rouge" in lowered and "bleu" in lowered) or (
            "red" in lowered and "blue" in lowered
        ), text
        assert "![" not in text and len(text) > 30
    report["J22_two_images_compare"] = True
    text, metadata = ask(cases["J24"])
    assert "![" in text and "/images/" in text, text
    first = metadata[0]
    for key in (
        "rewritten_prompt",
        "original_request",
        "seed",
        "model",
        "width",
        "height",
        "steps",
    ):
        assert key in first, key
    assert first["original_request"] == cases["J24"]
    report.update(
        j24_prompt=cases["J24"],
        j24_rewritten_prompt=first["rewritten_prompt"],
        j24_seed=first["seed"],
        j24_image=first["reference"],
    )
    # The generated image itself, rather than the requested constraints, is judged.
    detail, _ = ask(
        'Examine cette image. Retourne uniquement un objet JSON de booléens : "sheep_present", "blue_cow", "dancing", "pink_glasses_on_both", "yellow_skates_on_both", "sheep_has_five_legs". Observe les pattes visibles; false si incertain.',
        [{"type": "image_url", "image_url": {"url": first["assessment_image"]}}],
    )
    result = json.loads(detail[detail.index("{") : detail.rindex("}") + 1])
    keys = (
        "sheep_present",
        "blue_cow",
        "dancing",
        "pink_glasses_on_both",
        "yellow_skates_on_both",
        "sheep_has_five_legs",
    )
    assert all(type(result.get(k)) is bool for k in keys), result
    report["j24_constraints_detail"] = result
    report["J24_constraints_reported"] = True
    cold_start()
    text, metadata = ask("Dessine un cube vert.", stream=True)
    assert "démarre" in text and "![" in text and "/images/" in text, text
    assert metadata and metadata[0]["reference"]
    report["J25_worker_on_demand"] = True
