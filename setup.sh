#!/usr/bin/env bash
# One-time setup for the SMG + Ollama lab. Safe to re-run.
#
#   1. checks for Ollama (tells you how to install it if missing)
#   2. creates .venv (uv if available, otherwise python3 -m venv) and installs requirements.txt
#   3. copies lab.env.example to lab.env (once; edit it freely afterwards)
#   4. downloads the lab model(s) and runs verify_environment.py
#
# Usage:
#   bash setup.sh                 # everything
#   bash setup.sh --skip-models   # do not download models now
set -euo pipefail
cd "$(dirname "$0")"

SKIP_MODELS=0
for a in "$@"; do
  case "$a" in
    --skip-models) SKIP_MODELS=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done

say()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# -------------------------------------------------------------- 1. ollama ---
if ! command -v ollama >/dev/null 2>&1; then
  cat <<'EOF'
Ollama is not installed. Install it, then re-run this script:
  macOS:   brew install ollama          (or the app from https://ollama.com/download)
  Linux:   curl -fsSL https://ollama.com/install.sh | sh
  Windows: use WSL2 and follow the Linux line
EOF
  exit 1
fi
say "found $(ollama --version 2>/dev/null | tail -1)"

# ---------------------------------------------------------------- 2. venv ---
if ! command -v uv >/dev/null 2>&1 && [ -x "$HOME/.local/bin/uv" ]; then export PATH="$HOME/.local/bin:$PATH"; fi
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then
    say "creating .venv with uv (Python 3.12)"
    uv venv --python 3.12 .venv
  elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    say "creating .venv with $(python3 --version)"
    python3 -m venv .venv
  else
    die "need Python >= 3.10 (or uv: curl -LsSf https://astral.sh/uv/install.sh | sh)"
  fi
else
  say ".venv already exists ($(.venv/bin/python --version))"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
say "installing requirements.txt"
if command -v uv >/dev/null 2>&1; then
  uv pip install -q -r requirements.txt
else
  python -m pip install -q --upgrade pip
  python -m pip install -q -r requirements.txt
fi
say "$(smg --version 2>/dev/null | head -1)"

# ------------------------------------------------------------- 3. lab.env ---
if [ ! -f lab.env ]; then
  cp lab.env.example lab.env
  say "created lab.env from lab.env.example"
else
  say "keeping your existing lab.env"
fi

# -------------------------------------------------- 4. models + verify ---
if [ "$SKIP_MODELS" -eq 0 ]; then
  python verify_environment.py --pull
else
  python verify_environment.py
fi

cat <<EOF

Done. In every new terminal:
    cd $(pwd) && source .venv/bin/activate

Next: read docs/01_concepts.md, then LAB_GUIDE.md from Task 1.
EOF
