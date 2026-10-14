#!/bin/sh
# Publish the version that main has: build the DMG, tag main and create the GitHub release. It makes no commit:
# mac/bump.sh sets the version as part of the release PR, before the merge.
#   mac/release.sh "What changed, in a sentence or two"
# Needs: main, clean and the same as origin/main; gh logged in to an account that can publish to TedHaley/pensieve.
set -eu
cd "$(dirname "$0")/.."
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
NOTES="${1:-Pensieve $VERSION}"
case "$NOTES" in [0-9]*.[0-9]*) case "$NOTES" in *" "*) ;; *) echo "pass the notes only: the version comes from main ($VERSION)"; exit 1;; esac;; esac
[ "$(git branch --show-current)" = main ] || { echo "release from main: git checkout main && git pull"; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "commit or stash your changes first"; exit 1; }
git fetch -q origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || { echo "main is not the same as origin/main: git pull"; exit 1; }
git rev-parse "v$VERSION" >/dev/null 2>&1 && { echo "v$VERSION already exists: run mac/bump.sh in the release PR first"; exit 1; }
APP=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" mac/Resources/Info.plist)
[ "$APP" = "$VERSION" ] || { echo "Info.plist says $APP, pyproject.toml says $VERSION: run mac/bump.sh $VERSION"; exit 1; }

sh mac/dmg.sh
cp "mac/build/Pensieve-$VERSION.dmg" mac/build/Pensieve.dmg
git tag "v$VERSION"
git push -q origin "v$VERSION"
gh release create "v$VERSION" mac/build/Pensieve.dmg --repo TedHaley/pensieve --title "Pensieve $VERSION for Mac" \
  --notes "$NOTES

**Install:** open Pensieve.dmg and drag Pensieve to Applications (Apple Silicon, macOS 14+). Already installed? The app offers the update itself."
echo "Released v$VERSION. Installed apps will offer it within a day (or via Check for Updates…)."
