"""Business-day arithmetic. Saturday and Sunday are never business days."""

from datetime import timedelta


def is_business_day(day, holidays=()):
    return day.weekday() < 5 and day not in set(holidays)


def count_business_days(start, end, holidays=()):
    """Number of business days in the half-open range [start, end).

    ``holidays`` is an iterable of dates; a holiday only matters when it falls
    on a weekday inside the range, and duplicates count once. Raises
    ValueError if end < start. Returns 0 when start == end.
    """
    if end < start:
        raise ValueError("end before start")
    days = (end - start).days
    weeks, rest = divmod(days, 7)
    count = weeks * 5 + min(rest, 5)
    for holiday in holidays:
        if start <= holiday < end:
            count -= 1
    return count


def add_business_days(start, n, holidays=()):
    """Return the date that is ``n`` business days after ``start`` (n >= 0).

    For n == 0 return ``start`` if it is a business day, otherwise the next
    business day. For n > 0 step forward one business day at a time, so
    add_business_days(friday, 1) is the following Monday.
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    holidays = set(holidays)
    day = start
    while not is_business_day(day, holidays):
        day += timedelta(days=1)
    for _ in range(n):
        day += timedelta(days=1)
        while not is_business_day(day, holidays):
            day += timedelta(days=1)
    return day
