"""Real GPU image through the gateway and chat pipeline; synthetic prompt only."""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from threading import Thread

from gateway_cpu import CPUModels
from http_gateway import serve
from packages.images import image_info
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.image_store import generated_image


def main() -> None:
    target = Path("BRAIN/eval/imagegen.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    with serve(CPUModels(), 0) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            os.environ["ATLAS_GATEWAY_URL"] = f"http://127.0.0.1:{server.server_port}"
            item = Interaction()
            request = ChatRequest.model_validate(
                {
                    "model": "atlas-qwen",
                    "messages": [
                        {
                            "role": "user",
                            "content": "Generate an image of a red cube on a blue table, plain white background, studio photograph.",
                        }
                    ],
                }
            )
            asyncio.run(process(request, item))
            assert item.state == "done" and item.task_type == "imagegen"
            assert item.modele_utilise == "local" and 0 < item.cout_eur <= 0.05
            url = item.reponse.partition("](")[2].removesuffix(")")
            response = generated_image(url.rsplit("/", 1)[1])
            assert response.status_code == 200
            raw = bytes(response.body)
            metadata = image_info(
                "data:image/png;base64," + base64.b64encode(raw).decode()
            )
            assert metadata["width"] == metadata["height"] == 1024
            Path("BRAIN/eval/imagegen.png").write_bytes(raw)
            report = {
                "image_produced": True,
                "served_locally": True,
                "metadata": metadata,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "cost_eur": item.cout_eur,
                "latency_ms": item.latence_ms["generation"],
            }
            target.write_text(json.dumps(report, indent=2))
            print(json.dumps(report))
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    main()
