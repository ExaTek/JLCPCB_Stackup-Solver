#!/bin/sh
# Start from source on Linux/macOS: the first run makes .venv and installs the requirements.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv || { echo "Python 3.10+ with venv is required (e.g. sudo apt install python3-venv)"; exit 1; }
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -m jlc "$@"
