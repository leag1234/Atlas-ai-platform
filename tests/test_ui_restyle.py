"""Offline contracts for the owner-approved Open WebUI restyle."""

import unittest
from pathlib import Path

from services.orchestrator.ui_injection import ATLAS_UI_TAGS, inject_atlas_ui


def luminance(hex_color: str) -> float:
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return sum(
        value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722))
    )


def contrast(left: str, right: str) -> float:
    a, b = sorted((luminance(left), luminance(right)), reverse=True)
    return (a + 0.05) / (b + 0.05)


class UIRestyleTest(unittest.TestCase):
    def test_every_approved_text_pair_is_aa(self) -> None:
        palettes = {
            "dense": (
                (
                    "#66665f",
                    ("#fbfbfa", "#f4f4f1", "#ffffff", "#f0efea", "#e6e6e1"),
                    "#ffffff",
                ),
                (
                    "#909088",
                    ("#121211", "#171716", "#1a1a19", "#242422", "#262624"),
                    "#0b0b0c",
                ),
            ),
            "arrondi": (
                (
                    "#646468",
                    ("#ffffff", "#f2f2f7", "#f2f2f7", "#f2f2f7", "#e5e5ea"),
                    "#ffffff",
                ),
                (
                    "#98989d",
                    ("#000000", "#1c1c1e", "#1c1c1e", "#1c1c1e", "#2c2c2e"),
                    "#0b0b0c",
                ),
            ),
            "document": (
                (
                    "#676662",
                    ("#ffffff", "#fbfaf8", "#f7f6f3", "#f7f6f3", "#e9e8e4"),
                    "#ffffff",
                ),
                (
                    "#9b9a97",
                    ("#191919", "#202020", "#252525", "#252525", "#2f2f2f"),
                    "#0b0b0c",
                ),
            ),
        }
        accents = (
            ("#1f7534", "#0960d6", "#b63d1d", "#6d4bd1", "#2a2a28"),
            ("#30d158", "#4d9bff", "#ff7a45", "#a58bff", "#e6e6e6"),
        )
        for style, modes in palettes.items():
            for mode, (muted, surfaces, on_accent) in enumerate(modes):
                for surface in surfaces:
                    self.assertGreaterEqual(
                        contrast(muted, surface), 4.5, f"{style}:{mode}:muted:{surface}"
                    )
                    for accent in accents[mode]:
                        self.assertGreaterEqual(
                            contrast(accent, surface),
                            4.5,
                            f"{style}:{mode}:{accent}:{surface}",
                        )
                        self.assertGreaterEqual(
                            contrast(on_accent, accent),
                            4.5,
                            f"{style}:{mode}:onAccent:{accent}",
                        )

    def test_defaults_and_persistence_contract_are_present(self) -> None:
        script = Path("services/orchestrator/chat-ui.js").read_text()
        self.assertIn(
            'style: "arrondi", mode: "system", accent: "green", size: "16"', script
        )
        self.assertIn("localStorage.setItem(key, JSON.stringify(saved))", script)
        self.assertIn('localStorage.setItem("theme", button.dataset.value)', script)
        self.assertIn('event.key === "Escape"', script)
        self.assertIn("aria-expanded", script)

    def test_selector_has_every_label_in_every_supported_language(self) -> None:
        script = Path("services/orchestrator/chat-ui.js").read_text()
        labels = (
            "title",
            "style",
            "mode",
            "accent",
            "size",
            "dense",
            "arrondi",
            "document",
            "system",
            "light",
            "dark",
            "green",
            "blue",
            "orange",
            "violet",
            "graphite",
        )
        for language in ("en", "fr", "de", "es", "it"):
            section = script.split(f"    {language}: {{", 1)[1].split(" },", 1)[0]
            for label in labels:
                self.assertIn(f"{label}: ", section, f"{language}:{label}")
        self.assertIn("document.documentElement.lang", script)
        self.assertIn("navigator.language", script)
        self.assertIn("${value} px", script)

    def test_long_message_toggle_is_localized_for_every_supported_language(
        self,
    ) -> None:
        script = Path("services/orchestrator/chat-ui.js").read_text()
        expected = {
            "en": ("Show more", "Show less"),
            "fr": ("Afficher plus", "Afficher moins"),
            "de": ("Mehr anzeigen", "Weniger anzeigen"),
            "es": ("Mostrar más", "Mostrar menos"),
            "it": ("Mostra di più", "Mostra di meno"),
        }
        for language, labels in expected.items():
            section = script.split(f"    {language}: {{", 1)[1].split(" },", 1)[0]
            for label in labels:
                self.assertIn(label, section, f"{language}:{label}")
        self.assertIn("new MutationObserver", script)
        self.assertIn('attributeFilter: ["data-atlas-size"]', script)
        self.assertIn('setAttribute("aria-expanded"', script)

    def test_file_card_text_is_readable_for_every_palette_and_accent(self) -> None:
        palettes = {
            "dense": (("#1c1c19", "#ffffff"), ("#ecece8", "#1a1a19")),
            "arrondi": (("#1d1d1f", "#f2f2f7"), ("#f5f5f7", "#1c1c1e")),
            "document": (("#2a2a28", "#f7f6f3"), ("#e6e6e3", "#252525")),
        }
        accents = ("green", "blue", "orange", "violet", "graphite")
        for style, modes in palettes.items():
            for mode, (text, card) in enumerate(modes):
                for accent in accents:
                    self.assertGreaterEqual(
                        contrast(text, card), 4.5, f"{style}:{mode}:{accent}:file-card"
                    )
        css = Path("services/orchestrator/chat-ui.css").read_text()
        self.assertIn("color:var(--atlas-text)!important", css)
        self.assertIn(".chat-user > .mb-1 > .self-end > button", css)
        self.assertIn("background:var(--atlas-surface)!important", css)

    def test_injector_adds_exactly_one_link_and_script(self) -> None:
        shell = (
            b"<html><head><title>Open WebUI</title></head><body>unchanged</body></html>"
        )
        injected = inject_atlas_ui(shell)
        self.assertEqual(injected.count(ATLAS_UI_TAGS), 1)
        self.assertEqual(inject_atlas_ui(injected), injected)
        self.assertIn(b"<body>unchanged</body>", injected)
