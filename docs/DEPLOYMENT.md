# Deployment

Deploying `stocks.example.com` on a Linux server, with Docker (recommended)
or systemd.

**Docker is only ever needed for the server.** Players install the client with
`pip`/`pipx` and never touch a container.

---

## Before you start

You need a Linux host with:

- Docker Engine and the Compose plugin (or Python 3.10+ for the systemd route)
- ports 80 and 443 open
- a DNS `A` record for `stocks.example.com` pointing at the server

The server itself is modest: SQLite, one process, a 2-second tick over 47
symbols. 1 vCPU and 512 MB of RAM comfortably handles dozens of concurrent
players.

---

## Option A — Docker (recommended)

### 1. Clone and configure

```bash
sudo mkdir -p /opt/stockgame
sudo chown "$USER" /opt/stockgame
git clone https://github.com/CodificationCodes/STOCK /opt/stockgame
cd /opt/stockgame

cp .env.example .env
```

Generate a real secret key. **This is the one setting you must not skip** —
without it the server generates an ephemeral key and every player is logged out
on each restart.

```bash
python3 -c "import secrets; print('STOCKGAME_SECRET_KEY=' + secrets.token_urlsafe(48))" >> .env
chmod 600 .env
```

Then open `.env` and check:

```ini
STOCKGAME_TRUST_PROXY_HEADERS=true   # you are behind nginx
STOCKGAME_LOG_JSON=true              # structured logs
STOCKGAME_DEBUG=false                # never true in production
STOCKGAME_DAY_SECONDS=3600           # one simulated day per hour
```

`STOCKGAME_TRUST_PROXY_HEADERS` matters: with it, rate limiting sees real client
IPs instead of throttling everyone as if they were the proxy. Only ever set it
`true` when a proxy you control is actually in front — otherwise anyone can forge
`X-Forwarded-For` and bypass the limit.

### 2. Start the server

```bash
docker compose up -d
docker compose logs -f
```

The first start seeds the world: 47 companies and 400 days of price history. It
takes a few seconds and happens exactly once — the market is persisted after that.

Check it:

```bash
curl http://127.0.0.1:8765/api/health
# {"status":"ok","database":true,"market":"open","symbols":47}
```

The compose file binds to `127.0.0.1:8765`, so this is **not** reachable from the
internet yet. That is deliberate: nginx terminates TLS in front of it.

### 3. nginx and TLS

```bash
sudo apt install nginx certbot python3-certbot-nginx
sudo cp docker/nginx.conf /etc/nginx/sites-available/stocks.example.com
sudo ln -s /etc/nginx/sites-available/stocks.example.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d stocks.example.com
```

The supplied config already contains the piece people usually miss:

```nginx
location /ws {
    proxy_http_version 1.1;
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;      # game sockets are long-lived and quiet
    proxy_buffering off;
}
```

Without the `Upgrade`/`Connection` headers the WebSocket never establishes. Without
the long `proxy_read_timeout`, nginx closes it about once a minute and every
player watches their client reconnect.

### 4. Verify end to end

```bash
curl https://stocks.example.com/api/health
curl https://stocks.example.com/api/info
```

Then from any machine:

```bash
stockgame --server stocks.example.com
```

Register the **first** account yourself — it is automatically the admin.

---

## Option B — systemd, without Docker

```bash
sudo useradd --system --create-home --home-dir /opt/stockgame stockgame
sudo -u stockgame git clone https://github.com/CodificationCodes/STOCK /opt/stockgame
cd /opt/stockgame

sudo -u stockgame python3 -m venv .venv
sudo -u stockgame .venv/bin/pip install ".[server]"
sudo -u stockgame mkdir -p data logs

sudo -u stockgame cp .env.example .env
sudo -u stockgame python3 -c \
  "import secrets; print('STOCKGAME_SECRET_KEY=' + secrets.token_urlsafe(48))" \
  | sudo -u stockgame tee -a .env
sudo chmod 600 .env

sudo cp docker/stockgame.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stockgame
sudo systemctl status stockgame
```

