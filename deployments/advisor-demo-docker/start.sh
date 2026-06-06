#!/usr/bin/env bash
# Start the full LIF advisor stack: MDR, GraphQL, semantic search MCP, advisor API + app.
# MCP is reachable at http://localhost:8003/mcp once healthy.

set -euo pipefail

cd "$(dirname "$0")"

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "ERROR: OPENAI_API_KEY is not set. Run: export OPENAI_API_KEY=sk-..." >&2
  exit 1
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "WARNING: ANTHROPIC_API_KEY is not set — MDR AI mapping suggestions will fail." >&2
fi

BUILD_FLAG="--build"
DETACH_FLAG="-d"
for arg in "$@"; do
  case "$arg" in
    --no-build) BUILD_FLAG="" ;;
    --foreground|-f) DETACH_FLAG="" ;;
  esac
done

docker compose up $BUILD_FLAG $DETACH_FLAG

if [[ -n "$DETACH_FLAG" ]]; then
  echo
  echo "Stack started. Waiting for MCP health..."
  for i in {1..40}; do
    if curl -sf http://localhost:8003/health >/dev/null 2>&1; then
      echo "MCP healthy."
      break
    fi
    sleep 3
  done

  cat <<EOF

Endpoints:
  Advisor app    http://localhost:5174
  Advisor API    http://localhost:8004
  MCP server     http://localhost:8003/mcp     (health: /health)
  GraphQL org1   http://localhost:8010/graphql
  MDR API        http://localhost:8012
  MDR app        http://localhost:5173

Tail logs:   docker compose logs -f lif-advisor-api lif-semantic-search-mcp-server
Stop:        docker compose down            (keep volumes)
Stop + wipe: docker compose down -v
EOF
fi
