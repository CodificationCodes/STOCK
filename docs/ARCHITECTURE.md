# Architecture

## The one rule

**The server owns the truth. The client owns the pixels.**

Everything else in this document follows from that. A player's terminal is on
their machine, running code they can read and modify, so it is treated as
hostile. It may *ask* for things; it may not *assert* anything.

| The client may say | The client may never say |
|---|---|
| "buy 100 ACME at market" | "ACME costs $1.00" |
| "cancel order `x7Kd…`" | "my balance is $500,000" |
| "show me 1Y candles for NOVA" | "I own 10,000 shares" |
| "add QNTM to my watchlist" | "that trade made me $50,000" |

Concretely, `TradingService.place_order` accepts a `user_id` (taken from the
authenticated socket, never from the frame) and an `OrderRequest` containing only
symbol, side, type, quantity and an optional limit price. Everything else in the
inbound frame is discarded. There is a test that sends `price`, `cash_cents`,
`user_id`, `status` and `realized_pl_cents` in an order and asserts none of them
had any effect.

---

## Layers

```
        ┌──────────────────────────────────────────────┐
        │  client/  — Textual TUI                      │
        │  renders ClientState; computes chart geometry │
        └───────────────────┬──────────────────────────┘
                            │  WebSocket (JSON frames)
                            │  REST only for register/login
        ┌───────────────────┴──────────────────────────┐
        │  server/api/  — transport                    │
        │  routes.py · websocket.py · hub.py            │
        └───────────────────┬──────────────────────────┘
                            │  plain Python calls
        ┌───────────────────┴──────────────────────────┐
        │  server/services/  — ALL game rules           │
        │  auth · trading · execution · portfolio       │
        │  leaderboard · achievements · market_data     │
        └───────────────────┬──────────────────────────┘
                            │
        ┌──────────────┬────┴─────────┬─────────────────┐
        │ server/market│  server/db   │  server/core    │
        │ the simulation│ persistence │ security, events│
        └──────────────┴──────────────┴─────────────────┘
```

The transport layer contains no game logic. `websocket.py` parses a frame,
resolves the acting user from the connection, calls a service and serialises the
result. If you deleted it, the game would still be fully playable through the
services — which is exactly how most of the test-suite drives it.

### `shared/`

The only code both sides import. It has **no third-party dependencies**, which is
what lets the client be installed without the server's stack.

- `protocol.py` — frame envelope, message types, channel names, error codes.
- `money.py` — integer-cent arithmetic and display formatting.
- `validation.py` — the input rules. The client runs them for instant feedback;
  the server runs the *same functions* to actually enforce them. Client-side
  validation is a convenience, never a gate.
- `enums.py` — order sides, statuses, regimes, timeframes.

---

## The market engine

`server/market/` is deliberately split so the model can be tested without a
database and replaced without touching anything else.

| Module | Responsibility | Depends on |
|---|---|---|
| `pricing.py` | The price process. Pure functions, plain dataclasses. | nothing |
| `universe.py` | The 47 companies and their genesis parameters. | nothing |
| `news.py` | Headline templates and the generator. | nothing |
| `engine.py` | World clock, candles, persistence, news scheduling. | all three + the DB |

Because `pricing.py` is pure, the whole model can be replayed deterministically
from a seed — which is how the statistical tests (realised volatility, step-size
invariance, beta coupling, mean reversion) work.

### In-memory authority, periodic durability

Only one process mutates prices, so live state lives in memory and is flushed to
the database on candle boundaries and day rollovers. A 2-second tick across 47
symbols therefore costs no I/O at all in the common case, and a crash loses at
most one candle.

### Two candle resolutions

| Interval | Backs | Retention |
|---|---|---|
| `'i'` intraday | the 1D chart | pruned after 2 simulated days |
| `'d'` end-of-day | 1W / 1M / 3M / 1Y | kept forever |

Candles are downsampled **server-side** to the number of columns the client asked
for. A 1Y chart is ~250 bars but a terminal is 80 columns wide; sending all 250
would be three times the bytes for no extra pixels. `_downsample` preserves true
OHLC — first open, max high, min low, last close, summed volume.

### Time

A *simulated day* is `STOCKGAME_DAY_SECONDS` of wall clock (default one hour).
At default settings that is 1,800 ticks per day and 120 intraday candles. A
simulated year (252 days) takes about ten and a half real days.

