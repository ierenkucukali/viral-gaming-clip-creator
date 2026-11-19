#!/usr/bin/env bash
# Start the local web UI on 127.0.0.1 only (default port 8000). Extra args go to `cli.py serve`.
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd "$_SC_ROOT/backend"
exec python cli.py serve --no-browser "$@"
