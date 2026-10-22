import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from packages.ports import atlas_ports
from services.retrieval.pipeline import ingest
from services.retrieval.search import Gateway
from services.retrieval.store import Store


class PipelineTests(TestCase):
    def test_metadata_identity_and_atomic_write(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "doc.md"
            source.write_text("---\ndoc_id: doc\nlangue: fr\n---\nTexte public.")
            gateway = Gateway(atlas_ports().url("gateway"))
            store = Store("unused")
            with (
                patch.object(gateway, "embed", return_value=([[1.0]], "rev")),
                patch.object(store, "sync") as sync,
            ):
                first = ingest(root, store, gateway)
                self.assertEqual(len(first), 1)
                self.assertEqual(first[0].doc_id, "doc")
                self.assertEqual(first[0].langue, "fr")
                self.assertEqual(first, ingest(root, store, gateway))
                source.write_text(source.read_text() + " Changed.")
                changed = ingest(root, store, gateway)
                self.assertNotEqual(first[0].chunk_id, changed[0].chunk_id)
                self.assertEqual(sync.call_count, 3)
                (root / "bad.md").write_text("Missing metadata")
                with self.assertRaisesRegex(ValueError, "metadata"):
                    ingest(root, store, gateway)
                self.assertEqual(sync.call_count, 3)

    def test_sidecar_symlink_empty_and_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = Gateway(atlas_ports().url("gateway"))
            store = Store("unused")
            with (
                patch.object(store, "sync") as sync,
                patch.object(gateway, "embed", return_value=([[1.0]], "rev")),
            ):
                with self.assertRaisesRegex(ValueError, "empty_corpus"):
                    ingest(root, store, gateway)
                path = root / "doc.html"
                path.write_text("<p>Hello</p>")
                path.with_suffix(".html.json").write_text(
                    json.dumps({"doc_id": "doc", "langue": "en"})
                )
                self.assertEqual(ingest(root, store, gateway)[0].text, "Hello")
                (root / "link.md").symlink_to(path)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    ingest(root, store, gateway)
                self.assertEqual(sync.call_count, 1)
