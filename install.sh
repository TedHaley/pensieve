#!/bin/sh
# Pensieve installer — https://tedhaley.ca/pensieve
#   curl -fsSL https://tedhaley.ca/pensieve/install.sh | sh
# Installs uv if needed, installs Pensieve as a command-line tool, and starts it when run from a terminal.
# Set PENSIEVE_NO_START=1 to install without starting (agents and scripts get this behaviour automatically).
set -eu

REPO="git+https://github.com/TedHaley/pensieve"
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

if ! command -v git >/dev/null 2>&1; then
  echo "Pensieve needs git. Install it first (macOS: xcode-select --install), then re-run this script."
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  bold "Installing uv (fast Python package manager, https://docs.astral.sh/uv/)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

bold "Installing Pensieve…"
uv tool install --force --python 3.12 "$REPO"
uv tool update-shell >/dev/null 2>&1 || true

BIN="$(command -v pensieve 2>/dev/null || echo "$(uv tool dir --bin)/pensieve")"
echo
bold "Pensieve is installed: $BIN"
echo "  Start it:            pensieve                      (opens http://localhost:8765)"
echo "  Index more repos:    pensieve --repos ~/code       (a repo, or a folder to search for repos)"
echo "  Chat and summaries:  run LM Studio (https://lmstudio.ai) with a chat model, e.g. qwen/qwen3.5-9b, on port 1234"
echo "  Uninstall:           uv tool uninstall pensieve && rm -rf ~/.pensieve"
echo

if [ -t 1 ] && [ -z "${PENSIEVE_NO_START:-}" ]; then
  bold "Starting Pensieve → http://localhost:8765  (Ctrl-C to stop)"
  exec "$BIN"
fi
