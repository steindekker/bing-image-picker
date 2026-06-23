#!/usr/bin/env bash
# Symlink this working tree into Anki as a dev add-on, replacing any installed
# copy (e.g. the AnkiWeb build) so Anki loads the repo directly. Restart Anki
# after running. Override the target dir with ANKI_ADDONS=... if needed.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
addons="${ANKI_ADDONS:-$HOME/.local/share/Anki2/addons21}"
link="$addons/bing_image_picker"

[ -d "$addons" ] || { echo "Anki addons dir not found: $addons" >&2; exit 1; }

# Drop any existing copy of this add-on: a previous dev symlink, a folder named
# bing_image_picker, or a numeric AnkiWeb folder whose name is "Bing Image
# Picker" (leaving it would make Anki load two copies / a duplicate 🖼 button).
for d in "$addons"/*; do
    [ -e "$d" ] || continue
    base="$(basename "$d")"
    if [ -L "$d" ]; then                       # symlink: remove the link, never its target
        [ "$base" = "bing_image_picker" ] && { echo "Removing dev link: $base"; rm -f "$d"; }
        continue
    fi
    [ -d "$d" ] || continue
    name="$(jq -r '.name // empty' "$d/meta.json" 2>/dev/null || true)"
    if [ "$base" = "bing_image_picker" ] || [ "$name" = "Bing Image Picker" ]; then
        echo "Removing installed copy: $base${name:+ ($name)}"
        rm -rf "$d"
    fi
done

ln -s "$repo" "$link"
echo "Linked $link -> $repo"
echo "Restart Anki to load the dev add-on."
