"""One daily-budget bucket per person, whichever way their request came in.

The interface account id is the person's budget bucket: the chat reads it from
the interface's forwarded headers, dictation from the signed-in user, and an API
key spends on it (its budget_user). Project, Work and scheduled requests carry
the verified email instead, so each person had two buckets and two daily caps
(staging 2026-11-16: the probe account's spend was split between its account id
and its email).

An email is mapped to the account id the interface asserted with it: the ledger
records the pair from a chat request (the interface forwards both) and from a
signed-in dictation session. Where the host's user directory is configured
(key_subject.resolve_login, as for API key creation), it answers for a person
never seen yet. Otherwise the email stays the bucket: spend is never dropped,
only not merged until the person has used the chat once.
"""

from __future__ import annotations

import logging
import time

from services.orchestrator.daily_budget import DailyLedger
from services.orchestrator.key_subject import SubjectError, resolve_login

RESOLVED_SECONDS = 600.0
UNRESOLVED_SECONDS = 60.0
_cache: dict[str, tuple[float, str]] = {}


async def person_bucket(subject: str, ledger: DailyLedger | None = None) -> str:
    """The ledger bucket of the person behind a verified project subject."""
    key = subject.strip().casefold()
    if "@" not in key:
        return subject
    known = (ledger or DailyLedger()).alias(key)
    if known is not None:
        return known
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        email, identifier = await resolve_login(key)
    except SubjectError as exc:
        if exc.code != "user_directory_unconfigured":
            logging.getLogger(__name__).warning(
                "budget_identity_unresolved code=%s", exc.code
            )
        _cache[key] = (now + UNRESOLVED_SECONDS, subject)
        return subject
    bucket = identifier if email.strip().casefold() == key else subject
    _cache[key] = (now + RESOLVED_SECONDS, bucket)
    return bucket
