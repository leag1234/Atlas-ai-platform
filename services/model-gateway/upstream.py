"""Dropping a provider response that will not be read to its end."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import aiohttp


def abort(response: aiohttp.ClientResponse) -> None:
    """Close the provider connection now, so that generation and billing stop.

    Releasing an unread response only closes its connection gracefully: over
    TLS, asyncio then waits for the provider's close_notify, a wait that never
    ends once the gateway's per-request event loop has finished. Measured on
    2026-11-14 on the gateway's own transport: a provider stream cancelled
    after 4 s stayed connected for more than 10 s (until garbage collection)
    when the loop ended with the request, and closed within 0.2 s when the
    loop kept running. Aborting closes the socket at once in both cases.
    """
    # A response already released has no connection left to drop.
    connection = getattr(response, "connection", None)
    transport = getattr(connection, "transport", None)
    if isinstance(transport, asyncio.Transport):
        transport.abort()


@contextmanager
def abort_if_abandoned(response: aiohttp.ClientResponse) -> Iterator[None]:
    """Abort the response when reading it stops early, for any reason.

    Cancellation, a deadline, a decoder limit, or a consumer that stops reading
    (GeneratorExit) all leave the provider generating otherwise.
    """
    try:
        yield
    except BaseException:
        abort(response)
        raise
