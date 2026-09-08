# Development

## Setup

```bash
git clone https://github.com/SpikeForsythe/stock-market-game
cd stock-market-game
./install.sh --dev          # editable install + server extras + test tooling
```

Then, in two terminals:

```bash
make server                 # fast clock: 1s ticks, 5-minute simulated days
make client                 # connects to 127.0.0.1:8765
```

`make help` lists everything.

A short `--day-seconds` is the single most useful development setting: day
rollover drives daily candles, portfolio snapshots, the daily/weekly leaderboards
and earnings, and you do not want to wait an hour to exercise any of it.

### Starting over

```bash
make reset-db               # delete the local world; next start seeds a new one
```

---

## Project layout

```
src/stockgame/
├── shared/       both sides; no third-party dependencies
├── server/       authoritative game world
└── client/       Textual TUI; renders, never decides
```

The dependency rule is one-directional and enforced by review:

```
client  ──▶ shared ◀──  server
```

`shared` must never import from `client` or `server`. That is what keeps the
client installable without FastAPI, SQLAlchemy and argon2.

---

## Tests

```bash
make test                   # 264 tests, ~35s
.venv/bin/pytest tests/test_trading.py -v
.venv/bin/pytest -k "limit_order"
make test-cov               # HTML report in htmlcov/
```

### How the fixtures work

`tests/conftest.py` gives every test a throwaway SQLite file and a fully wired
`GameServer` with the **background loops switched off**. Tests advance the market
explicitly:

```python
async def test_something(game, player, buy):
    await buy(player["user_id"], "ACME", 100)
    await game.tick_once()          # exactly one tick, deterministically
```

Nothing depends on wall-clock timing, so the suite is not flaky.

Useful fixtures: `settings`, `app`, `client` (httpx against the ASGI app), `game`,
`player` / `rival` (registered accounts with auth headers), `buy` / `sell`.

Argon2 cost is turned right down in the test settings; at production cost the auth
tests would take minutes.

### Writing tests

Test through the **service layer** where you can — it is the real API and needs no
network:

```python
result = await game.trading.place_order(
    user_id, OrderRequest.parse({"symbol": "ACME", "side": "buy",
                                 "type": "market", "quantity": 10})
)
```

For WebSocket behaviour, `tests/test_websocket.py` has a `FakeSocket` that drives
the real gateway without a network. One test at the bottom of that file goes
through the genuine ASGI route, to prove the transport is wired up.

To force a price, set it directly — the engine is the source of truth in-process:

```python
game.engine.sims["ACME"].price_cents = int(game.engine.price_of("ACME") * 1.5)
```

---

## Style

```bash
make lint                   # ruff check + format check
make format                 # fix and format
make typecheck              # mypy (advisory)
```

Line length 100. Ruff handles both linting and formatting; CI runs
`ruff format --check`, so run `make format` before pushing.

Conventions that matter more than formatting:

- **Money is integer cents.** Floats are for the price model and chart geometry
  only, and are quantised via `to_cents()` before touching an account.
- **Type hints everywhere**, with `from __future__ import annotations`.
  One exception: `server/api/routes.py`, because FastAPI evaluates route
  annotations at import time and the dependencies there are closures — as PEP 563
  strings they would be unresolvable and every `Depends` would silently become a
  required query parameter. There is a comment at the top of that file saying so.
- **Comment the why, not the what.** `# increment counter` above `count += 1` is
  noise; explaining why a lock exists is not.
- **Never trust the client.** If you are about to read a number from an inbound
  frame and use it in a calculation, stop.

---

## Common tasks

### Add a company

`server/market/universe.py`. It is a `# fmt: off` block on purpose — the table is
meant to be read as columns.

```python
Listing("WISP", "Wisp Aerospace", Sector.INDUSTRIAL, 88.40, 0.52, 0.12, 1.35, 0.61,
        _m(120), "Small-satellite launch vehicles."),
```

Genesis values only apply to a **fresh** world; an existing database is
authoritative. Run `make reset-db` to see it.

### Add a news template

`server/market/news.py`. `{name}`, `{symbol}` and `{sector}` are substituted.
Impact is a log-return *range*; the sign comes from the sentiment, so give a
positive range on both.

### Add an achievement

`server/services/achievements.py` — add a `Badge` with a predicate over the
portfolio payload. Definitions live in code, unlocks in the database, so no
migration is needed and rules stay re-evaluable.

### Add a WebSocket message

