#!/bin/sh
# Builds "Pensieve.app" (the menu bar item) with the Swift command-line tools. Xcode is not necessary.
# Run `cargo build --release` first: the app then holds the pensieve program, and its menu can install it.
#   mac/build.sh   -> mac/build/Pensieve.app
set -eu
cd "$(dirname "$0")"

# SwiftPM's build database fails with "disk I/O error" in folders that iCloud syncs, such as Desktop.
SCRATCH="${PENSIEVE_BUILD_DIR:-$HOME/Library/Caches/pensieve-mac-build}"
swift build -c release --scratch-path "$SCRATCH"
BIN="$(swift build -c release --scratch-path "$SCRATCH" --show-bin-path)/Pensieve"

APP="build/Pensieve.app"
rm -rf "$APP" "build/Pensieve Lite.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Helpers" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Pensieve"
cp Resources/Info.plist "$APP/Contents/Info.plist"
[ ! -f build/AppIcon.icns ] || cp build/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
# Helpers, not MacOS: the file system ignores case, so "pensieve" and "Pensieve" are one name in one folder.
[ ! -x ../target/release/pensieve ] || cp ../target/release/pensieve "$APP/Contents/Helpers/pensieve"
codesign --force --sign - "$APP"
echo "Built $APP"
