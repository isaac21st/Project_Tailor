#!/usr/bin/env bash
# One-time setup on a Mac (also works on Linux):  bash scripts/setup_mac.sh
# Installs the Python packages and the PDF browser under ~/project_tailor_data, then runs the self-check.
# It downloads about 700 MB (packages + Chromium). Nothing personal leaves the machine.
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${TAILOR_DATA:-$HOME/project_tailor_data}"
PYTHON="${PYTHON:-python3}"

echo "Project: $PROJECT"
echo "Data:    $DATA   (your profile lives here: keep it out of iCloud / OneDrive)"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python 3.12 or 3.13 is needed: https://www.python.org/downloads/  (or: brew install python@3.13)"
  exit 1
fi
"$PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 11):
    sys.exit("Python %s found; Project Tailor needs 3.11 or newer (3.12 / 3.13 recommended)" % sys.version.split()[0])
PY

mkdir -p "$DATA"
FREE_KB="$(df -k "$DATA" | awk 'NR==2 {print $4}')"
FREE_GB="$(awk -v k="$FREE_KB" 'BEGIN {printf "%.1f", k/1024/1024}')"
echo "Free disk space here: ${FREE_GB} GB"
echo "  this setup needs about 0.6 GB (packages + a small headless browser); the local model is extra:"
echo "  qwen3:8b about 5 GB, qwen3:4b-instruct about 2.5 GB, qwen3:1.7b about 1.4 GB (or skip the model: everything except"
echo "  writing free-text answers works without it)."
if [ "$FREE_KB" -lt 1000000 ]; then
  echo "  WARNING: under 1 GB free. Free some space first, or expect the install to fail."
fi
if [ ! -f "$DATA/profile/base_resume.json" ]; then
  echo "Missing $DATA/profile/base_resume.json"
  echo "Unzip the personal-data bundle into $DATA first (it contains the 'profile' folder)."
  exit 1
fi

"$PYTHON" -m venv "$DATA/venv"
"$DATA/venv/bin/python" -m pip install --no-cache-dir --upgrade pip
"$DATA/venv/bin/python" -m pip install --no-cache-dir -r "$PROJECT/requirements.txt"

export PLAYWRIGHT_BROWSERS_PATH="$DATA/browsers"
# Chromium for the PDFs and for the extension test (the extension itself runs in your own Chrome)
"$DATA/venv/bin/python" -m playwright install chromium

echo
"$DATA/venv/bin/python" "$PROJECT/scripts/check_setup.py" || true

cat <<EOF

Next:
  1. Local model (optional, this is the big download):
       Install Ollama (https://ollama.com/download), then ONE of:
         ollama pull qwen3:8b     (about 5 GB, best writing)
         ollama pull qwen3:4b-instruct  (about 2.5 GB, a good middle)
         ollama pull qwen3:1.7b   (about 1.4 GB, fits tight disks, plainer writing)
       Without a model everything still works except the free-text writing help.
  2. Start the server:
       "$DATA/venv/bin/python" "$PROJECT/scripts/run_server.py" --model ollama --ollama-model qwen3:8b
     (no model? use:  --model stub)
  3. In Chrome: chrome://extensions -> Developer mode -> Load unpacked -> $PROJECT/extension
     Then open the extension's Options page and paste the token from $DATA/server_token.txt
EOF
