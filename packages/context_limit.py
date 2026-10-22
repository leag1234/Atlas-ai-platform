"""Safe numeric diagnostics for model context rejections."""


class ContextExceeded(ValueError):
    def __init__(self, tokens: int, limit: int) -> None:
        self.tokens, self.limit = tokens, limit
        super().__init__(f"context_exceeded: {tokens} tokens, limit {limit} tokens")
