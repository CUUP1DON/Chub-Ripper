#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Find python3
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo "Could not find Python. Please install Python 3.11+ and make sure it is on your PATH."
    exit 1
fi

echo "Using Python: $("$PYTHON" -c 'import sys; print(sys.executable)') ($("$PYTHON" --version 2>&1))"
echo "Installing / updating requirements..."
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r requirements.txt

# Install Playwright browsers on first run (skips if already installed)
"$PYTHON" -m playwright install chromium --with-deps 2>/dev/null || true

"$PYTHON" chub_ripper.py