The unit applies migrations before starting (`ExecStartPre`) and is hardened with
`ProtectSystem=strict`, `NoNewPrivileges` and a `ReadWritePaths` allowlist
covering only `data/` and `logs/`.

Logs:

```bash
sudo journalctl -u stockgame -f
```

---

## Operating it

### Inspecting

`stockgame-admin` works against the database, so it does not need the server to
be up and exposes no network surface.

```bash
docker compose exec server stockgame-admin status
docker compose exec server stockgame-admin players
docker compose exec server stockgame-admin trades --limit 50

# systemd install:
sudo -u stockgame /opt/stockgame/.venv/bin/stockgame-admin status
```

There is also an authenticated HTTP admin surface at `/api/admin/*`, available
only to accounts with the admin flag (it returns 404 to everyone else). Disable it
entirely with `STOCKGAME_ADMIN_ENABLED=false`.

### Moderation

```bash
stockgame-admin promote alice      # grant admin
stockgame-admin suspend griefer    # disable account, revoke its sessions
stockgame-admin reinstate griefer
stockgame-admin sessions --revoke-user alice
stockgame-admin halt LITH          # halt one symbol (takes effect on restart)
```

### Backups

The entire world is one SQLite file. Back it up **with the SQLite backup API**,
not `cp` — copying a live WAL database can produce a torn file.

```bash
#!/usr/bin/env bash
# /opt/stockgame/backup.sh
set -euo pipefail
STAMP=$(date -u +%Y%m%d-%H%M%S)
mkdir -p /var/backups/stockgame
docker compose -f /opt/stockgame/docker-compose.yml exec -T server \
  python -c "
import sqlite3
src = sqlite3.connect('/data/stockgame.sqlite3')
dst = sqlite3.connect('/data/backup.sqlite3')
src.backup(dst); dst.close(); src.close()
"
docker compose -f /opt/stockgame/docker-compose.yml cp \
  server:/data/backup.sqlite3 "/var/backups/stockgame/stockgame-$STAMP.sqlite3"
gzip -f "/var/backups/stockgame/stockgame-$STAMP.sqlite3"
find /var/backups/stockgame -name '*.gz' -mtime +30 -delete
```

```bash
chmod +x /opt/stockgame/backup.sh
sudo crontab -e
# 0 4 * * *  /opt/stockgame/backup.sh
```

To restore: stop the server, drop the file in as `/data/stockgame.sqlite3`, start
it again. The market resumes exactly where the backup left off.

### Upgrading

```bash
cd /opt/stockgame
/opt/stockgame/backup.sh          # always back up first
git pull
docker compose up -d --build      # entrypoint runs `alembic upgrade head`
docker compose logs -f
```

Migrations run automatically on container start. A failed migration stops the
container rather than letting a half-migrated server accept trades.

### Monitoring

`/api/health` returns 200 with database status. Both the Dockerfile and compose
file already define a healthcheck against it.

```bash
watch -n5 'curl -s https://stocks.example.com/api/health'
docker compose ps            # health column
```

Logs are JSON when `STOCKGAME_LOG_JSON=true`, with `stockgame.auth`,
`stockgame.trading` and `stockgame.ws` carrying the audit trail (registrations,
logins, every fill, connects and disconnects).

---

## Moving to PostgreSQL

The models and migrations are backend-agnostic; there is no code change.

```bash
echo "POSTGRES_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" >> .env
docker compose -f docker-compose.yml -f docker/postgres-compose.yml up -d
```

The overlay sets `STOCKGAME_DATABASE_URL` to the `asyncpg` driver and makes the
server wait for the database's healthcheck. `asyncpg` is already in the image.

To migrate existing data you will need a SQLite → PostgreSQL dump (`pgloader`
handles this well); the schema is created by `alembic upgrade head` either way.

---

## Trading hours

The market keeps a wall-clock session and freezes outside it — prices stop
moving, the simulated day stops advancing, and orders are rejected with the
market closed. Defaults:

```dotenv
STOCKGAME_MARKET_TIMEZONE=Australia/Sydney
STOCKGAME_MARKET_OPEN_TIME=09:00
STOCKGAME_MARKET_CLOSE_TIME=15:00
STOCKGAME_MARKET_WEEKDAYS_ONLY=true
```

