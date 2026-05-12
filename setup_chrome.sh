#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "Checking Google Chrome installation..."
if [[ "$(uname)" == "Darwin" ]]; then
  CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  if [[ ! -x "$CHROME" ]]; then
    echo "Install Google Chrome from https://www.google.com/chrome/"
    exit 1
  fi
  echo "OK: $CHROME"
else
  if ! command -v google-chrome >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1; then
    echo "Install Google Chrome or Chromium for your OS."
    exit 1
  fi
  echo "OK: Chrome/Chromium found on PATH"
fi

echo "Creating Python virtual environment..."
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp env.example .env
  echo "Created .env from env.example — edit it with your DMV details and SMTP settings."
fi

echo "Setup complete. Selenium 4 will download a matching ChromeDriver when you first run the monitor."
echo "Run: source .venv/bin/activate && python monitor.py"
