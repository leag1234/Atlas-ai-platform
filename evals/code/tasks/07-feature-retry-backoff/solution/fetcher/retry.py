import time

from fetcher.client import TransientError


def call_with_retry(fn, *, attempts=3, base_delay=0.5, max_delay=8.0, retry_on=(TransientError,), sleep=time.sleep):
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for k in range(1, attempts + 1):
        try:
            return fn()
        except retry_on:
            if k == attempts:
                raise
            sleep(min(max_delay, base_delay * 2 ** (k - 1)))