The tick loop is drift-corrected: it sleeps until the *next scheduled* tick, not
for a fixed interval, so a slow tick does not push the simulated clock
permanently behind.

---

## Execution

`services/execution.py` defines a narrow `ExecutionVenue` protocol:

```python
def quote(symbol, side, quantity) -> int
def fill_market(symbol, side, quantity) -> Fill | None
def available_size(symbol) -> int
```

`SimulatedVenue` implements it against the engine price. It models the things
that change how the game *plays*:

- **spread** — 5 bp on the most liquid names, up to 30 bp on the thinnest;
- **slippage** — square-root in size, the standard empirical shape, so doubling
  your order costs ~1.4× more rather than 2×;
- **permanent impact** — ~35% of the concession sticks, so a large order visibly
  moves the tape for everyone else. This is what makes the market feel shared;
- **partial fills** — a resting order fills at most a randomised slice of depth
  per tick, so identical orders do not fill in lockstep.

Replacing this with a real central limit order book means implementing the same
three methods against a book. Nothing in `TradingService` would change.

### Money invariants

Enforced on every path in `services/trading.py`:

1. `portfolio.cash_cents` never goes negative — also a database `CHECK`.
2. Cash for a resting buy is **deducted at placement** and refunded on cancel or
   over-reservation, so the same dollar cannot back two orders.
3. Shares backing a resting sell are **reserved on the holding**, so the same
   share cannot be sold twice.
4. Cost basis is stored as a *total*, and relieved proportionally on a sell, so
   average-price rounding can never manufacture or destroy value.
5. Money is integer cents everywhere. Floats appear only in the price model and
   in chart geometry, and are quantised before touching an account.

Invariants 2 and 3 are why the schema has `orders.reserved_cash_cents` and
`holdings.reserved_quantity` rather than computing availability on the fly.

---

## Real-time fan-out

A naive server broadcasts every price to every client several times a second.
With 47 symbols and a 2-second tick that is a lot of JSON for someone staring at
one chart. Instead, clients subscribe to channels:

| Channel | Contents | Sent to |
|---|---|---|
| `market` | compact `{s,p,c,v,h,l}` array for all symbols | market-screen viewers |
| `stock:ACME` | detail + live candle for one symbol | whoever is on that page |
| `portfolio` | your own cash/holdings/P&L | you only |
| `leaderboard` | rankings (+ *your* rank, per connection) | leaderboard viewers |
| `news` | headlines | news viewers, plus the relevant `stock:` channel |
| `tape` | anonymised global fills | tape viewers |
| `status` | market open/closed, regime, day rollover | anyone who asks |

Additional discipline in `api/hub.py`:

- Per-symbol updates go out only for symbols someone is actually watching —
  usually one or two of 47.
- Portfolio pushes are throttled (`PORTFOLIO_THROTTLE`) and coalesced, so a burst
  of fills produces one refresh rather than one per fill.
- Each connection has a bounded send queue and its own writer task. A client that
  stops reading is **dropped**, not buffered forever — one stalled terminal
  cannot slow the tick loop.

Price fan-out goes through `GameServer.tick_listeners` rather than the event bus:
it fires 30× a minute and is routed by subscription, not by topic.

---

## Authentication

- **Passwords** → Argon2id via `argon2-cffi`, with automatic rehash when the cost
  parameters change. No cryptography is implemented here.
- **Sessions** → 256-bit random tokens. Only the SHA-256 digest is persisted, so a
  database leak does not hand an attacker working sessions. (A slow KDF would be
  pointless: the token already has full entropy.)
- **Login timing** → an unknown username still burns an Argon2 verification, so
  response time does not reveal whether an account exists. The error message is
  identical either way, and there is a test asserting the two responses match.
- **WebSocket auth** → the token goes in the first `hello` *frame*, never the
  query string, where proxies would log it.
- **Password change** → bumps `token_epoch` and deletes every session, logging
  all other devices out.

Rate limiting is a sliding window keyed by client IP (auth) or user id (orders).
`X-Forwarded-For` is only trusted when `STOCKGAME_TRUST_PROXY_HEADERS=true`,
because trusting it unconditionally would let anyone bypass the limit with a
forged header.

Admin routes return **404** to non-admins rather than 403, so an unauthorised
caller learns nothing about what exists.

---

## Persistence

