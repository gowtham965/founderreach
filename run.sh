#!/usr/bin/env bash
# One-command local setup + run (macOS / Linux).
#   ./run.sh          start the app on http://localhost:8000
#   ./run.sh test     run the test suite
#   ./run.sh demo     start with the offline fake LLM (no API key needed)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtualenv…"
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  secret=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
  # BSD sed (macOS) needs the '' after -i
  if sed --version >/dev/null 2>&1; then sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$secret|" .env
  else sed -i '' "s|^SECRET_KEY=.*|SECRET_KEY=$secret|" .env; fi
  echo "Created .env — add your ANTHROPIC_API_KEY (and Google OAuth keys for Gmail) to it."
fi

case "${1:-run}" in
  test) python -m pytest -q tests ;;
  demo) LLM_PROVIDER=fake exec uvicorn app.main:app --reload --port 8000 ;;
  *)    exec uvicorn app.main:app --reload --port 8000 ;;
esac
