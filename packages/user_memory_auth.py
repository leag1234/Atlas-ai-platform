"""Internal service credential selection; never accepts public request JSON."""

import os


def service_secret() -> str:
    return next(
        (
            os.environ[k]
            for k in (
                "ATLAS_USER_MEMORY_SECRET",
                "ATLAS_PROJECT_IDENTITY_SECRET",
                "ATLAS_PROXY_SECRET",
            )
            if os.environ.get(k)
        ),
        "",
    )
