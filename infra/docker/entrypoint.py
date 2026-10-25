"""Compose entrypoints reuse the public services without host provisioning."""

import logging
import os
import sys


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    service = sys.argv[1]
    if service == "gateway":
        from gateway_cpu import CPUModels
        from http_gateway import serve

        with serve(CPUModels(), port=13710, host="0.0.0.0") as server:
            server.serve_forever()
    elif service == "devapi-health":
        from services.orchestrator.dev_health import main as check_devapi

        check_devapi()
    elif service == "devapi":
        import uvicorn

        uvicorn.run(
            "services.orchestrator.devapi:app",
            host="0.0.0.0",
            port=13731,
            access_log=False,
        )
    elif service == "retrieval":
        import uvicorn

        from services.retrieval.store import Store

        # Project schemas are installed explicitly by an operator. Runtime uses
        # atlas_project_app, which must never attempt schema creation.
        if not os.environ.get("ATLAS_PROJECT_DSN"):
            Store(os.environ["ATLAS_RETRIEVAL_DSN"]).initialize()
        uvicorn.run(
            "services.retrieval.api:app", host="0.0.0.0", port=13711, access_log=False
        )
    elif service == "orchestrator":
        import uvicorn

        uvicorn.run(
            "services.orchestrator.chat_api:app",
            host="0.0.0.0",
            port=13720,
            access_log=False,
        )
    else:
        raise ValueError("unknown_container_service")


if __name__ == "__main__":
    main()
