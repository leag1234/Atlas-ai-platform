"""Install the owned attachment inlet after the first administrator signs up.

Source: open-webui v0.11.3 backend/open_webui/models/{users,functions}.py,
read 2026-10-22. Runs inside that pinned image with its own dependencies.
"""

import asyncio
from pathlib import Path
import urllib.request


async def main() -> None:
    while True:
        try:
            with urllib.request.urlopen("http://127.0.0.1:13730/health", timeout=2):
                break
        except (OSError, TimeoutError):
            await asyncio.sleep(2)
    from open_webui.models.functions import Functions, FunctionForm, FunctionMeta
    from open_webui.models.users import Users

    while (user := await Users.get_first_user()) is None:
        await asyncio.sleep(2)
    identifier = "atlas_image_references"
    source = Path("/opt/atlas/filter.py").read_text()
    if await Functions.get_function_by_id(identifier) is None:
        result = await Functions.insert_new_function(
            user.id,
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


asyncio.run(main())
