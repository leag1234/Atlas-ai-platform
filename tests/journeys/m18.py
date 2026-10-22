"""M18 user journeys; all model interactions use the public chat endpoint."""

import base64
import io
import json
import re
from pathlib import Path
from urllib.request import urlopen

from PIL import Image
from journey_language import matches
from journeys.chat import ask, user


def atlas(event: dict[str, object]) -> dict[str, object]:
    value = event.get("atlas", {})
    assert isinstance(value, dict)
    return value


def picture(answer: str) -> tuple[str, bytes]:
    match = re.search(r"!\[[^\]]*\]\((https?://[^)]+/images/[a-f0-9]{32})\)", answer)
    assert match and "base64" not in answer, answer
    with urlopen(match[1], timeout=5) as response:
        raw = response.read()
    with Image.open(io.BytesIO(raw)) as image:
        assert image.size == (1024, 1024)
        image.verify()
    return match[1], raw


def run_m18(report: dict[str, object], rag_answer: str, encoded: str) -> None:
    translated, events = ask(
        [
            user(
                "D'après la politique interne de télétravail, quels salariés sont concernés ?"
            ),
            {"role": "assistant", "content": rag_answer},
            user("Traduis ta réponse précédente en allemand."),
        ]
    )
    assert matches(translated, "de"), translated
    assert not any(atlas(e).get("phase") == "tool_finished" for e in events)
    report["J9_followup_after_rag"] = True

    prompt = "Crée une image d'un chat dansant avec un chien, tous deux portant des patins à roulettes et des lunettes roses."
    generated, events = ask([user(prompt)])
    reference, raw = picture(generated)
    images = atlas(events[-1])["images"]
    assert isinstance(images, list)
    metadata = images[0]
    assert isinstance(metadata, dict)
    assert metadata["prompt"] == prompt
    rewritten = metadata["rewritten_prompt"]
    assert rewritten != prompt and all(
        k in rewritten for k in ("Subjects:", "Attributes:", "Scene:", "Style:")
    )
    assert type(metadata["seed"]) is int and metadata["seed"] != 42
    report.update(
        j14_prompt=prompt,
        j14_rewritten_prompt=rewritten,
        j14_seed=metadata["seed"],
        j14_image=reference,
    )
    Path("BRAIN/eval/m18-image.png").write_bytes(raw)
    assessment, _ = ask(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": 'Examine uniquement cette image. Retourne un objet JSON avec trois booléens : "animals_dancing" (chat et chien dansants visibles), "roller_skates" (patins à roulettes visibles), "pink_glasses" (lunettes roses visibles). Si incertain, false. Pas de Markdown.',
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(raw).decode()
                        },
                    },
                ],
            }
        ]
    )
    start, end = assessment.index("{"), assessment.rindex("}") + 1
    detail = json.loads(assessment[start:end])
    keys = ("animals_dancing", "roller_skates", "pink_glasses")
    assert all(type(detail.get(k)) is bool for k in keys), assessment
    report["j14_constraints_detail"] = {k: detail[k] for k in keys}
    report["j14_constraints_met"] = sum(detail[k] for k in keys)
    report["J14_settings_applied"] = True

    modified, _ = ask(
        [
            user(prompt),
            {"role": "assistant", "content": generated},
            user("Ajoute un chapeau bleu au chat."),
        ]
    )
    next_reference, _ = picture(modified)
    assert next_reference != reference
    report["J10_iterate_on_image"] = True

    answer, _ = ask(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Pourrais-tu modifier cette image ?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + encoded},
                    },
                ],
            }
        ]
    )
    assert "pas prise en charge" in answer and "générer" in answer, answer
    report["J11_edit_intent_honest"] = True

    answer, events = ask(
        [
            user(
                "Recherche sur le web : en quelle année Python a-t-il été publié pour la première fois ? Cite une source et sa date de consultation."
            )
        ],
        ui_locale="de",
    )
    search = [
        atlas(e)
        for e in events
        if atlas(e).get("phase") == "tool_finished"
        and atlas(e).get("tool") == "web_search"
    ]
    assert (
        len(search) >= 2
        and search[0]["ok"] is False
        and any(e["ok"] is True for e in search[1:])
    ), search
    assert "1991" in answer and "http" in answer, answer
    report["J12_web_retry"] = True
    labels = json.loads(Path("prompts/progress.json").read_text())
    assert labels["de"] in answer and labels["fr"] not in answer, answer
    report["J13_labels_fixed"] = True
