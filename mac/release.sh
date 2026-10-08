#!/bin/sh
# Publish a new version: bumps the version, builds the DMG, tags it and creates the GitHub release that the
# app's update check (and tedhaley.ca/pensieve's Download button) picks up.
#   mac/release.sh 0.3.0 "What changed, in a sentence or two"
# Needs: a clean working tree, gh logged in to an account that can publish to TedHaley/pensieve.
set -eu
cd "$(dirname "$0")/.."
VERSION="${1:?usage: mac/release.sh <version> [notes]}"
NOTES="${2:-Pensieve $VERSION}"
case "$VERSION" in *[!0-9.]*|"") echo "version must look like 0.3.0"; exit 1;; esac
[ -z "$(git status --porcelain)" ] || { echo "commit or stash your changes first"; exit 1; }
git rev-parse "v$VERSION" >/dev/null 2>&1 && { echo "v$VERSION already exists"; exit 1; }

/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" mac/Resources/Info.plist
BUILD=$(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" mac/Resources/Info.plist)
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $((BUILD + 1))" mac/Resources/Info.plist
sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
uv lock -q
git commit -qam "Release $VERSION"

sh mac/dmg.sh
cp "mac/build/Pensieve-$VERSION.dmg" mac/build/Pensieve.dmg
git tag "v$VERSION"
git push -q origin HEAD "v$VERSION"
gh release create "v$VERSION" mac/build/Pensieve.dmg --repo TedHaley/pensieve --title "Pensieve $VERSION for Mac" \
  --notes "$NOTES

**Install:** open Pensieve.dmg and drag Pensieve to Applications (Apple Silicon, macOS 14+). Already installed? The app offers the update itself."
echo "Released v$VERSION. Installed apps will offer it within a day (or via Check for Updates…)."
