#!/bin/sh
# Set the version for the next release, as the last commit of the release PR: the app's version and build number,
# pyproject.toml and uv.lock. After the PR is merged, mac/release.sh publishes it from main.
#   mac/bump.sh 0.6.6
set -eu
cd "$(dirname "$0")/.."
VERSION="${1:?usage: mac/bump.sh <version>}"
case "$VERSION" in *[!0-9.]*|"") echo "version must look like 0.6.6"; exit 1;; esac
[ "$(git branch --show-current)" != main ] || { echo "bump on the release branch, not main"; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "commit or stash your changes first"; exit 1; }
git rev-parse "v$VERSION" >/dev/null 2>&1 && { echo "v$VERSION already exists"; exit 1; }

/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" mac/Resources/Info.plist
BUILD=$(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" mac/Resources/Info.plist)
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $((BUILD + 1))" mac/Resources/Info.plist
sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
sed -i '' "s/^  \"version\": \".*\"/  \"version\": \"$VERSION\"/" .claude-plugin/plugin.json
uv lock -q
git commit -qam "Version $VERSION"
echo "Committed version $VERSION. Push, merge the PR, then on main run: mac/release.sh \"<notes>\""
