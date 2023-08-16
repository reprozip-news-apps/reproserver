#!/bin/sh

download_asset() {
  URL="$1"
  DEST="$2"
  CHECKSUM="$3"

  if [ -n "$(dirname "$DEST")" ]; then
    mkdir -p "$(dirname "$DEST")" || return 1
  fi

  # If the file exists and has the correct checksum
  if [ -e "$2" ] && printf '%s  %s\n' "$CHECKSUM" "$DEST" | sha256sum -c >/dev/null 2>&1; then
    return 0
  fi

  # Download the file
  printf 'Downloading %s from %s\n' "$DEST" "$URL" >&2
  if ! curl -fsSLo "$DEST" "$URL"; then
    return 1
  fi

  # Check the checksum
  if ! printf '%s  %s\n' "$CHECKSUM" "$DEST" | sha256sum -c >/dev/null 2>&1; then
    printf 'Invalid checksum for %s\n' "$DEST"
    return 1
  fi

  return 0
}

set -eu

cd "$(dirname "$0")/.."

download_asset "https://cdn.jsdelivr.net/npm/@webrecorder/archivewebpage@0.11.0/dist/embed/ui.js" "reproserver/web/static/js/archivewebpage-ui.js" "9b1a7edad4ed7f07daf96c3b32a8db8ea08aa155fda87c2ba089d36dc51758a2"
download_asset "https://cdn.jsdelivr.net/npm/@webrecorder/archivewebpage@0.11.0/dist/embed/replay/sw.js" "reproserver/web/static/replay/sw.js" "bb742cc4cec2e373e9dee9de018f567cd5c10dd3e4a9a9d807841cd9b2e090f2"
