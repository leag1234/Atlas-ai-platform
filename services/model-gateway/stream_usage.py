"""Dependency-free validation shared by the gateway and installation probe."""


def validate_usage(
    usage: dict[str, object], previous: dict[str, object] | None
) -> None:
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if name == "total_tokens" and name not in usage:
            continue
        count = usage.get(name)
        if type(count) is not int or count < 0:
            raise ValueError("invalid_usage")
        before = (previous or {}).get(name)
        if type(before) is int and count < before:
            raise ValueError("decreasing_usage")
