"""Daily counters. By specification every day boundary is midnight UTC."""

import time
from collections import Counter
from datetime import datetime


def day_of(ts):
    """ISO date (UTC) of a Unix timestamp."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def day_start(day):
    """Unix timestamp of 00:00:00 UTC on ISO date ``day``."""
    return int(time.mktime(time.strptime(day, "%Y-%m-%d")))


def daily_counts(timestamps):
    """Map each UTC day to the number of timestamps that fall on it, sorted by day."""
    return dict(sorted(Counter(day_of(ts) for ts in timestamps).items()))


def window(timestamps, first_day, last_day):
    """Timestamps from 00:00 UTC on ``first_day`` up to the end of ``last_day`` (inclusive)."""
    start = day_start(first_day)
    end = day_start(last_day) + 86400
    return [ts for ts in timestamps if start <= ts < end]
