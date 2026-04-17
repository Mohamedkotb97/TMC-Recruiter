#!/bin/bash
# Quick start for the Recruiter CRM backend
set -e

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "→ Creating venv..."
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

# Default env vars — override by exporting before running
export CRM_API_KEY="${CRM_API_KEY:-dev-key-change-me}"

if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "⚠  ANTHROPIC_API_KEY not set — AI features will fail."
  echo "   Set it with:  export ANTHROPIC_API_KEY=sk-ant-..."
fi

echo ""
echo "→ CRM_API_KEY is: $CRM_API_KEY"
echo "→ Starting server at http://localhost:8000"
echo "   Web UI:      http://localhost:8000"
echo "   Health:      http://localhost:8000/api/health"
echo ""

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
