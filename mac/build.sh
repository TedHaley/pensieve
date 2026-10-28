#!/bin/sh
# Builds Pensieve.app from source with the Swift command-line tools (no Xcode needed).
#   mac/build.sh             -> mac/build/Pensieve.app
#   mac/build.sh --install   -> also copies it to $PENSIEVE_APP (default ~/Applications/Pensieve.app)
set -eu
cd "$(dirname "$0")"

# Build products live outside the repo: SwiftPM's build database fails with "disk I/O error" in folders that
# iCloud syncs (Desktop and Documents often are).
SCRATCH="${PENSIEVE_BUILD_DIR:-$HOME/Library/Caches/pensieve-mac-build}"
swift build -c release --scratch-path "$SCRATCH"
BINDIR="$(swift build -c release --scratch-path "$SCRATCH" --show-bin-path)"
BIN="$BINDIR/Pensieve"

mkdir -p build
if [ ! -f build/AppIcon.icns ] || [ scripts/make_icon.swift -nt build/AppIcon.icns ]; then
  swiftc -O scripts/make_icon.swift -o build/make_icon
  rm -rf build/AppIcon.iconset
  build/make_icon build/AppIcon.iconset
  iconutil -c icns build/AppIcon.iconset -o build/AppIcon.icns
fi

APP=build/Pensieve.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Pensieve"
cp "$BINDIR/pensieve-media" "$APP/Contents/MacOS/pensieve-media"
cp Resources/Info.plist "$APP/Contents/Info.plist"
cp build/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
# the site's fonts, shared with the web UI; Brand.registerFonts loads them at launch
cp ../pensieve/static/InterTight.woff2 ../pensieve/static/JetBrainsMono.woff2 "$APP/Contents/Resources/"
. ./sign.sh
[ -n "${PENSIEVE_NO_SIGN:-}" ] || sign_app "$APP"

LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
DEST="$APP"
if [ "${1:-}" = "--install" ]; then
  DEST="${PENSIEVE_APP:-$HOME/Applications/Pensieve.app}"
  mkdir -p "$(dirname "$DEST")"
  rm -rf "$DEST"
  cp -R "$APP" "$DEST"
fi
# so pensieve:// links open this copy (PENSIEVE_NO_REGISTER=1 for dev builds next to an installed app)
[ -n "${PENSIEVE_NO_REGISTER:-}" ] || "$LSREGISTER" -f "$DEST"
echo "Built $DEST"
