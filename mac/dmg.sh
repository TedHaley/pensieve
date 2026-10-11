#!/bin/sh
# Builds Pensieve-<version>.dmg: the app with its backend installer inside (uv + the pensieve wheel).
# On first launch the app installs the backend (Python 3.12 + dependencies, ~1 GB download) into ~/.local.
#   mac/dmg.sh            -> mac/build/Pensieve-<version>.dmg
# Signed ad hoc (no Developer ID): on other Macs, approve it once in System Settings → Privacy & Security.
set -eu
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' Resources/Info.plist)"

PENSIEVE_NO_REGISTER=1 sh build.sh
APP=build/Pensieve.app

# backend installer: the wheel built from this checkout, and a uv binary
rm -rf build/wheel
(cd "$ROOT" && uv build --wheel --out-dir mac/build/wheel >/dev/null)
mkdir -p "$APP/Contents/Resources/backend"
cp build/wheel/*.whl "$APP/Contents/Resources/backend/"
UV="${PENSIEVE_UV:-$(command -v uv)}"
cp "$(realpath "$UV")" "$APP/Contents/Resources/backend/uv"
codesign --force --deep -s - "$APP"

if [ ! -f build/dmg-background.tiff ] || [ scripts/make_dmg_background.swift -nt build/dmg-background.tiff ]; then
  swiftc -O scripts/make_dmg_background.swift -o build/make_dmg_background
  build/make_dmg_background build/dmg-bg.png 1
  build/make_dmg_background build/dmg-bg@2x.png 2
  tiffutil -cathidpicheck build/dmg-bg.png build/dmg-bg@2x.png -out build/dmg-background.tiff 2>/dev/null
fi

STAGE=build/dmg
rm -rf "$STAGE" && mkdir -p "$STAGE/.background"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp build/dmg-background.tiff "$STAGE/.background/background.tiff"
OUT="build/Pensieve-$VERSION.dmg"
RW=build/Pensieve-rw.dmg
rm -f "$OUT" "$RW"
hdiutil create -quiet -volname "Pensieve" -srcfolder "$STAGE" -fs HFS+ -format UDRW -ov "$RW"
rm -rf "$STAGE"

# Finder lays out the window: the background, the app on the left and Applications on the right (the positions
# make_dmg_background.swift draws the arrow between). Finder only finds the disk by name, so no other "Pensieve"
# volume may be mounted. The first run asks to let the terminal control Finder.
hdiutil detach -quiet /Volumes/Pensieve 2>/dev/null || true
DEV="$(hdiutil attach -readwrite -noverify -noautoopen "$RW" | awk '/Apple_HFS/ {print $1}')"
chflags hidden /Volumes/Pensieve/.background /Volumes/Pensieve/.fseventsd 2>/dev/null || true
osascript <<'EOF'
tell application "Finder"
  tell disk "Pensieve"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set bounds of container window to {200, 120, 840, 520}
    set opts to icon view options of container window
    set arrangement of opts to not arranged
    set icon size of opts to 112
    set text size of opts to 13
    set background picture of opts to file ".background:background.tiff"
    set position of item "Pensieve.app" of container window to {160, 200}
    set position of item "Applications" of container window to {480, 200}
    update without registering applications
    delay 1
    close
  end tell
end tell
EOF
sync
hdiutil detach -quiet "$DEV"
hdiutil convert -quiet "$RW" -format UDZO -o "$OUT"
rm -f "$RW"
echo "Built $(pwd)/$OUT ($(du -h "$OUT" | cut -f1))"
