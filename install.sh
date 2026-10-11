#!/bin/sh
# Pensieve installer — https://tedhaley.ca/pensieve
#   curl -fsSL https://tedhaley.ca/pensieve/install.sh | sh
# On a Mac: downloads the latest Pensieve.dmg, copies the app to Applications, and installs the `pensieve` backend
# from the wheel inside the app with the app's own uv. Nothing else is needed: no git, Xcode tools or Python.
# Elsewhere, or with PENSIEVE_REF=main: installs uv if needed and the backend from source (git), and on a Mac with
# the Xcode command-line tools builds the app.
# The app starts only when this runs in a terminal; agents and scripts install without starting it.
# PENSIEVE_NO_START=1 never starts it. PENSIEVE_NO_APP=1 skips the Mac app.
set -eu

REPO_URL="https://github.com/TedHaley/pensieve"
SRC="$HOME/.pensieve/src"
bold() { printf '\033[1m%s\033[0m\n' "$*"; }
APP=""
BIN=""

# The app the user already has keeps its place; a new install goes to ~/Applications, which needs no admin rights.
# PENSIEVE_APP=/path/Pensieve.app picks the place.
app_dest() {
  if [ -n "${PENSIEVE_APP:-}" ]; then echo "$PENSIEVE_APP"
  elif [ -d /Applications/Pensieve.app ] && [ -w /Applications ]; then echo /Applications/Pensieve.app
  else echo "$HOME/Applications/Pensieve.app"; fi
}

# A backend left running keeps the old code in memory while its files are replaced. The app starts the new one.
stop_backend() {
  # [p]: this pattern must not match the installer's own command line, which holds this script's text
  if pkill -f "[p]ensieve serve" 2>/dev/null; then
    echo "Stopped the running Pensieve backend; the app starts the new one."
  fi
}

install_from_dmg() {
  if [ -n "${PENSIEVE_REF:-}" ]; then url="$REPO_URL/releases/download/$PENSIEVE_REF/Pensieve.dmg"
  else url="$REPO_URL/releases/latest/download/Pensieve.dmg"; fi
  tmp="$(mktemp -d)"
  bold "Downloading Pensieve…"
  curl -fL --progress-bar -o "$tmp/Pensieve.dmg" "$url" || { rm -rf "$tmp"; return 1; }
  mkdir "$tmp/mnt"
  hdiutil attach -quiet -nobrowse -readonly -mountpoint "$tmp/mnt" "$tmp/Pensieve.dmg" || { rm -rf "$tmp"; return 1; }
  dest="$(app_dest)"
  # replacing a running app's files can crash it; quit it and open it again afterwards
  running=""
  if pgrep -qf "$dest/Contents/MacOS/Pensieve"; then
    running=1
    osascript -e 'tell application id "ca.tedhaley.pensieve" to quit' >/dev/null 2>&1 || true
    sleep 2
  fi
  mkdir -p "$(dirname "$dest")"
  rm -rf "$dest"
  ditto "$tmp/mnt/Pensieve.app" "$dest"
  hdiutil detach -quiet "$tmp/mnt" || true
  rm -rf "$tmp"
  APP="$dest"

  bold "Installing the Pensieve backend (Python 3.12 and its packages)…"
  wheel="$(ls "$APP"/Contents/Resources/backend/*.whl | head -1)"
  UV_TOOL_BIN_DIR="$HOME/.local/bin" "$APP/Contents/Resources/backend/uv" tool install --force --python 3.12 "$wheel"
  "$APP/Contents/Resources/backend/uv" tool update-shell >/dev/null 2>&1 || true
  # the same stamp the app writes, so it doesn't install the backend again on first launch
  mkdir -p "$HOME/.pensieve"
  printf '%s %s\n' "$(basename "$wheel")" "$(shasum -a 256 "$wheel" | cut -c1-16)" > "$HOME/.pensieve/backend-version"
  stop_backend
  BIN="$HOME/.local/bin/pensieve"
  [ -z "$running" ] || open "$APP"
}

install_from_source() {
  REF="${PENSIEVE_REF:-$(curl -fsSL https://api.github.com/repos/TedHaley/pensieve/releases/latest 2>/dev/null \
    | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)}"
  REF="${REF:-main}"
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
  stop_backend
  BIN="$(command -v pensieve 2>/dev/null || echo "$(uv tool dir --bin)/pensieve")"
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
    echo "Skipping the Mac app: building it from source needs the Xcode command-line tools (xcode-select --install)."
  fi
}

if [ "$(uname)" = Darwin ] && [ "$(uname -m)" = arm64 ] && [ -z "${PENSIEVE_NO_APP:-}" ] && [ "${PENSIEVE_REF:-}" != main ]; then
  install_from_dmg || { echo "Couldn't download the app; installing from source instead."; install_from_source; }
else
  install_from_source
fi

echo
bold "Pensieve is installed."
if [ -n "$APP" ]; then
  echo "  App:                 $APP  (menu bar; press Control+Shift to search)"
fi
echo "  Backend:             $BIN  (visualizer at http://localhost:8765)"
echo "  Set up your agents:  pensieve integrate recommended   (Claude Code, Codex, Cursor, Gemini CLI, Qwen Code,"
echo "                       Windsurf, Claude Desktop and VS Code: the ones on this Mac get Pensieve as an MCP server)"
echo "  Choose what to index: pensieve setup   (lists what it found; nothing is indexed until you choose)"
echo "  Uninstall:           rm -rf ~/.local/share/uv/tools/pensieve ~/.local/bin/pensieve ~/.pensieve ${APP:-}"
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
