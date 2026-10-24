"""Readiness checks the authenticated model-list boundary without creating a key."""

import json
from urllib.error import HTTPError
from urllib.request import urlopen


def main() -> None:
    try:
        with urlopen("http://127.0.0.1:13731/v1/models", timeout=2):
            raise RuntimeError("devapi_authentication_required")
    except HTTPError as exc:
        if (
            exc.code != 401
            or json.loads(exc.read(4096)).get("error", {}).get("type")
            != "authentication_error"
        ):
            raise RuntimeError("devapi_unhealthy") from None


if __name__ == "__main__":
    main()
