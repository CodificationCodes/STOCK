"""A tiny async publish/subscribe bus.

Services publish domain events without knowing that WebSockets exist; the
connection hub subscribes and decides who, if anyone, should hear about them.
That indirection is what lets the trading service stay unit-testable with no
network in sight.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[["Event"], Awaitable[None]]


@dataclass(slots=True)
class Event:
    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    #: When set, only this player's connections should receive the event.
    user_id: int | None = None


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._handlers.setdefault(topic, []).append(handler)

    async def publish(self, event: Event) -> None:
        """Deliver to every handler. A failing handler never breaks a trade."""
        handlers = self._handlers.get(event.topic, ())
        if not handlers:
            return
        results = await asyncio.gather(
            *(handler(event) for handler in handlers), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                log.exception("event handler failed for %s", event.topic, exc_info=result)

    def emit_soon(self, event: Event) -> None:
        """Fire-and-forget from a synchronous or latency-sensitive path."""
        task = asyncio.create_task(self.publish(event))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)


# Strong references, so fire-and-forget tasks are not garbage collected
# mid-flight (asyncio only keeps weak references to running tasks).
_BACKGROUND: set[asyncio.Task] = set()


class Topics:
    TRADE_EXECUTED = "trade.executed"
    TAPE_PRINTED = "trade.tape"
    ORDER_UPDATED = "order.updated"
    PORTFOLIO_CHANGED = "portfolio.changed"
    NEWS_PUBLISHED = "news.published"
    LEADERBOARD_UPDATED = "leaderboard.updated"
    MARKET_STATUS = "market.status"
    ACHIEVEMENT_UNLOCKED = "achievement.unlocked"
