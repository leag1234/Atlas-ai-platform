"""Offline contracts for the injected Open WebUI diagram viewer."""

import unittest
from pathlib import Path


class DiagramViewerContractTest(unittest.TestCase):
    def test_localized_accessible_viewer_controls_cover_supported_languages(
        self,
    ) -> None:
        script = Path("services/orchestrator/chat-ui.js").read_text()
        expected = {
            "en": "Expand diagram",
            "fr": "Agrandir le diagramme",
            "de": "Diagramm vergrößern",
            "es": "Ampliar diagrama",
            "it": "Ingrandisci diagramma",
        }
        for language, label in expected.items():
            section = script.split(f"    {language}: {{", 1)[1].split(" },", 1)[0]
            self.assertIn(label, section, f"{language}:expand")
            for key in (
                "closeDiagram",
                "zoomIn",
                "zoomOut",
                "fitDiagram",
                "downloadDiagram",
                "regenerateDiagram",
                "copyDiagram",
            ):
                self.assertIn(f"{key}:", section, f"{language}:{key}")

    def test_svg_enhancer_is_generic_late_render_safe_and_forwards_actions(
        self,
    ) -> None:
        script = Path("services/orchestrator/chat-ui.js").read_text()
        css = Path("services/orchestrator/chat-ui.css").read_text()
        self.assertIn('querySelectorAll(".markdown-prose svg")', script)
        self.assertIn("new MutationObserver", script)
        self.assertIn('role="dialog"', script)
        self.assertIn('aria-modal="true"', script)
        self.assertIn('event.key === "Escape"', script)
        self.assertIn("native.click()", script)
        self.assertIn("navigator.clipboard.writeText", script)
        self.assertIn("overflow:auto", css)
        self.assertIn("max-width:100%", css)
        self.assertNotIn("font-family:var(--atlas-body-font)!important", css)

    def test_icon_svgs_are_never_enhanced(self) -> None:
        script = Path("services/orchestrator/chat-ui.js").read_text()
        guard = script.split("const isDiagram = svg => {", 1)[1].split("};", 1)[0]
        self.assertIn("summary,button,a", guard)
        self.assertIn("width >= 160 && height >= 80", guard)
        enhancer = script.split("const prepareDiagrams", 1)[1].split("\n", 2)[1]
        self.assertIn("if (!isDiagram(svg)) return;", enhancer)

    def test_user_copy_uses_native_action_without_duplication_or_fallback(self) -> None:
        script = Path("services/orchestrator/chat-ui.js").read_text()
        css = Path("services/orchestrator/chat-ui.css").read_text()
        labels = {
            "en": ("Copy message", "Message copied"),
            "fr": ("Copier le message", "Message copié"),
            "de": ("Nachricht kopieren", "Nachricht kopiert"),
            "es": ("Copiar mensaje", "Mensaje copiado"),
            "it": ("Copia messaggio", "Messaggio copiato"),
        }
        for language, values in labels.items():
            section = script.split(f"    {language}: {{", 1)[1].split(" },", 1)[0]
            for value in values:
                self.assertIn(value, section, f"{language}:{value}")
        self.assertIn("const nativeCopyButton", script)
        self.assertIn("native || bubble.querySelector", script)
        self.assertIn("navigator.clipboard.writeText(content.innerText)", script)
        self.assertIn("setTimeout(() => {", script)
        self.assertIn("}, 1500)", script)
        self.assertIn('aria-live", "polite"', script)
        self.assertIn("width:16px; height:16px", css)
        self.assertIn("@media (hover:none),(pointer:coarse)", css)
