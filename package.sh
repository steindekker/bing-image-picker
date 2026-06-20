#!/usr/bin/env bash
# Build bing_image_picker.ankiaddon for AnkiWeb upload.
# Ships only the runtime files — never meta.json (your local config) or caches.
set -euo pipefail
cd "$(dirname "$0")"

out=bing_image_picker.ankiaddon
files=(__init__.py manifest.json config.json)
[[ -f config.md ]] && files+=(config.md)

rm -f "$out"
zip -q "$out" "${files[@]}"
echo "Wrote $out (${files[*]})"
