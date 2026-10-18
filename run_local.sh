#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT/backend"

if [[ ! -x ".venv/bin/python" ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
export GRIDWISE_DB_PATH="$PWD/gridwise.db"
echo "Starting GridWise EMS at http://127.0.0.1:8000"
echo "API docs: http://127.0.0.1:8000/docs"
exec python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
