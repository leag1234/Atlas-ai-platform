"""Opt-in pipeline preserves the worker HTTP contract and token rejection."""

import importlib
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image
import image_worker


class PipelineConfigTests(unittest.TestCase):
    def test_configured_pipeline_uses_text_prompt_and_framed_token_limit(self) -> None:
        ready = Event()
        servers: list[ThreadingHTTPServer] = []
        modules = MagicMock()
        modules.cuda.get_device_properties.return_value.total_memory = 24 * 1024**3
        pipeline = modules.Flux2KleinPipeline.from_pretrained.return_value
        pipeline.to.return_value = pipeline
        pipeline.tokenizer.apply_chat_template.return_value = "framed prompt"
        pipeline.tokenizer.return_value = {"input_ids": [1, 2, 3]}
        pipeline.return_value.images = [Image.new("RGB", (1024, 1024), "blue")]
        legacy = modules.FluxPipeline.from_pretrained.return_value
        legacy.return_value.images = pipeline.return_value.images

        def server(
            address: object, handler: type[BaseHTTPRequestHandler]
        ) -> ThreadingHTTPServer:
            result = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            servers.append(result)
            ready.set()
            return result

        config = {
            "repository": "fixture",
            "revision": "pinned",
            "pipeline": "flux2-klein",
        }
        with (
            patch.object(Path, "read_text", return_value=json.dumps(config)),
            patch.object(importlib, "import_module", return_value=modules),
            patch.object(image_worker, "ThreadingHTTPServer", side_effect=server),
        ):
            thread = Thread(target=image_worker.main, daemon=True)
            thread.start()
            self.assertTrue(ready.wait(5))
            try:
                url = f"http://127.0.0.1:{servers[0].server_port}/generate"
                with urlopen(
                    Request(url, data=b'{"prompt":"a blue cube","seed":42}'), timeout=5
                ) as response:
                    result = json.load(response)
                self.assertEqual(result["seed"], 42)
                self.assertEqual(result["steps"], 4)
                self.assertTrue(result["image"].startswith("data:image/png;base64,"))
                modules.Flux2KleinPipeline.from_pretrained.assert_called_once_with(
                    "fixture", revision="pinned", torch_dtype=modules.bfloat16
                )
                pipeline.to.assert_called_once_with("cuda")
                pipeline.enable_sequential_cpu_offload.assert_not_called()
                pipeline.tokenizer.apply_chat_template.assert_called_once_with(
                    [{"role": "user", "content": "a blue cube"}],
                    tokenize=False,
                    add_generation_prompt=True,
                    enable_thinking=False,
                )
                pipeline.tokenizer.assert_called_once_with(
                    "framed prompt", truncation=False
                )
                self.assertEqual(pipeline.call_args.args, ())
                options = pipeline.call_args.kwargs
                self.assertEqual(options["prompt"], "a blue cube")
                self.assertEqual(options["guidance_scale"], 1.0)
                self.assertEqual(options["max_sequence_length"], 512)
                self.assertEqual((options["height"], options["width"]), (1024, 1024))
                pipeline.tokenizer.return_value = {"input_ids": list(range(513))}
                with self.assertRaises(HTTPError) as refused:
                    urlopen(
                        Request(url, data=b'{"prompt":"short raw text"}'), timeout=5
                    )
                self.assertEqual(refused.exception.code, 413)
                self.assertEqual(
                    json.load(refused.exception),
                    {"measured": 513, "limit": 512, "unit": "tokens"},
                )
                refused.exception.close()
                self.assertEqual(pipeline.call_count, 1)
            finally:
                servers[0].shutdown()
                thread.join(5)
                servers[0].server_close()

    def test_unknown_pipeline_is_rejected_before_loading_weights(self) -> None:
        modules = MagicMock()
        modules.cuda.get_device_properties.return_value.total_memory = 24 * 1024**3
        with (
            patch.object(
                Path,
                "read_text",
                return_value=json.dumps(
                    {
                        "repository": "fixture",
                        "revision": "pinned",
                        "pipeline": "unknown",
                    }
                ),
            ),
            patch.object(importlib, "import_module", return_value=modules),
            patch.object(image_worker, "ThreadingHTTPServer"),
        ):
            with self.assertRaisesRegex(ValueError, "unsupported_image_pipeline"):
                image_worker.main()
            modules.FluxPipeline.from_pretrained.assert_not_called()
