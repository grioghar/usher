#!/usr/bin/env bash
# Rebuild the routing core from Sigil -> wasm32. Only needed if you change the
# policy in src/plex_director.sg. Requires a cc0 binary (the Sigil compiler):
#   https://github.com/grioghar/sigil
# cc0 reads a 4-line stdin protocol:  <out>\n<src>\n\n<target>\n
set -euo pipefail
CC0="${CC0:-cc0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
printf '%s\n%s\n\nwasm\n' "$ROOT/src/plex_director.wasm" "$ROOT/src/plex_director.sg" | "$CC0"
echo "built src/plex_director.wasm"
