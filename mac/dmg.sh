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

STAGE=build/dmg
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
OUT="build/Pensieve-$VERSION.dmg"
rm -f "$OUT"
hdiutil create -quiet -volname "Pensieve" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$OUT"
rm -rf "$STAGE"
echo "Built $(pwd)/$OUT ($(du -h "$OUT" | cut -f1))"
