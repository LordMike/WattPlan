#!/usr/bin/env bash

# Run Home Assistant tests from the canonical repo with a worktree-specific
# Linux virtualenv and pytest temporary directory on the local filesystem.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKTREE_KEY="$(printf '%s' "$ROOT_DIR" | cksum | awk '{print $1}')"
DEFAULT_VENV_DIR="/tmp/wattplan-venv-$WORKTREE_KEY"
DEFAULT_BASE_TEMP_DIR="/tmp/wattplan-pytest-$WORKTREE_KEY"
VENV_DIR="${WATTPLAN_TEST_VENV:-$DEFAULT_VENV_DIR}"
BASE_TEMP_DIR="${WATTPLAN_TEST_BASETEMP:-$DEFAULT_BASE_TEMP_DIR}"

print_setup_help() {
    cat >&2 <<EOF
Create or repair the test environment from a Linux shell (WSL is fine):
  uv venv --python 3.14 "$VENV_DIR"
  uv pip install --python "$VENV_DIR/bin/python" -r "$ROOT_DIR/requirements-test.txt"
Then rerun ./scripts/run_tests.sh. If dependencies are already in uv's cache
and the package index is unavailable, add --offline to the install command.
The /tmp environment is temporary; recreate it if your system clears /tmp.
If the venv exists but has a broken or outdated Python, replace only that venv:
  uv venv --clear --python 3.14 "$VENV_DIR"
EOF
}

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "WattPlan Home Assistant tests require Linux. Run this script inside Linux or WSL, not Windows/Git Bash." >&2
    exit 2
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "WattPlan test Python is missing or not executable: $VENV_DIR/bin/python" >&2
    print_setup_help
    exit 2
fi

if ! "$VENV_DIR/bin/python" -c 'import sys; assert sys.version_info >= (3, 14, 2)' >/dev/null 2>&1; then
    echo "WattPlan tests require a working Python 3.14.2+ virtualenv at: $VENV_DIR" >&2
    print_setup_help
    exit 2
fi

if [[ ! -x "$VENV_DIR/bin/pytest" ]]; then
    echo "WattPlan test pytest is missing or not executable: $VENV_DIR/bin/pytest" >&2
    print_setup_help
    exit 2
fi

export TMPDIR=/tmp
export TEMP=/tmp
export TMP=/tmp

cd "$ROOT_DIR"
exec "$VENV_DIR/bin/pytest" --basetemp="$BASE_TEMP_DIR" "$@"
