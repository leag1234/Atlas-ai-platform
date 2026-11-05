Transient errors from the transport make `fetcher.Client.get_json` fail immediately. Add retries.

1. Create `fetcher/retry.py` with
   `call_with_retry(fn, *, attempts=3, base_delay=0.5, max_delay=8.0, retry_on=(TransientError,), sleep=time.sleep)`.
   It calls `fn()` up to `attempts` times. After failed attempt number k (1-based) with an exception that is an
   instance of one of `retry_on`, and if attempts remain, it calls `sleep(min(max_delay, base_delay * 2 ** (k - 1)))`
   then tries again. Other exceptions propagate immediately without sleeping. When all attempts fail, the last
   exception is re-raised unchanged. It returns the first successful result. `attempts < 1` raises `ValueError`.
2. `Client(transport, attempts=3, sleep=time.sleep)` uses `call_with_retry` in `get_json`. A response with HTTP
   status 502, 503 or 504 counts as a `TransientError` and is retried; other statuses keep their current behaviour.
   No real sleeping may happen in the tests: `sleep` is injectable.