1. Add the type to `ClientMessage` / `ServerMessage` in `shared/protocol.py`.
2. Write a `_handle_*` method on `WebSocketGateway`.
3. Register it in the `_HANDLERS` map at the bottom of `api/websocket.py`.
4. Handle it in the client's `_wire()`.

Bump `PROTOCOL_VERSION` only for a **breaking** change; the server rejects
handshakes from a different version.

### Add a screen

1. A view class in `client/screens/views.py` with `refresh_view(state)`.
2. Add it to `DashboardScreen.compose()`, `VIEW_IDS` and `NAV_ITEMS`.
3. A `Binding` for its shortcut key.

### Change the schema

```bash
# edit server/db/models.py, then:
make migration m="add dividend column"
# review the generated file in migrations/versions/ -- always read it
make migrate
```

CI runs `alembic check` and fails if the models and migrations disagree.

---

## Debugging the TUI

A TUI cannot print to its own stdout. Options:

**Textual's dev console** — run in one terminal, the app in another:

```bash
.venv/bin/textual console
.venv/bin/textual run --dev -c stockgame
```

Then `self.log(...)` from anywhere in the app.

**Drive it headlessly.** This is how the UI was developed and is the fastest way
to reproduce a bug:

```python
import asyncio
from stockgame.client.app import StockGameApp
from stockgame.client.config import ClientConfig

async def main():
    app = StockGameApp(ClientConfig.load())
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause(3)
        await pilot.press("m")
        await pilot.pause(0.5)
        print(app.state.portfolio)

asyncio.run(main())
```

**Capture the rendered screen as text**, which is invaluable for layout work:

```python
comp = app.screen._compositor
for y in range(45):
    print("".join(seg.text for seg in comp.render_strips()[y]).rstrip())
```

Set `STOCKGAME_CONFIG_DIR=/tmp/whatever` so experiments do not clobber your real
config.

### Textual gotchas

- **Do not shadow Textual's attribute names.** `name`, `_context`, `id`, `styles`,
  `parent`, `screen`, `log` and friends belong to `Widget`/`MessagePump`.
  `TradeDialog` uses `company_name` for exactly this reason.
- **`ContentSwitcher` children need `id`s**, and the ids are what you switch on.
- **Timers keep the app "busy."** `pilot.press` waits for pending messages, so a
  fast repeating timer can trip `WaitForScreenTimeout` in tests. The tape ticker
  skips work when its screen is not on top, partly for this reason.

---

## Tuning the market

The knobs are the constants at the top of `server/market/pricing.py`. They are
interdependent — change one at a time and re-run `pytest tests/test_market.py`,
which asserts realised volatility, step-size invariance, beta coupling and mean
reversion.

| Constant | Effect if raised |
|---|---|
| `MOMENTUM_WEIGHT` | Stronger trends. Above ~0.4 it stops being momentum and becomes a runaway |
| `REVERSION_SCALE` | Prices snap back to fair value faster; too high and everything tracks its drift |
| `FAIR_VALUE_VOL_SHARE` | More long-run dispersion between winners and losers |
| `EVENT_DECAY` | News shocks unwind more slowly |
| `VOL_CLUSTER_REACTION` | Volatility clusters harder after big moves |
| `MAX_TICK_RETURN` | Larger single-tick jumps; it is a circuit breaker |

A quick way to eyeball a change:

```bash
.venv/bin/python -c "
import random, statistics
from stockgame.server.market import pricing
from stockgame.server.market.pricing import MarketSim, StockSim
rng, mkt = random.Random(7), MarketSim()
sim = StockSim('T','Technology',10000,10000,0.38,0.14,1.25,0.95,0.8)
start = sim.price_cents
for _ in range(252):
    pricing.step_stock(sim, mkt, pricing.market_factor(mkt, rng, 1/252), rng, 1/252)
print(f'one year: {start/100:.2f} -> {sim.price_cents/100:.2f}')"
```

Execution feel lives in `server/services/execution.py`: `MIN_HALF_SPREAD`,
`IMPACT_COEFFICIENT`, `PERMANENT_IMPACT_SHARE`, `BASE_DEPTH_SHARES`.

---

## Pull requests

1. `make test lint` must be green.
2. Add a test with any behaviour change.
3. If you touched the schema, commit the migration.
4. If you changed the protocol, update both sides and `docs/ARCHITECTURE.md`.

CI runs the suite on Python 3.10–3.13 on Linux plus macOS and Windows, checks
lint and formatting, verifies migrations match the models, and builds and
smoke-tests the Docker image.
