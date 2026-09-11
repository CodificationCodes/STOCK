"""Global chat.

The message body is the only thing a client controls, so most of this is
about what happens to hostile input before it reaches other players'
terminals.
"""

from __future__ import annotations

import pytest

from stockgame.server.core.events import Topics
from stockgame.server.services.chat import MAX_LENGTH, clean_body
from stockgame.shared.validation import ValidationError


class TestCleaning:
    def test_ordinary_text_passes_through(self):
        assert clean_body("buying the dip on ACME") == "buying the dip on ACME"

    def test_whitespace_is_collapsed(self):
        assert clean_body("  too   many    spaces  ") == "too many spaces"

    def test_terminal_escape_sequences_are_neutralised(self):
        """An embedded escape must never reach another player's terminal."""
        cleaned = clean_body("hello \x1b[31mred\x1b[0m world")
        assert "\x1b" not in cleaned
        assert "hello" in cleaned and "world" in cleaned

    @pytest.mark.parametrize("raw", ["", "   ", "\x00\x07", None, 42])
    def test_nothing_usable_is_rejected(self, raw):
        with pytest.raises(ValidationError):
            clean_body(raw)

    def test_length_is_capped(self):
        with pytest.raises(ValidationError):
            clean_body("x" * (MAX_LENGTH + 1))
        assert len(clean_body("x" * MAX_LENGTH)) == MAX_LENGTH


class TestPosting:
    @pytest.mark.asyncio
    async def test_a_post_is_stored_and_attributed(self, game, player):
        posted = await game.chat.post(player["user_id"], "alice", "gm everyone")

        assert posted["username"] == "alice"
        assert posted["body"] == "gm everyone"
        assert posted["at"]

    @pytest.mark.asyncio
    async def test_a_post_goes_out_on_the_bus(self, game, player):
        seen = []

        async def listener(event):
            seen.append(event.payload)

        game.bus.subscribe(Topics.CHAT_POSTED, listener)
        await game.chat.post(player["user_id"], "alice", "ping")

        assert [line["body"] for line in seen] == ["ping"]

    @pytest.mark.asyncio
    async def test_history_comes_back_oldest_first(self, game, player):
        for text in ("one", "two", "three"):
            await game.chat.post(player["user_id"], "alice", text)

        assert [line["body"] for line in await game.chat.recent()] == ["one", "two", "three"]

    @pytest.mark.asyncio
    async def test_history_is_bounded(self, game, player):
        for index in range(6):
            await game.chat.post(player["user_id"], "alice", f"line {index}")

        recent = await game.chat.recent(limit=4)

        assert [line["body"] for line in recent] == ["line 2", "line 3", "line 4", "line 5"]

    @pytest.mark.asyncio
    async def test_a_bad_body_is_rejected_before_it_is_stored(self, game, player):
        with pytest.raises(ValidationError):
            await game.chat.post(player["user_id"], "alice", "   ")
        assert await game.chat.recent() == []
