# Contributing

```bash
./install.sh --dev
make test lint
```

See [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md) for the full guide.

## Ground rules

1. **The client is never trusted.** If a number can be computed on the server, it
   must be. A pull request that lets the client assert a price, balance or fill
   will not be merged.
2. **Money is integer cents.** Floats belong in the price model and chart
   geometry, nowhere else.
3. **Every behaviour change needs a test.** CI will not merge a red suite.
4. **Run `make format` before pushing.** CI checks formatting.
5. **Schema changes need a migration.** CI runs `alembic check`.

## Good first issues

- New companies in `server/market/universe.py`
- New headline templates in `server/market/news.py`
- New achievements in `server/services/achievements.py`
- Chart improvements in `client/widgets/chart.py`

Each of these is self-contained and has an existing pattern to copy.
