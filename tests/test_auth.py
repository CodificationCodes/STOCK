"""Registration, login, sessions and access control."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from stockgame.server.core.errors import AuthError, ConflictError
from stockgame.server.core.security import hash_token
from stockgame.server.db.models import Portfolio, Session, User
from stockgame.shared.validation import ValidationError
from tests.conftest import PASSWORD


class TestRegistration:
    async def test_creates_account_with_starting_cash(self, client, game):
        response = await client.post(
            "/api/auth/register", json={"username": "newbie", "password": PASSWORD}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["username"] == "newbie"
        assert body["starting_cash_cents"] == 10_000_000
        assert body["token"]

        portfolio = await game.portfolios.get_portfolio(1)
        assert portfolio["cash_cents"] == 10_000_000
        assert portfolio["total_value_cents"] == 10_000_000
        assert portfolio["positions"] == []

    async def test_password_is_never_stored_in_plaintext(self, client, game):
        await client.post(
            "/api/auth/register", json={"username": "secretive", "password": PASSWORD}
        )
        async with game.db.session() as session:
            user = await session.scalar(select(User).where(User.username_lower == "secretive"))
        assert PASSWORD not in user.password_hash
        assert user.password_hash.startswith("$argon2id$")

    async def test_username_is_unique_case_insensitively(self, client, player):
        response = await client.post(
            "/api/auth/register", json={"username": "ALICE", "password": PASSWORD}
        )
        assert response.status_code == 409

    async def test_first_account_becomes_admin_and_others_do_not(self, client):
        first = await client.post(
            "/api/auth/register", json={"username": "founder", "password": PASSWORD}
        )
        second = await client.post(
            "/api/auth/register", json={"username": "guest", "password": PASSWORD}
        )
        assert first.json()["is_admin"] is True
        assert second.json()["is_admin"] is False

    @pytest.mark.parametrize(
        "username",
        ["ab", "a" * 21, "has space", "has-dash", "emoji😀", ""],
    )
    async def test_rejects_bad_usernames(self, client, username):
        response = await client.post(
            "/api/auth/register", json={"username": username, "password": PASSWORD}
        )
        assert response.status_code in (400, 422)

    @pytest.mark.parametrize("password", ["short", "12345678", "alllettersonly", ""])
    async def test_rejects_weak_passwords(self, client, password):
        response = await client.post(
            "/api/auth/register", json={"username": "weakling", "password": password}
        )
        assert response.status_code in (400, 422)

    async def test_new_account_gets_a_starter_watchlist(self, game, player):
        watchlist = await game.portfolios.get_watchlist(player["user_id"])
        assert {row["symbol"] for row in watchlist} >= {"ACME", "NOVA"}

    async def test_registration_creates_exactly_one_portfolio(self, game, player):
        async with game.db.session() as session:
            portfolios = (await session.execute(select(Portfolio))).scalars().all()
        assert len(portfolios) == 1


class TestLogin:
    async def test_correct_password_returns_a_token(self, client, player):
        response = await client.post(
            "/api/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        assert response.status_code == 200
        assert response.json()["token"] != player["token"]

    async def test_login_is_case_insensitive_on_username(self, client, player):
        response = await client.post(
            "/api/auth/login", json={"username": "ALICE", "password": PASSWORD}
        )
        assert response.status_code == 200

    async def test_wrong_password_is_rejected(self, client, player):
        response = await client.post(
            "/api/auth/login", json={"username": "alice", "password": "wrong-password-1"}
        )
        assert response.status_code == 401

    async def test_unknown_user_gives_the_same_error_as_a_wrong_password(self, client, player):
        unknown = await client.post(
            "/api/auth/login", json={"username": "nobody", "password": "wrong-password-1"}
        )
        wrong = await client.post(
            "/api/auth/login", json={"username": "alice", "password": "wrong-password-1"}
        )
        # Identical responses: a caller must not be able to enumerate accounts.
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["detail"] == wrong.json()["detail"]

    async def test_suspended_account_cannot_log_in(self, client, game, player):
        async with game.db.write_session() as session:
            user = await session.get(User, player["user_id"])
            user.is_active = False
        response = await client.post(
            "/api/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        assert response.status_code == 403


class TestSessions:
    async def test_token_authenticates_api_calls(self, client, player):
        response = await client.get("/api/auth/me", headers=player["headers"])
        assert response.status_code == 200
        assert response.json()["username"] == "alice"

    async def test_missing_token_is_rejected(self, client):
        assert (await client.get("/api/portfolio")).status_code == 401

    @pytest.mark.parametrize(
        "header",
        ["Bearer nonsense", "Basic abc", "nonsense", "Bearer ", "Bearer " + "x" * 400],
    )
    async def test_invalid_tokens_are_rejected(self, client, header):
        response = await client.get("/api/portfolio", headers={"Authorization": header})
        assert response.status_code in (401, 403)

    async def test_only_the_token_digest_is_persisted(self, game, player):
        async with game.db.session() as session:
            rows = (await session.execute(select(Session))).scalars().all()
        assert len(rows) == 1
        assert rows[0].token_hash == hash_token(player["token"])
        assert player["token"] not in rows[0].token_hash

    async def test_logout_invalidates_the_token(self, client, player):
        assert (await client.post("/api/auth/logout", headers=player["headers"])).status_code == 204
        assert (await client.get("/api/portfolio", headers=player["headers"])).status_code == 401

    async def test_expired_sessions_are_rejected_and_purgeable(self, game, player):
        from datetime import datetime, timedelta, timezone

        async with game.db.write_session() as session:
            row = await session.scalar(select(Session))
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        with pytest.raises(AuthError):
            await game.auth.authenticate(player["token"])
        assert await game.auth.purge_expired_sessions() == 1

    async def test_changing_the_password_ends_every_session(self, game, client, player):
        await game.auth.change_password(player["user_id"], PASSWORD, "brand-new-pass-9")
        assert (await client.get("/api/portfolio", headers=player["headers"])).status_code == 401
        response = await client.post(
            "/api/auth/login", json={"username": "alice", "password": "brand-new-pass-9"}
        )
        assert response.status_code == 200

    async def test_change_password_requires_the_current_one(self, game, player):
        with pytest.raises(AuthError):
            await game.auth.change_password(player["user_id"], "not-the-password", "another-pass-9")


class TestAdminAccess:
    async def test_admin_routes_hidden_from_normal_users(self, client, player, rival):
        # alice registered first, so she is the admin; bob is not.
        assert (await client.get("/api/admin/status", headers=player["headers"])).status_code == 200
        response = await client.get("/api/admin/status", headers=rival["headers"])
        # 404, not 403: a non-admin should not learn that these routes exist.
        assert response.status_code == 404

    async def test_admin_routes_require_authentication(self, client, player):
        assert (await client.get("/api/admin/status")).status_code == 401


class TestRateLimiting:
    async def test_repeated_failures_are_throttled(self, app, client, player):
        app.state.settings.auth_rate_limit = 3
        app.state.game.auth_limiter.limit = 3
        app.state.game.auth_limiter.reset("testclient")

        statuses = []
        for _ in range(6):
            response = await client.post(
                "/api/auth/login", json={"username": "alice", "password": "wrong-password-1"}
            )
            statuses.append(response.status_code)
        assert 429 in statuses

    async def test_a_successful_login_clears_the_throttle(self, app, client, player):
        limiter = app.state.game.auth_limiter
        limiter.limit = 5
        limiter.reset("testclient")
        for _ in range(3):
            await client.post(
                "/api/auth/login", json={"username": "alice", "password": "wrong-password-1"}
            )
        assert (
            await client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        ).status_code == 200
        assert limiter.check("testclient").allowed


class TestValidationHelpers:
    def test_username_is_trimmed_but_case_preserved(self):
        from stockgame.shared.validation import validate_username

        assert validate_username("  Trader_9 ") == "Trader_9"

    @pytest.mark.parametrize("value", ["ab", "x" * 21, "no spaces", "sym$bol"])
    def test_invalid_usernames_raise(self, value):
        from stockgame.shared.validation import validate_username

        with pytest.raises(ValidationError):
            validate_username(value)

    async def test_service_raises_conflict_for_duplicates(self, game, player):
        with pytest.raises(ConflictError):
            await game.auth.register("Alice", PASSWORD)
