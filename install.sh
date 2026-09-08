#!/usr/bin/env bash
#
# Installs the Stock Market Game client.
#
#   ./install.sh                 # client only (what most people want)
#   ./install.sh --server        # client + server, for hosting or development
#   ./install.sh --dev           # everything, editable, plus test tooling
#
# Prefers pipx (isolated, and puts `stockgame` on your PATH); falls back to a
# virtualenv in .venv if pipx is unavailable.

set -euo pipefail

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$BOLD" "$OFF" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%s  !!%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()  { printf '%s error:%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

MODE=client
for arg in "$@"; do
  case "$arg" in
    --server) MODE=server ;;
    --dev)    MODE=dev ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "unknown option: $arg (try --help)" ;;
  esac
done

cd "$(dirname "$0")"

# ---------------------------------------------------------------- python
say "Looking for Python 3.10 or newer"
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PYTHON="$candidate"; break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  die "Python 3.10+ is required but was not found.
     macOS:   brew install python@3.12
     Debian:  sudo apt install python3 python3-venv python3-pip
     Fedora:  sudo dnf install python3
     Windows: https://python.org/downloads (tick 'Add Python to PATH')"
fi
ok "using $($PYTHON --version) at $(command -v "$PYTHON")"

EXTRAS=""
case "$MODE" in
  server) EXTRAS="[server]" ;;
  dev)    EXTRAS="[server,dev]" ;;
esac

# ------------------------------------------------------- pipx (preferred)
if [ "$MODE" = "client" ] && command -v pipx >/dev/null 2>&1; then
  say "Installing with pipx"
  pipx install --force --python "$PYTHON" .
  ok "installed"
  echo
  printf '%sRun the game with:%s  stockgame\n' "$BOLD" "$OFF"
  exit 0
fi

# ------------------------------------------------------- virtualenv path
say "Creating a virtual environment in .venv"
if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv || die "could not create a virtualenv.
     On Debian/Ubuntu you may need: sudo apt install python3-venv"
fi

VENV_PY=".venv/bin/python"
VENV_BIN=".venv/bin"
if [ ! -x "$VENV_PY" ]; then          # Windows layout (Git Bash / MSYS)
  VENV_PY=".venv/Scripts/python.exe"
  VENV_BIN=".venv/Scripts"
fi
[ -x "$VENV_PY" ] || die "the virtualenv looks broken; delete .venv and retry"

say "Installing dependencies"
"$VENV_PY" -m pip install --quiet --upgrade pip
if [ "$MODE" = "dev" ]; then
  "$VENV_PY" -m pip install --editable ".${EXTRAS}"
else
  "$VENV_PY" -m pip install ".${EXTRAS}"
fi
ok "installed"

echo
printf '%s─────────────────────────────────────────────────────────%s\n' "$BOLD" "$OFF"
printf '%s  Stock Market Game is ready%s\n\n' "$BOLD" "$OFF"
printf '  Play:            %s/stockgame\n' "$VENV_BIN"
printf '  Pick a server:   %s/stockgame --server stocks.example.com\n' "$VENV_BIN"
if [ "$MODE" != "client" ]; then
  printf '\n  Run a server:    %s/stockgame-server\n' "$VENV_BIN"
  printf '  Inspect it:      %s/stockgame-admin status\n' "$VENV_BIN"
fi
if [ "$MODE" = "dev" ]; then
  printf '  Run tests:       %s/pytest\n' "$VENV_BIN"
fi
printf '\n  Tip: `source %s/activate` to drop the path prefix.\n' "$VENV_BIN"
printf '%s─────────────────────────────────────────────────────────%s\n' "$BOLD" "$OFF"

if [ ! -f .env ] && [ "$MODE" != "client" ]; then
  warn "No .env found. Copy .env.example to .env before running in production."
fi
