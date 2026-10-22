"""M19 user-visible intent and language, through the public chat API only."""

import json
from pathlib import Path
import re

from journey_language import matches
from journeys.chat import ask, user
from journeys.m18 import atlas, picture


def run_m19(report: dict[str, object], encoded: str) -> None:
    cases = json.loads(Path("tests/journeys/m19-cases.json").read_text())
    labels = json.loads(Path("prompts/progress.json").read_text())
    history: list[dict[str, object]] = [user("Bonjour, discutons en français.")]
    for key in ("J15", "J16"):
        for prompt in cases[key]:
            answer, events = ask([user(prompt)])
            picture(answer)
            assert "[Image générée]" in answer, answer
            assert any(
                atlas(e).get("tool") == "generate_image" and atlas(e).get("ok") is True
                for e in events
            )
        report["J15_draw_me_phrasing" if key == "J15" else "J16_other_phrasings"] = True
    for prompt in (
        "Draw me a sheep",
        "Make me a portrait of a cat",
        "I would like to see a dragon",
    ):
        answer, events = ask([user(prompt)], ui_locale="en")
        picture(answer)
        assert "[Generated image]" in answer, answer
        assert any(atlas(e).get("tool") == "generate_image" for e in events)
    report["english_image_phrasings"] = True
    for key in ("J17", "J19"):
        for prompt in cases[key]:
            message: dict[str, object] = {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + encoded},
                    },
                ],
            }
            answer, events = ask(
                history
                + [
                    {
                        "role": "assistant",
                        "content": "Bonjour, comment puis-je vous aider ?",
                    },
                    message,
                ],
                ui_locale="en",
            )
            assert not re.search(r"!\[|/images/", answer), answer
            assert not any(atlas(e).get("tool") == "generate_image" for e in events)
            assert "rouge" in answer.lower() and "bleu" in answer.lower(), answer
            assert matches(answer, "fr"), answer
        report[
            "J17_no_false_generation" if key == "J17" else "J19_short_message_language"
        ] = True
    answer, events = ask([user(cases["J18"][0])])
    assert matches(answer, "fr") and re.search(r"\boui\b", answer, re.I), answer
    assert not re.search(
        r"je ne peux pas|assistant textuel|dall[-· ]?e|midjourney|stable diffusion|!\[",
        answer,
        re.I,
    ), answer
    assert not any(atlas(e).get("tool") == "generate_image" for e in events)
    report["J18_capability_acknowledged"] = True
    question = "Recherche sur le web puis lis une source : en quelle année le CERN a-t-il rendu le World Wide Web public ? Cite la source et la date de consultation."
    web, web_events = ask(
        history + [{"role": "assistant", "content": "Bien sûr."}, user(question)]
    )
    assert matches(web, "fr") and "1993" in web and "http" in web, web
    assert {"web_search", "web_fetch"} <= {
        atlas(e).get("tool") for e in web_events if atlas(e).get("ok") is True
    }
    history += [
        {"role": "assistant", "content": "Bien sûr."},
        user(question),
        {"role": "assistant", "content": web},
    ]
    generated, events = ask(history + [user("Dessine un dragon bleu.")])
    picture(generated)
    assert "[Image générée]" in generated, generated
    for event in web_events + events:
        if atlas(event).get("phase") == "intermediate":
            choices = event["choices"]
            assert isinstance(choices, list)
            content = choices[0]["delta"].get("content", "")
            if content:
                assert content.strip() == "*" + labels["fr"] + "*", content
    assert not any(
        label in web + generated for lang, label in labels.items() if lang != "fr"
    )
    report["J20_language_consistent"] = True
