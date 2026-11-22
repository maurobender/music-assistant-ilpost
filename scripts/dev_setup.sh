#!/usr/bin/env bash
# Prepare the development environment: a Music Assistant server checkout with a virtualenv
# that has the server, the lint tools and pytest, and the provider linked into it.
#
#   scripts/dev_setup.sh            # clones the server's dev branch into ../music-assistant-server
#   MA_DIR=/path/to/server MA_REF=<branch or tag> scripts/dev_setup.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MA_DIR="${MA_DIR:-$REPO_DIR/../music-assistant-server}"
MA_REF="${MA_REF:-dev}"

if [[ ! -d "$MA_DIR/.git" ]]; then
  git clone --depth 1 --branch "$MA_REF" https://github.com/music-assistant/server.git "$MA_DIR"
fi
echo "Music Assistant server at $(git -C "$MA_DIR" rev-parse --short HEAD)"

cd "$MA_DIR"
[[ -d .venv ]] || uv venv -p 3.14 .venv

# aiolibdatachannel does not build on macOS and the provider does not use it
REQS="$(mktemp)"
trap 'rm -f "$REQS"' EXIT
.venv/bin/python - > "$REQS" <<'EOF'
import tomllib
with open("pyproject.toml", "rb") as f:
    project = tomllib.load(f)
deps = project["project"]["dependencies"]
tools = project["project"]["optional-dependencies"]["test"]
wanted = ("mypy", "ruff", "pytest", "pytest-aiohttp", "pytest-cov", "pytest-timeout", "pre-commit")
print("\n".join(d for d in deps if not d.startswith("aiolibdatachannel")))
print("\n".join(t for t in tools if t.split("=")[0].split("[")[0] in wanted))
EOF
uv pip install -q -p .venv -r "$REQS"
uv pip install -q -p .venv --no-deps -e .

ln -sfn "$REPO_DIR/ilpost" music_assistant/providers/ilpost
echo "Ready: provider linked into $MA_DIR/music_assistant/providers/ilpost"
