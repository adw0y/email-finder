#!/usr/bin/env bash
# Mac / Linux: first run sets everything up, later runs just open the app.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi
./.venv/bin/streamlit run app.py
