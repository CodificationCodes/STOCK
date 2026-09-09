# Stock Market Game

A multiplayer stock market simulator that runs in your terminal. One shared
market — everyone trades against the same prices, and your portfolio is still
there when you come back tomorrow.

> **Virtual currency only.** No real money, no real companies. Everything is
> simulated.

```
  STOCKGAME │ SGX 1,020.47 ▲ +2.05%  │ NEUTRAL  │ ● OPEN  │ DAY 400 ███▌   │ S1 The Opening Bell │ 3 online
   M MARKET │ T TICKER │ P PORTFOLIO │ W WATCHLIST │ O ORDERS │ L LEAGUE │ N NEWS │ U PROFILE │  ? help
 ╭──────────────────────────────────────────────────────────────────╮ SECTORS
 │  MARKET  47 symbols   sorted by SYMBOL                           │╭──────────────────────────────╮
 │  SYM      COMPANY              SECTOR        PRICE    CHANGE     ││ Energy         +3.32% ███████│
 │  ◆ ACME   Acme Technologies    Technology    $227.26   +8.46%    ││ Consumer       +2.61% █████▋ │
 │    AEGS   Aegis Insurance      Finance       $109.32   +1.73%    ││ Technology     +2.20% ████▎  │
 │  · AERO   Aero Dynamics        Industrial    $124.68   -1.52%    │╰──────────────────────────────╯
 │    ATLS   Atlas Robotics       Industrial     $50.90   +3.69%    │ LIVE TAPE
 │    AURM   Aurum Gold           Mining         $71.14   -4.69%    │╭──────────────────────────────╮
 │    BREW   Brew & Co            Consumer       $25.41   -0.08%    ││ nova    BUY  ACME   40  $227 │
 │  · BYTE   Byte Systems         Technology     $28.58   +0.56%    ││ hex     SELL LITH  900   $27 │
 ╰──────────────────────────────────────────────────────────────────╯╰──────────────────────────────╯
  CASH  $73,919.20  VALUE $101,190.40  TODAY +$1,190.40 (+1.19%)  TOTAL +$1,190.40  RANK #1  │ ● live
```

## Requirements

- Python 3.10 or newer
- A terminal that supports Unicode and color (Windows: use **Windows Terminal**, not the legacy console)

## Install and run

Clone the repo and run the installer — it creates a virtual environment and
installs everything you need for both the client and a local server:

```bash
git clone https://github.com/CodificationCodes/STOCK
cd STOCK
./install.sh --server
```

**Start the server** (first run generates the market — takes a few seconds):

```bash
.venv/bin/stockgame-server
```

**In a second terminal, start the game:**

```bash
.venv/bin/stockgame --server 127.0.0.1:8765
```

Press `R` to register. You start with **$100,000**. Press `?` for the key map.

### Just want to play on someone else's server?

You don't need the server stack at all:

```bash
git clone https://github.com/CodificationCodes/STOCK
cd STOCK
./install.sh
.venv/bin/stockgame --server <address-of-server>
```

Save the address so you don't have to type it every time:

```bash
.venv/bin/stockgame --set-server <address-of-server>
.venv/bin/stockgame
```

## Market hours

The market trades **09:00–15:00 Sydney time, Monday to Friday**. Outside the
session it freezes: prices stop moving, the day counter stops advancing, and
orders are rejected until the next open. You can still log in and look around —
the header shows `● CLOSED` and when the market reopens.

Running your own server? The window is configurable, including a 24/7 setting —
see [Trading hours](docs/DEPLOYMENT.md#trading-hours).

## Updating

Pull the latest code and re-run the installer:

```bash
git pull
./install.sh
```

Use `./install.sh --server` instead if you also run a server. The re-run matters
— `install.sh` installs the client into `.venv`, so a `git pull` on its own
leaves you playing the old build.

### "refusing to merge unrelated histories"

If `git pull` fails with that message, your clone predates a history rewrite and
can't be fast-forwarded. Re-clone once and you're back on the normal path above:

```bash
cd ..
rm -rf STOCK
git clone https://github.com/CodificationCodes/STOCK
cd STOCK
./install.sh
```

Your account lives on the server, not on your machine, so nothing in your
portfolio is affected by any of this.

## Controls

| Key | Action | | Key | Action |
|---|---|---|---|---|
| `M` | Market | | `B` | Buy selected |
| `T` | Ticker / stock detail | | `S` | Sell selected |
| `P` | Portfolio | | `C` | Cancel selected order |
| `W` | Watchlist | | `O` | Orders & history |
| `L` | Leaderboard | | `N` | News |
| `U` | Profile | | `?` | Help |
| `Q` | Quit | | `Esc` | Back |

## Tests

```bash
./install.sh --dev
.venv/bin/pytest
```

## Learn more

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the client/server split and price simulation work
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — hosting a server for other people to play on

## Licence

MIT — see [LICENSE](LICENSE).

*A simulation. Virtual currency only. Not investment advice, and not a broker.*
