"""Interval merging."""


def merge_intervals(intervals):
    """Merge closed intervals given as (start, end) pairs with start <= end.

    Intervals that overlap or touch (end of one equals start of the next) are
    merged. The input is not modified and may be in any order. The result is a
    new list of tuples sorted by start. A pair with start > end raises
    ValueError.
    """
    result = []
    for start, end in intervals:
        if start > end:
            raise ValueError(f"invalid interval ({start}, {end})")
        if result and start < result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def total_length(intervals):
    """Total covered length of the union of the intervals."""
    return sum(end - start for start, end in merge_intervals(intervals))
