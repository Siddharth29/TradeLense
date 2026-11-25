"""Order execution helpers with retry logic."""

from __future__ import annotations

from typing import Callable, Protocol, Any
from uuid import uuid4


class TransientError(Exception):
    """Error indicating a temporary failure which may succeed on retry."""


class BrokerLike(Protocol):
    def place(self, decision: Any):
        ...


def place_with_retries(
    broker: BrokerLike,
    decision: Any,
    max_retries: int = 2,
    backoff: Callable[[int], float] = lambda n: 0,
):
    """Place *decision* using *broker* retrying on transient errors.

    Parameters
    ----------
    broker:
        Object providing a ``place`` method which accepts *decision*.
    decision:
        Decision to submit. If ``decision.client_order_id`` is missing a new
        UUID will be assigned to ensure idempotency across retries.
    max_retries:
        Maximum number of retry attempts after the initial failure.
    backoff:
        Callable invoked with the retry number (starting from 1) that can be
        used by callers to implement sleeping/backoff strategies. The returned
        value is ignored and no sleeping is performed by this function.

    Returns
    -------
    Order from ``broker.place``.

    Raises
    ------
    Exception
        The last encountered exception if the operation does not succeed
        within ``max_retries`` attempts.
    """

    if getattr(decision, "client_order_id", None) is None:
        decision.client_order_id = str(uuid4())

    attempt = 0
    while True:
        try:
            return broker.place(decision)
        except TransientError as exc:
            attempt += 1
            if attempt > max_retries:
                raise
            backoff(attempt)


__all__ = ["TransientError", "place_with_retries"]
