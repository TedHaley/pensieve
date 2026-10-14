# Sourced by build.sh and dmg.sh. Signs the app with the "Pensieve Code Signing" certificate (mac/signing-cert.sh
# makes it) when it is in the keychain: every version then has the same identity (bundle id + certificate), so macOS
# keeps the app's permissions, such as reading the Desktop, across updates. Without it the app is signed ad hoc, and
# macOS treats each build as a new app. PENSIEVE_SIGN_IDENTITY names another identity, for example a Developer ID.
SIGN_IDENTITY="${PENSIEVE_SIGN_IDENTITY:-Pensieve Code Signing}"

sign_app() {
  # by its SHA-1, so two certificates with the same name can't make codesign stop and ask
  id="$(security find-identity -p codesigning | awk -v n="\"$SIGN_IDENTITY\"" 'index($0, n) {print $2; exit}')"
  if [ -n "$id" ]; then
    codesign --force --deep -s "$id" "$1"
  else
    echo "note: signing ad hoc (\"$SIGN_IDENTITY\" is not in the keychain). macOS will ask again for the folders this" >&2
    echo "      build reads. Optional: mac/signing-cert.sh signs your builds so macOS remembers (README: Signing)." >&2
    codesign --force --deep -s - "$1"
  fi
}

signed_with_certificate() {
  codesign -d -r- "$1" 2>&1 | grep -q "certificate leaf\|anchor apple"
}
