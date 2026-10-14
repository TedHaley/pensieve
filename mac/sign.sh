# Sourced by build.sh and dmg.sh. Signs the app ad hoc, or with the identity that PENSIEVE_SIGN_IDENTITY names, for
# example a Developer ID. An ad-hoc app is a new app to macOS after each update, so macOS asks again for the folders
# it reads.
SIGN_IDENTITY="${PENSIEVE_SIGN_IDENTITY:-}"

sign_app() {
  if [ -z "$SIGN_IDENTITY" ]; then
    codesign --force --deep -s - "$1"
    return
  fi
  # by its SHA-1, so two certificates with the same name can't make codesign stop and ask
  id="$(security find-identity -p codesigning | awk -v n="\"$SIGN_IDENTITY\"" 'index($0, n) {print $2; exit}')"
  [ -n "$id" ] || { echo "\"$SIGN_IDENTITY\" is not in the keychain" >&2; return 1; }
  codesign --force --deep -s "$id" "$1"
}
