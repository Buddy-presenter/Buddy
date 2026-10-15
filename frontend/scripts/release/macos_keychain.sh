#!/bin/bash
set -euo pipefail
: "${MAC_SIGNING_P12_BASE64:?Production signing identity is required}"
: "${MAC_SIGNING_PASSWORD:?Certificate password is required}"
keychain="$RUNNER_TEMP/buddy-signing.keychain-db"
password=$(uuidgen)
echo "::add-mask::$password"
python -c 'import base64,os,pathlib; pathlib.Path(os.environ["RUNNER_TEMP"],"buddy-signing.p12").write_bytes(base64.b64decode(os.environ["MAC_SIGNING_P12_BASE64"]))'
security create-keychain -p "$password" "$keychain"
security set-keychain-settings -lut 21600 "$keychain"
security unlock-keychain -p "$password" "$keychain"
security import "$RUNNER_TEMP/buddy-signing.p12" -k "$keychain" -P "$MAC_SIGNING_PASSWORD" -T /usr/bin/codesign
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$password" "$keychain" >/dev/null
security list-keychains -d user -s "$keychain" "$HOME/Library/Keychains/login.keychain-db"
rm -f "$RUNNER_TEMP/buddy-signing.p12"
