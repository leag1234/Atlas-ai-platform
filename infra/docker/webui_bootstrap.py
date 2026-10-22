"""Install the managed image/document inlet before accepting any accounts.

Source: open-webui v0.11.3 backend/open_webui/{config,models/functions}.py,
read 2026-10-22. Runs inside that pinned image with its own dependencies.
"""

import asyncio
from importlib import import_module
from pathlib import Path


async def main() -> None:
    from open_webui.models.functions import Functions, FunctionForm, FunctionMeta  # type: ignore[import-not-found]  # runs inside the Open WebUI image

    identifier = "atlas_image_references"
    source = Path("/opt/atlas/filter.py").read_text()
    if await Functions.get_function_by_id(identifier) is None:
        result = await Functions.insert_new_function(
            "atlas-managed",
            "filter",
            FunctionForm(
                id=identifier,
                name="Atlas attachments",
                content=source,
                meta=FunctionMeta(description="Atlas managed attachment inlet"),
            ),
        )
        if result is None:
            raise RuntimeError("attachment_inlet_install_failed")
    result = await Functions.update_function_by_id(
        identifier,
        {"content": source, "is_active": True, "is_global": True},
    )
    if result is None:
        raise RuntimeError("attachment_inlet_activation_failed")
    print("Atlas attachment inlet ready.", flush=True)


if __name__ == "__main__":
    # The pinned WebUI configuration runs its own Alembic migrations on import.
    # Do this before opening an event loop or importing the async table models.
    import_module("open_webui.config")

    asyncio.run(main())
