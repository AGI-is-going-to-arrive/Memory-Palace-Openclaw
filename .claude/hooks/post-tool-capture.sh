#!/usr/bin/env bash
# Post-tool-use hook wrapper. The shared Node script is used for cross-platform
# behavior; this shell wrapper remains for users who call it directly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_HOOK="$SCRIPT_DIR/post-tool-capture.mjs"

if command -v node >/dev/null 2>&1 && [ -f "$NODE_HOOK" ]; then
    node "$NODE_HOOK"
    exit 0
fi

cat >/dev/null
exit 0
