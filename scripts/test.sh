#!/usr/bin/env bash
# Run the automated tests in an isolated compose project ("aigw-test"); real data is never touched
# and no real model provider is called (LiteLLM routes to tests/mock_upstream.py).
#   scripts/test.sh          unit + integration + security tests, then the end-to-end kill-switch proof
#   scripts/test.sh unit     only the pytest suites
set -euo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME=aigw-test COMPOSE_FILE=docker-compose.yml:docker-compose.test.yml
export GATEWAY_PORT=18080 MOCK_PORT=19000
docker compose --profile tests build -q   # the tests image is behind a profile
docker compose --profile tests run --rm tests
[ "${1:-}" = unit ] || python3 tests/e2e_killswitch.py
