#!/usr/bin/env bash
# Run every quality check: lint and format, type checks, Music Assistant's provider checks and
# the tests. Needs the environment from scripts/dev_setup.sh.
#
#   scripts/check.sh           # everything
#   scripts/check.sh --fast    # lint and format only (used by pre-commit)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MA_DIR="${MA_DIR:-$REPO_DIR/../music-assistant-server}"
BIN="$MA_DIR/.venv/bin"

if [[ ! -x "$BIN/ruff" || ! -L "$MA_DIR/music_assistant/providers/ilpost" ]]; then
  echo "Run scripts/dev_setup.sh first" >&2
  exit 1
fi

step() { printf '\n== %s\n' "$1"; }

# Fail when mypy reports an error in one of our files. mypy also reports errors in the server
# packages it follows, which are ignored. Its output is captured first: with pipefail, piping
# it into grep would take mypy's exit status instead of grep's.
mypy_only() {
  local pattern="$1" output
  shift
  output="$("$BIN/mypy" "$@" 2>&1 || true)"
  if grep -E "$pattern" <<<"$output"; then
    exit 1
  fi
}

cd "$REPO_DIR"
step "ruff (repository config)"
"$BIN/ruff" check --no-fix ilpost tests scripts
"$BIN/ruff" format --check ilpost tests scripts
[[ "${1:-}" == "--fast" ]] && exit 0

cd "$MA_DIR"
step "ruff (Music Assistant config)"
"$BIN/ruff" check --no-fix music_assistant/providers/ilpost/
"$BIN/ruff" format --check music_assistant/providers/ilpost/

step "mypy (Music Assistant config)"
# the server config checks the whole package; only errors in this provider count
mypy_only "providers/ilpost/" music_assistant/providers/ilpost/
echo "no errors in the provider"

step "Music Assistant provider checks"
"$BIN/python" -m scripts.check_manifests music_assistant/providers/ilpost/manifest.json
"$BIN/python" -m scripts.check_translatable_labels music_assistant/providers/ilpost/*.py
"$BIN/python" -m scripts.check_config_entries
echo "passed"

cd "$REPO_DIR"
step "mypy (tests and scripts)"
MYPYPATH="$MA_DIR" mypy_only "^(tests|scripts)/" --config-file pyproject.toml tests scripts/live_check.py
echo "no errors in the tests and scripts"

step "pytest"
"$BIN/python" -m pytest -p no:cacheprovider --cov --cov-report=term-missing:skip-covered
