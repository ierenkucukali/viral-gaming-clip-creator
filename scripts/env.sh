# Source this file: `source scripts/env.sh`
# Local-only environment for this private SharkyClips checkout (no system-wide changes).
_SC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
export PATH="$_SC_ROOT/tools/ffmpeg/bin:$_SC_ROOT/.venv/bin:$PATH"
# Settings / models / profiles stay inside the project folder (gitignored), not ~/.sharkyclips
export SHARKYCLIPS_HOME="${SHARKYCLIPS_HOME:-$_SC_ROOT/.sharkyclips}"
export SHARKYCLIPS_NO_MIGRATE=1
