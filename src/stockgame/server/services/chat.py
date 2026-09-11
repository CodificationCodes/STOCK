"""Global chat.

One room, everyone in it. Messages are persisted so a player who connects
mid-conversation sees the last few lines rather than an empty panel, and
so there is an audit trail if moderation is ever needed.

The body is the only thing a client controls, and it is treated as hostile:
length-capped, stripped of control characters, and never interpreted.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

from sqlalchemy import select

from stockgame.server.core.events import Event, EventBus, Topics
from stockgame.server.db.models import ChatMessage
from stockgame.server.db.session import Database
from stockgame.shared.validation import ValidationError

log = logging.getLogger("stockgame.chat")

MAX_LENGTH = 240


def clean_body(raw: Any) -> str:
    """Normalise a message body, or raise if nothing usable is left.

    Control characters are dropped rather than escaped: a terminal client
    renders this text, and an embedded escape sequence must never reach it.
    """
    if not isinstance(raw, str):
        raise ValidationError("A message must be text.")
    text = "".join(
        ch if not unicodedata.category(ch).startswith("C") or ch == " " else " " for ch in raw
    )
    text = " ".join(text.split())
    if not text:
        raise ValidationError("Say something first.")
    if len(text) > MAX_LENGTH:
        raise ValidationError(f"Keep it under {MAX_LENGTH} characters.")
    return text


class ChatService:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus

    async def post(self, user_id: int, username: str, raw: Any) -> dict[str, Any]:
        body = clean_body(raw)
        async with self.db.write_session() as session:
            message = ChatMessage(user_id=user_id, username=username, body=body)
            session.add(message)
            await session.flush()
            payload = self._payload(message)
        await self.bus.publish(Event(Topics.CHAT_POSTED, payload))
        return payload

    async def recent(self, limit: int = 40) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 40), 200))
        async with self.db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatMessage).order_by(ChatMessage.created_at.desc()).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [self._payload(row) for row in reversed(rows)]

    @staticmethod
    def _payload(message: ChatMessage) -> dict[str, Any]:
        return {
            "id": message.id,
            "username": message.username,
            "body": message.body,
            "at": message.created_at.isoformat(),
        }
