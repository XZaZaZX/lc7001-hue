#!/bin/bash
# Sets up a private Python environment for lc7001-hue, then runs the wizard.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "lc7001-hue installer"
echo "===================="

PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
      PY="$candidate"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "No Python 3.9+ found."
  echo "Install one with:  brew install python3"
  echo "(or install Xcode Command Line Tools:  xcode-select --install)"
  exit 1
fi

echo "Using $($PY --version) at $(command -v "$PY")"

if [ ! -d "$HERE/venv" ]; then
  echo "Creating a private Python environment in ./venv ..."
  "$PY" -m venv "$HERE/venv"
fi

echo "Installing dependencies ..."
"$HERE/venv/bin/pip" install --quiet --upgrade pip
"$HERE/venv/bin/pip" install --quiet -r "$HERE/requirements.txt"

echo
echo "Dependencies installed."
echo

if [ -f "$HERE/config.json" ]; then
  echo "An existing config.json was found."
  read -r -p "Run the setup wizard again? (y/N): " again
  case "$again" in
    [yY]*) "$HERE/venv/bin/python" "$HERE/setup_wizard.py" ;;
    *) echo "Keeping the existing config." ;;
  esac
else
  "$HERE/venv/bin/python" "$HERE/setup_wizard.py"
fi

echo
echo "Next:"
echo "  ./service.sh test      # check it can reach both hubs"
echo "  ./service.sh install   # keep it running in the background, always"
