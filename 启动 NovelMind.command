#!/bin/zsh
cd -- "$(dirname -- "$0")"
exec .venv/bin/python scripts/novelmind_launcher.py  "$@"