SQLAlchemy 2.0 async models. Every column type maps cleanly onto both SQLite and
PostgreSQL, and no backend-specific SQL is used anywhere, so
`STOCKGAME_DATABASE_URL` is genuinely a configuration change rather than a port.

Two details worth knowing:

- **`UTCDateTime`** — SQLite drops `tzinfo`, so naive datetimes come back and
  comparisons against aware ones raise. This `TypeDecorator` normalises to UTC on
  the way in and re-attaches it on the way out, on every backend.
- **`Database.write_session()`** — SQLite is a single-writer database, so all
  write transactions are funnelled through a process-wide lock. On PostgreSQL the
  lock is skipped automatically (`needs_write_lock`). WAL mode plus a busy
  timeout keeps readers unblocked.

Schema is managed with Alembic; CI runs `alembic check` and fails the build if the
models have drifted from the migrations.

---

## Client

```
StockGameApp (Textual App)
├── owns GameConnection, ClientConfig and ClientState
├── WelcomeScreen → CredentialsScreen → NewAccountScreen
└── DashboardScreen
    ├── TopBar / NavBar / StatusBar / TickerTape
    └── ContentSwitcher → Market · Stock · Portfolio · Watchlist
                          Orders · Leaderboard · News · Profile
```

`GameConnection` multiplexes one socket: a request gets an id and returns a future
that resolves when the matching `ref` arrives, while unsolicited pushes are routed
to handlers. So the UI can `await connection.request(...)` without blocking the
live price stream. It reconnects on its own with exponential backoff and jitter,
and re-subscribes to whatever the UI had open.

`ClientState` is a plain mirror. It performs **no financial calculation** — every
currency figure in it arrived pre-computed. The only maths the client does is
chart geometry.

### Rendering without flicker

Two decisions do most of the work:

1. **Tables update cell by cell.** `KeyedTable.sync()` diffs against the last
   rendered cells and only pushes what changed. Most cells (symbol, company,
   sector) never change, which turns a 47×7 refresh into a handful of updates —
   and keeps the cursor where the player left it.
2. **Off-screen views are not rendered.** `refresh_view()` marks hidden views
   dirty and redraws them on switch, so a price tick repaints one table, not eight.

### Charts

`client/widgets/chart.py`, pure text, no images:

- **Candlesticks** — one column per bar, using `│` for wicks and `█`/`▀`/`▄` for
  bodies, so a body can start or end mid-cell. That doubles vertical resolution.
  When there are fewer bars than columns, candles widen to fill the space.
- **Line** — Braille (U+2800–U+28FF) at 2×4 sub-cell resolution, roughly eight
  times the detail of block characters in the same box, drawn with Bresenham.
- Both take an optional `reference` (previous close) drawn as a dashed line, and
  an optional volume strip beneath.

---

## Extending it

The interfaces below are where the seams already are.

| Feature | Where to start |
|---|---|
| **Real market data** | Implement a `MarketEngine`-shaped object that polls a provider. `PortfolioService`, `TradingService` and the API only use `prices()`, `price_of()`, `sims`, `stock_ids` and `day_bar()`. |
| **Proper matching engine** | Implement the `ExecutionVenue` protocol against a real book. `TradingService` is unchanged. |
| **Stop / stop-limit orders** | `OrderType` already has the members and the order pipeline routes on it; add a trigger rule in `process_resting_orders`. |
| **Short selling** | Allow negative `Holding.quantity`, add a margin requirement to `_authorise_sell`, and a borrow cost to the day rollover. |
| **Dividends** | A day-rollover hook that credits `cash_cents` per share held. The rollover already iterates every stock. |
| **Options / crypto** | New instrument tables plus a second `ExecutionVenue`; the protocol's `symbol` field is already free-form. |
| **AI traders** | A background task that calls `TradingService.place_order` with a bot user id. Bots would appear on the tape and the leaderboard for free. |
| **Private leagues** | A `league_id` on `LeaderboardEntry` and a filter in `LeaderboardService._rank`. |
| **Chat** | A new `Channel` and a `ClientMessage`; the hub's fan-out already supports it. |
| **Web spectator client** | The REST API and the same WebSocket protocol are transport-agnostic; enable `STOCKGAME_CORS_ORIGINS`. |
| **Horizontal scaling** | `RateLimiter` and `ConnectionHub` are the only per-process state. Both are behind interfaces; move them to Redis and run the engine as a single writer. |
