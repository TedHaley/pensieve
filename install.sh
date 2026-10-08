#!/bin/sh
# Pensieve installer — https://tedhaley.ca/pensieve
#   curl -fsSL https://tedhaley.ca/pensieve/install.sh | sh
# Installs uv if needed and the `pensieve` backend. On a Mac with the Xcode command-line tools it also builds the
# Pensieve app (search panel on Control+Shift) into ~/Applications and starts it.
# PENSIEVE_NO_START=1 installs without starting (agents and scripts get this behaviour automatically).
# PENSIEVE_NO_APP=1 skips the Mac app.
set -eu

REPO_URL="https://github.com/TedHaley/pensieve"
# The latest release (PENSIEVE_REF=main for the newest code, or a tag like v0.2.0)
REF="${PENSIEVE_REF:-$(curl -fsSL https://api.github.com/repos/TedHaley/pensieve/releases/latest 2>/dev/null \
  | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)}"
REF="${REF:-main}"
SRC="$HOME/.pensieve/src"
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

bold "Installing the Pensieve backend…"
uv tool install --force --python 3.12 "git+$REPO_URL@$REF"
uv tool update-shell >/dev/null 2>&1 || true
BIN="$(command -v pensieve 2>/dev/null || echo "$(uv tool dir --bin)/pensieve")"

APP=""
if [ "$(uname)" = Darwin ] && [ -z "${PENSIEVE_NO_APP:-}" ] && xcrun --find swift >/dev/null 2>&1; then
  bold "Building the Pensieve app (about a minute)…"
  mkdir -p "$HOME/.pensieve"
  rm -rf "$SRC" && git clone -q --depth 1 --branch "$REF" "$REPO_URL" "$SRC"
  if sh "$SRC/mac/build.sh" --install; then
    APP="$HOME/Applications/Pensieve.app"
  else
    echo "Couldn't build the Mac app; the backend and browser visualizer still work."
  fi
elif [ "$(uname)" = Darwin ] && [ -z "${PENSIEVE_NO_APP:-}" ]; then
  echo "Skipping the Mac app: it needs the Xcode command-line tools (xcode-select --install). Re-run this script after."
fi

echo
bold "Pensieve is installed."
if [ -n "$APP" ]; then
  echo "  App:                 $APP  (menu bar; press Control+Shift to search)"
fi
echo "  Backend:             $BIN  (visualizer at http://localhost:8765)"
echo "  Connect an agent:    claude mcp add pensieve -- pensieve mcp      (scoped to the repo the agent runs in)"
echo "                       or over HTTP: claude mcp add --transport http pensieve \"http://127.0.0.1:8765/mcp?scope=<name>\""
echo "  Optional summaries:  run LM Studio (https://lmstudio.ai) with a chat model, e.g. qwen/qwen3.5-9b, on port 1234"
echo "  Uninstall:           uv tool uninstall pensieve && rm -rf ~/.pensieve ~/Applications/Pensieve.app"
echo

if [ -t 1 ] && [ -z "${PENSIEVE_NO_START:-}" ]; then
  if [ -n "$APP" ]; then
    bold "Starting Pensieve…"
    open "$APP"
  else
    bold "Starting Pensieve → http://localhost:8765  (Ctrl-C to stop)"
    exec "$BIN"
  fi
fi
