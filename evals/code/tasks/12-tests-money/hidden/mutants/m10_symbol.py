"""Money helpers. Amounts are integers in cents."""


def split(total, parts):
    """Split ``total`` cents into ``parts`` integer shares.

    - ``parts`` must be >= 1, otherwise ValueError. ``total`` may be negative.
    - The shares sum exactly to ``total``.
    - Shares differ by at most one cent; the larger shares (in absolute value) come first.
    - Returns a list of length ``parts``.
    """
    if parts < 1:
        raise ValueError("parts must be >= 1")
    sign = -1 if total < 0 else 1
    base, extra = divmod(abs(total), parts)
    return [sign * (base + (1 if i < extra else 0)) for i in range(parts)]


def apply_discount(amount, percent):
    """Return ``amount`` reduced by ``percent`` (0 to 100 inclusive, may be a float).

    The discount is rounded to the nearest cent, halves rounded away from zero, then subtracted.
    A percent outside [0, 100] raises ValueError.
    """
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    from decimal import ROUND_HALF_UP, Decimal

    discount = (Decimal(amount) * Decimal(str(percent)) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return amount - int(discount)


def format_amount(amount, symbol=""):
    """Format cents as ``[-]X,XXX.YY`` with comma thousands separators and two decimals.

    A non-empty ``symbol`` is appended after a single space, e.g. ``"1,234.50 EUR"``.
    Negative amounts have a leading minus before the digits: ``"-0.05"``.
    """
    sign = "-" if amount < 0 else ""
    units, cents = divmod(abs(amount), 100)
    text = f"{sign}{units:,}.{cents:02d}"
    return f"{text}{symbol}"
