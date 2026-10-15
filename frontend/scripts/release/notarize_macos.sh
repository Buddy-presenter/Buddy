#!/bin/bash
set -euo pipefail
output="$1"
: "${APPLE_NOTARY_KEY_BASE64:?Notarization key is required}"
: "${APPLE_NOTARY_KEY_ID:?Notarization key ID is required}"
: "${APPLE_NOTARY_ISSUER:?Notarization issuer is required}"
key="$RUNNER_TEMP/buddy-notary.p8"
trap 'rm -f "$key"' EXIT
python -c 'import base64,os,pathlib; pathlib.Path(os.environ["RUNNER_TEMP"],"buddy-notary.p8").write_bytes(base64.b64decode(os.environ["APPLE_NOTARY_KEY_BASE64"]))'
codesign --verify --deep --strict "$output/bundle/Buddy.app"
ditto -c -k --sequesterRsrc --keepParent "$output/bundle/Buddy.app" "$RUNNER_TEMP/buddy-notary.zip"
xcrun notarytool submit "$RUNNER_TEMP/buddy-notary.zip" --key "$key" --key-id "$APPLE_NOTARY_KEY_ID" --issuer "$APPLE_NOTARY_ISSUER" --wait --output-format json > "$RUNNER_TEMP/notary-result.json"
python -c 'import json,os,pathlib; assert json.loads(pathlib.Path(os.environ["RUNNER_TEMP"],"notary-result.json").read_text())["status"] == "Accepted"'
xcrun stapler staple "$output/bundle/Buddy.app"
spctl --assess --type execute "$output/bundle/Buddy.app"
for archive in "$output"/*.zip; do
    ditto -c -k --sequesterRsrc --keepParent "$output/bundle/Buddy.app" "$archive"
done
