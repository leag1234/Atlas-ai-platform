"""Safe measured boundary failures; never retain rejected input content."""

import logging


class LimitError(ValueError):
    def __init__(self, code: str, measured: int, limit: int, unit: str) -> None:
        self.code, self.measured, self.limit, self.unit = code, measured, limit, unit
        self.detail = f"{code}: measured {measured} {unit}; limit {limit} {unit}"
        super().__init__(self.detail)
        logging.getLogger(__name__).warning(self.detail)


class ProviderLimitError(LimitError, RuntimeError):
    """Retain the provider-failure interface while carrying numeric diagnostics."""


class ProviderFailure(RuntimeError):
    """Only bounded diagnostic categories cross the public provider boundary."""

    def __init__(self, code: str, finish_reason: str | None, retries: int) -> None:
        self.code = (
            code
            if code
            in {
                "provider_error",
                "empty_completion",
                "empty_content_after_retry",
                "incomplete_provider_answer",
                "malformed_tool_call",
                "promise_only_final_answer",
                "missing_file_failure",
            }
            else "provider_error"
        )
        self.finish_reason = (
            finish_reason
            if finish_reason in {"stop", "length", "tool_calls"}
            else "not_received"
        )
        self.retries = retries
        self.detail = f"{self.code}; finish_reason={self.finish_reason}; retries={retries} (limit 1)"
        super().__init__(self.detail)


class MalformedToolCall(ProviderFailure):
    """Provider output diagnostics retain only a size, never the invalid name."""

    def __init__(self, measured: int, retries: int = 0) -> None:
        super().__init__("malformed_tool_call", None, retries)
        self.measured = measured
        self.limit = 80
        self.detail = (
            "The provider returned a malformed tool call: invalid name; "
            f"measured {measured} characters; allowed 1..80 characters; " + self.detail
        )
        self.args = (self.detail,)
