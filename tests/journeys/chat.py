"""User journeys use only the same HTTP API and SSE responses as Open WebUI."""

import base64
import json
import os
from packages.ports import atlas_ports
from pathlib import Path
import re
from urllib.request import Request, urlopen

from journey_language import matches

API = os.environ.get("ATLAS_CHAT_API", atlas_ports().url("adapter"))


def ask(
    messages: list[dict[str, object]], stream: bool = True, ui_locale: str = "fr"
) -> tuple[str, list[dict[str, object]]]:
    request = Request(
        API + "/v1/chat/completions",
        data=json.dumps(
            {
                "model": "atlas-qwen",
                "messages": messages,
                "stream": stream,
                "ui_locale": ui_locale,
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=125) as response:
        body = response.read().decode()
    if not stream:
        return str(json.loads(body)["choices"][0]["message"]["content"]), []
    assert "data: [DONE]" in body
    events = [
        json.loads(line[6:])
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert events and not any("error" in event for event in events), body[:1000]
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    return "".join(
        event["choices"][0]["delta"].get("content", "") for event in events
    ), events


def user(text: str) -> dict[str, object]:
    return {"role": "user", "content": text}


def main() -> None:
    report: dict[str, object] = {
        "mode": "live"
        if os.environ.get("JOURNEYS_LIVE") == "1"
        or os.environ.get("JOURNEYS_REFRESH") == "1"
        else "replay"
    }
    if os.environ.get("JOURNEYS_RESUME"):
        report["resumed_recorded_prefix"] = True
    report["image_mode"] = report["mode"]
    report["web_mode"] = (
        "live" if os.environ.get("JOURNEYS_REFRESH") == "1" else report["mode"]
    )
    if os.environ.get("JOURNEYS_REFRESH") == "1":
        report.update(
            acquisition_mode="mixed",
            image_mode="replay",
            web_mode="mixed",
            inference_mode="mixed",
        )
    path = Path("BRAIN/eval/journeys.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        answer, _ = ask(
            [user("Réponds en une phrase : quelle est la capitale de la France ?")]
        )
        assert "paris" in answer.lower()
        report["J1_plain_answer"] = True
        assert matches(answer, "fr"), answer
        report["J1_language_match"] = True
        answer, _ = ask(
            [
                user(
                    "D'après la politique interne de télétravail, quel est l'objet du document et quels salariés sont concernés ? Cite le document."
                )
            ]
        )
        links = re.findall(r"\]\((https?://[^)]+/sources/[a-f0-9]{64})\)", answer)
        assert links, answer
        for link in links:
            with urlopen(link, timeout=5) as response:
                assert response.status == 200 and len(response.read()) > 100
        report["J2_citation_resolvable"] = True
        rag_answer = answer
        encoded = base64.b64encode(
            Path("tests/cassettes/vision/shapes.png").read_bytes()
        ).decode()
        answer, _ = ask(
            [
                user("Bonjour, discutons en français."),
                {
                    "role": "assistant",
                    "content": "Bonjour, comment puis-je vous aider ?",
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Décris les formes, leurs couleurs et leurs positions dans cette image.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + encoded},
                        },
                    ],
                },
            ]
        )
        assert all(
            word in answer.lower() for word in ("rouge", "bleu", "carré", "cercle")
        ), answer
        report["J3_image_described"] = True
        assert matches(answer, "fr"), answer
        report["J3_language_match"] = True
        answer, _ = ask(
            [user("crée moi une image d'un chien qui danse sur une table bleue")]
        )
        from journeys.m18 import picture, run_m18

        picture(answer)
        report["J4_image_returned"] = True
        command = user("/project create JourneyAlpha")
        selected, _ = ask([command])
        answer, _ = ask(
            [
                command,
                {"role": "assistant", "content": selected},
                user(
                    "Le nom du prototype de ce projet est Ambre-742. Mémorise ce fait."
                ),
            ]
        )
        command = user("/project use JourneyAlpha")
        selected, _ = ask([command])
        answer, _ = ask(
            [
                command,
                {"role": "assistant", "content": selected},
                user("Quel est le nom du prototype de ce projet ?"),
            ]
        )
        recalled = answer.split("\n\n", 1)[-1]
        assert "Ambre-742" in recalled, answer
        report["J5_memory_shared"] = True
        alpha_command, alpha_selected = command, selected
        alpha_image: dict[str, object] = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Décris cette image du projet Alpha."},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + encoded},
                },
            ],
        }
        description, _ = ask(
            [
                alpha_command,
                {"role": "assistant", "content": alpha_selected},
                alpha_image,
            ]
        )
        assert "rouge" in description.lower() and "bleu" in description.lower()
        command = user("/project create JourneyBeta")
        selected, _ = ask([command])
        answer, _ = ask(
            [
                user("/project use JourneyAlpha"),
                {"role": "assistant", "content": alpha_selected},
                alpha_image,
                {"role": "assistant", "content": description},
                command,
                {"role": "assistant", "content": selected},
                user("Quel est le nom du prototype de ce projet ?"),
            ]
        )
        assert "Ambre" not in answer and "742" not in answer, answer
        report["J5_projects_isolated"] = True
        answer, events = ask(
            [
                user(
                    "Recherche sur le web puis lis une source : en quelle année le CERN a-t-il rendu le World Wide Web public ? Cite la source et la date de consultation."
                )
            ]
        )
        finished = {
            atlas.get("tool")
            for event in events
            if isinstance(atlas := event.get("atlas"), dict)
            and atlas.get("phase") == "tool_finished"
            and atlas.get("ok") is True
        }
        assert {"web_search", "web_fetch"} <= finished
        report["J6_tools_used"] = True
        assert "1993" in answer and "http" in answer, answer
        assert matches(answer, "fr"), answer
        report["J6_final_answer"] = True
        config = dict(
            line.split("=", 1)
            for line in Path("infra/chat-ui.env").read_text().splitlines()
            if "=" in line and not line.startswith("#")
        )
        for name in (
            "ENABLE_TITLE_GENERATION",
            "ENABLE_TAGS_GENERATION",
            "ENABLE_FOLLOW_UP_GENERATION",
            "ENABLE_AUTOCOMPLETE_GENERATION",
            "ENABLE_PERSISTENT_CONFIG",
        ):
            assert config[name] == "False"
        assert "auxiliary" in Path("runbooks/chat.md").read_text().lower()
        report["J7_auxiliary_ok"] = True
        run_m18(report, rag_answer, encoded)
        from journeys.m19 import run_m19

        run_m19(report, encoded)
    finally:
        report["journey_modes"] = {
            f"J{number}": report["mode"] for number in range(1, 21) if number != 8
        }
        path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