Times are local to `STOCKGAME_MARKET_TIMEZONE`, so the bell stays at 09:00
through daylight saving instead of drifting an hour twice a year.

At the default `STOCKGAME_DAY_SECONDS=3600`, a six-hour session is six
simulated days, so the market advances Monday to Friday and stands still over
the weekend.

**To run continuously instead**, open the full day and drop the weekday rule:

```dotenv
STOCKGAME_MARKET_OPEN_TIME=00:00
STOCKGAME_MARKET_CLOSE_TIME=23:59
STOCKGAME_MARKET_WEEKDAYS_ONLY=false
```

`STOCKGAME_MARKET_OPEN=false` is a separate master switch that keeps the
market shut whatever the schedule says. To stop trading during a session
without fighting the schedule, use the admin halt — the scheduler leaves a
halt alone, but it will reopen a market you merely closed.

---

## Tuning

| Setting | Effect | When to change it |
|---|---|---|
| `STOCKGAME_TICK_SECONDS` | Price update frequency | Lower for a livelier market and more CPU/bandwidth; 2s is a good default |
| `STOCKGAME_MARKET_OPEN_TIME` / `_CLOSE_TIME` | Session window | See [Trading hours](#trading-hours) |
| `STOCKGAME_DAY_SECONDS` | Length of a simulated day | Shorter = faster seasons, more daily candles, more rollover work |
| `STOCKGAME_TICKS_PER_CANDLE` | 1D chart resolution | Higher = fewer rows written |
| `STOCKGAME_LEADERBOARD_INTERVAL_SECONDS` | Ranking refresh | Raise it if you have hundreds of players |
| `STOCKGAME_MAX_CONNECTIONS_PER_USER` | Sockets per account | Lower to 2 if abused |
| `STOCKGAME_ARGON2_MEMORY_COST` | Password hashing cost | Lower only on very small hosts; it is your main defence on a leak |

Intraday candles are pruned after two simulated days; end-of-day candles are kept
forever. At default settings that is roughly 47 rows per simulated day — a few
megabytes a year.

---

## Security checklist

- [ ] `STOCKGAME_SECRET_KEY` set to a real random value, `.env` is `chmod 600`
- [ ] `STOCKGAME_DEBUG=false` (otherwise `/docs` and verbose errors are exposed)
- [ ] `STOCKGAME_TRUST_PROXY_HEADERS=true` **only** because nginx is in front
- [ ] The container port is bound to `127.0.0.1`, not `0.0.0.0`
- [ ] TLS certificate installed and auto-renewing (`systemctl status certbot.timer`)
- [ ] `.env` is not committed — it is in `.gitignore`
- [ ] Firewall allows only 22, 80 and 443
- [ ] Backups running and *restore tested at least once*
- [ ] You registered the first account, so nobody else got admin

---

## Troubleshooting

**Client says "Could not reach …"**
`curl https://stocks.example.com/api/health`. If that works but the client
does not, the WebSocket upgrade is the suspect — check the `/ws` block in the
nginx config and `docker compose logs`.

**Clients reconnect every minute**
`proxy_read_timeout` in the `/ws` block is too low. It must be minutes, not
seconds; the supplied config uses 3600s.

**"database is locked"**
Two servers are pointed at the same SQLite file. Only one process may own a
world. Check `docker compose ps` and any stray systemd unit.

**Everyone logged out after a restart**
`STOCKGAME_SECRET_KEY` is unset, so a throwaway key was generated. The startup
log warns about this explicitly.

**Market seems frozen**
`stockgame-admin status` shows the market state. If it says `closed` or `halted`,
`curl -X POST -H "Authorization: Bearer <admin token>" .../api/admin/market/open`
or set `STOCKGAME_MARKET_OPEN=true` and restart.

**Prices restarted from the beginning**
The database volume was lost. `docker volume ls | grep stockgame` — if the volume
is gone, the next start seeds a fresh world. Restore from backup.
