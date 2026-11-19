"""Safe measured boundary failures; never retain rejected input content."""

import logging
from decimal import Decimal


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
                "unexpected_tool_history_answer",
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


def keep_cost(error: BaseException, spent: Decimal) -> BaseException:
    """Record on a failed call the cost the gateway kept for it (its reservation).

    The gateway's failure answer reports it (reserved_eur): a check whose call
    timed out counts what that call may have consumed (2026-11-19: an expired
    judge call kept about 0.017 EUR that no report showed).
    """
    setattr(error, "spent_eur", spent)
    return error


def kept_cost(error: BaseException) -> Decimal | None:
    """The cost keep_cost recorded on a failure, or None."""
    value = getattr(error, "spent_eur", None)
    return value if isinstance(value, Decimal) and value.is_finite() else None
