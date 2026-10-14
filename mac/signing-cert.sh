#!/bin/sh
# Once, on the Mac that builds releases: a self-signed code-signing certificate, "Pensieve Code Signing", in the
# login keychain. build.sh and dmg.sh sign with it (sign.sh), so macOS sees every version as the same app and keeps
# its permissions after an update. It is not a Developer ID: Gatekeeper still calls the app unidentified.
#   mac/signing-cert.sh [backup.p12]     (default: ~/Documents/Pensieve Code Signing.p12)
# Keep the backup. A release signed with another certificate makes macOS ask every user for permission again.
# To use the backup on another Mac: security import <backup.p12> -T /usr/bin/codesign, with the password from
#   security find-generic-password -s "Pensieve Code Signing backup" -w
set -eu
NAME="Pensieve Code Signing"
if security find-identity -p codesigning | grep -q "\"$NAME\""; then
  echo "\"$NAME\" is already in the keychain."
  exit 0
fi
OUT="${1:-$HOME/Documents/$NAME.p12}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
EOF
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$TMP/key.pem" -out "$TMP/cert.pem" -days 3650 \
  -config "$TMP/cert.cnf" 2>/dev/null
PASS="$(openssl rand -base64 24)"
openssl pkcs12 -export -inkey "$TMP/key.pem" -in "$TMP/cert.pem" -out "$TMP/id.p12" -passout "pass:$PASS" -name "$NAME"
security import "$TMP/id.p12" -k "$HOME/Library/Keychains/login.keychain-db" -P "$PASS" -T /usr/bin/codesign >/dev/null
security add-generic-password -a "$USER" -s "$NAME backup" -w "$PASS" -U
cp "$TMP/id.p12" "$OUT"
chmod 600 "$OUT"
echo "Created \"$NAME\" (valid 10 years) in the login keychain."
echo "Backup: $OUT (its password is in the keychain as \"$NAME backup\")."
echo "The first build may ask to let codesign use the key: choose Always Allow."
