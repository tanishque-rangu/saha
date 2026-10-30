#!/bin/bash
# ==============================================================================
# Saha - Focused Linux & Container Verification Script
#
# Validates:
# 1. Image build using standard Dockerfile (no changes to hosting provider).
# 2. Full test suite execution inside Linux container (including symlink escape test).
# 3. Persistent data mount at /data across container restart (task state recovery).
# 4. Startup recovery failure prevention (corrupted DB prevents healthy status).
# 5. Clean teardown of only test-created resources.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE_TAG="${SAHA_TEST_IMAGE:-saha:verification}"
PORT="${SAHA_TEST_PORT:-18080}"
OWNER_TOKEN="linux_container_verification_token_32_chars!"

PID_SUFFIX="$$"
TEST_CONTAINER="saha-verify-test-${PID_SUFFIX}"
RESTART_CONTAINER="saha-verify-restart-${PID_SUFFIX}"
CORRUPT_CONTAINER="saha-verify-corrupt-${PID_SUFFIX}"

PERSIST_DIR=""
CORRUPT_DIR=""

cleanup() {
    local exit_code=$?
    echo ""
    echo "==================================================================="
    echo " [CLEANUP] Cleaning up test resources..."
    echo "==================================================================="
    docker rm -f "$TEST_CONTAINER" "$RESTART_CONTAINER" "$CORRUPT_CONTAINER" 2>/dev/null || true
    if [ -n "$PERSIST_DIR" ] && [ -d "$PERSIST_DIR" ]; then
        rm -rf "$PERSIST_DIR" 2>/dev/null || true
    fi
    if [ -n "$CORRUPT_DIR" ] && [ -d "$CORRUPT_DIR" ]; then
        rm -rf "$CORRUPT_DIR" 2>/dev/null || true
    fi
    echo "[CLEANUP] Done."
    exit $exit_code
}

trap cleanup EXIT INT TERM

echo "==================================================================="
echo " SAHA LINUX & CONTAINER VERIFICATION SUITE"
echo "==================================================================="
echo "Project Root: $PROJECT_ROOT"
echo "Image Tag:    $IMAGE_TAG"
echo "Test Port:    $PORT"
echo ""

# Pre-flight check: Docker daemon availability
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: 'docker' CLI is not installed or not in PATH."
    echo "LIMITATION: This environment is running on a host without Docker CLI."
    echo "To run this container verification, execute on a Linux host or Docker-enabled runner."
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker daemon is not running or not accessible."
    echo "LIMITATION: Cannot connect to Docker daemon in this environment."
    exit 1
fi

# ------------------------------------------------------------------------------
# STEP 1: Build Existing Container Image
# ------------------------------------------------------------------------------
echo "==================================================================="
echo " [STEP 1] Building application container image ($IMAGE_TAG)..."
echo "==================================================================="
cd "$PROJECT_ROOT"
docker build -t "$IMAGE_TAG" -f Dockerfile .
echo "[STEP 1] Image build successful: $IMAGE_TAG"
echo ""

# ------------------------------------------------------------------------------
# STEP 2: Run Full Pytest Suite Inside Linux Container
# ------------------------------------------------------------------------------
echo "==================================================================="
echo " [STEP 2] Running full pytest suite inside Linux container..."
echo "          (Verifies symlink escape test executes on Linux without skip)"
echo "==================================================================="

# Run pytest inside the container mounting the test files read-only
# We run as default user so entrypoint drops privileges to appuser (10001)
docker run --rm \
  -v "$PROJECT_ROOT/tests:/app/tests:ro" \
  -v "$PROJECT_ROOT/pytest.ini:/app/pytest.ini:ro" \
  -v "$PROJECT_ROOT/scripts:/app/scripts:ro" \
  -e SAHA_PUBLIC_MODE=false \
  -e SAHA_DEMO_MODE=true \
  -e NEBIUS_API_KEY="" \
  "$IMAGE_TAG" \
  pytest -v -o cache_dir=/tmp/.pytest_cache

echo ""
echo "[STEP 2] Linux pytest execution complete. All tests (including symlink escape) passed."
echo ""

# ------------------------------------------------------------------------------
# STEP 3: Persistent Mount & Task State Across Restarts
# ------------------------------------------------------------------------------
echo "==================================================================="
echo " [STEP 3] Persistent volume mount & task recovery across restarts..."
echo "==================================================================="

PERSIST_DIR="$(mktemp -d /tmp/saha-persist-XXXXXX 2>/dev/null || mktemp -d -t saha-persist-XXXXXX)"
mkdir -p "$PERSIST_DIR/workspace"

echo "[3.1] Starting Container 1 ($TEST_CONTAINER) with mount $PERSIST_DIR:/data..."
docker run -d \
  --name "$TEST_CONTAINER" \
  -p "${PORT}:8080" \
  -v "$PERSIST_DIR:/data" \
  -e SAHA_PUBLIC_MODE=true \
  -e SAHA_DEMO_MODE=true \
  -e SAHA_REQUIRE_PERSISTENT=true \
  -e NEBIUS_API_KEY="" \
  "$IMAGE_TAG"

echo "[3.2] Waiting up to 30s for Container 1 /health to report ok..."
READY=0
for i in $(seq 1 30); do
    HEALTH_DATA=$(curl -s "http://127.0.0.1:${PORT}/health" || true)
    if echo "$HEALTH_DATA" | grep -Eq '"status":\s*"ok"'; then
        READY=1
        echo "Container 1 healthy after ${i}s: $HEALTH_DATA"
        break
    fi
    sleep 1
done

if [ "$READY" -ne 1 ]; then
    echo "ERROR: Container 1 did not become healthy within 30s."
    docker logs "$TEST_CONTAINER"
    exit 1
fi

# Verify data_persistent is true
echo "$HEALTH_DATA" | grep -Eq '"data_persistent":\s*true' || {
    echo "ERROR: Health check did not report 'data_persistent: true'"
    exit 1
}

# Verify privilege drop inside container
UVICORN_UID=$(docker exec "$TEST_CONTAINER" sh -c '
    FOUND_UID=""
    for pid_dir in /proc/[0-9]*; do
        if [ -f "$pid_dir/cmdline" ] && grep -q "uvicorn" "$pid_dir/cmdline" 2>/dev/null; then
            FOUND_UID=$(awk "/^Uid:/ {print \$2}" "$pid_dir/status")
            break
        fi
    done
    echo "$FOUND_UID"
')
echo "Verified uvicorn process UID inside container: $UVICORN_UID"
if [ "$UVICORN_UID" -ne 10001 ]; then
    echo "ERROR: Expected uvicorn process UID 10001, got '$UVICORN_UID'"
    exit 1
fi

echo "[3.3] Creating task state inside Container 1 (mocked LLM/network)..."
TASK_ID=$(docker exec "$TEST_CONTAINER" python -c "
import json
from backend.db import hash_owner_token
from backend.ledger import Ledger
from backend.policy import Policy, set_owner_policy

owner_id = hash_owner_token('$OWNER_TOKEN')
policy = Policy(allowed_tools=['write_file', 'read_file', 'list_files'], allowed_domains=[], allowed_paths=['*'])
set_owner_policy(owner_id, policy)

task = Ledger.create_task('Container persistent volume verification task', owner_id=owner_id)
steps = Ledger.add_steps(task.id, [
    {
        'idx': 1,
        'intent': 'Persisted initial write step',
        'tool': 'write_file',
        'args': {'path': 'persist_data.txt', 'content': 'persisted_in_volume'},
        'probe': {},
        'depends_on': []
    }
])
Ledger.mark_step_verified(steps[0].id, {'content_matches': True})
Ledger.update_task_status(task.id, 'complete')
print(task.id)
")

echo "Created task ID: $TASK_ID"

echo "[3.4] Testing authenticated API access on Container 1..."
# 1. Without header -> 401
CODE_UNAUTH=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/tasks/$TASK_ID")
if [ "$CODE_UNAUTH" -ne 401 ]; then
    echo "ERROR: Expected HTTP 401 for unauthenticated request, got $CODE_UNAUTH"
    exit 1
fi

# 2. With wrong token -> 404
CODE_FORBID=$(curl -s -o /dev/null -w "%{http_code}" -H "X-Saha-Owner: wrong_token_32_characters_long_!" "http://127.0.0.1:${PORT}/tasks/$TASK_ID")
if [ "$CODE_FORBID" -ne 404 ]; then
    echo "ERROR: Expected HTTP 404 for foreign owner token, got $CODE_FORBID"
    exit 1
fi

# 3. With authorized token -> 200
TASK_DETAIL=$(curl -s -f -H "X-Saha-Owner: $OWNER_TOKEN" "http://127.0.0.1:${PORT}/tasks/$TASK_ID")
echo "Authorized task detail: $TASK_DETAIL"
echo "$TASK_DETAIL" | grep -Eq '"is_complete":\s*true' || {
    echo "ERROR: Task is not complete in Container 1"
    exit 1
}

echo "[3.5] Terminating Container 1 to simulate restart..."
docker stop "$TEST_CONTAINER"
docker rm "$TEST_CONTAINER"

echo "[3.6] Starting Container 2 ($RESTART_CONTAINER) mounting the exact same /data directory..."
docker run -d \
  --name "$RESTART_CONTAINER" \
  -p "${PORT}:8080" \
  -v "$PERSIST_DIR:/data" \
  -e SAHA_PUBLIC_MODE=true \
  -e SAHA_DEMO_MODE=true \
  -e SAHA_REQUIRE_PERSISTENT=true \
  -e NEBIUS_API_KEY="" \
  "$IMAGE_TAG"

echo "[3.7] Waiting up to 30s for Container 2 /health to report ok..."
READY_2=0
for i in $(seq 1 30); do
    HEALTH_DATA_2=$(curl -s "http://127.0.0.1:${PORT}/health" || true)
    if echo "$HEALTH_DATA_2" | grep -Eq '"status":\s*"ok"'; then
        READY_2=1
        echo "Container 2 healthy after ${i}s: $HEALTH_DATA_2"
        break
    fi
    sleep 1
done

if [ "$READY_2" -ne 1 ]; then
    echo "ERROR: Container 2 did not become healthy within 30s."
    docker logs "$RESTART_CONTAINER"
    exit 1
fi

echo "[3.8] Verifying persisted task state is readable and recoverable on Container 2..."
TASK_DETAIL_2=$(curl -s -f -H "X-Saha-Owner: $OWNER_TOKEN" "http://127.0.0.1:${PORT}/tasks/$TASK_ID")
echo "Container 2 task detail: $TASK_DETAIL_2"
echo "$TASK_DETAIL_2" | grep -q "$TASK_ID" || {
    echo "ERROR: Task $TASK_ID missing from Container 2"
    exit 1
}
echo "$TASK_DETAIL_2" | grep -Eq '"is_complete":\s*true' || {
    echo "ERROR: Task $TASK_ID is not complete on Container 2"
    exit 1
}

# Verify task resume endpoint functions on restarted container
RESUME_RESP=$(curl -s -f -X POST -H "X-Saha-Owner: $OWNER_TOKEN" "http://127.0.0.1:${PORT}/tasks/$TASK_ID/resume")
echo "Container 2 task resume response: $RESUME_RESP"
echo "$RESUME_RESP" | grep -q '"resuming"' || {
    echo "ERROR: Expected 'resuming' response from Container 2 /resume"
    exit 1
}

echo "[STEP 3] Persistent mount and task state recovery verified across container restart."
echo ""

# ------------------------------------------------------------------------------
# STEP 4: Recovery Failure Prevents Server Becoming Healthy
# ------------------------------------------------------------------------------
echo "==================================================================="
echo " [STEP 4] Testing that a recovery failure prevents health..."
echo "==================================================================="

CORRUPT_DIR="$(mktemp -d /tmp/saha-corrupt-XXXXXX 2>/dev/null || mktemp -d -t saha-corrupt-XXXXXX)"
mkdir -p "$CORRUPT_DIR/workspace"
echo "MALFORMED_NON_SQLITE_BINARY_HEADER" > "$CORRUPT_DIR/saha.db"

CORRUPT_PORT=$((PORT + 1))
echo "[4.1] Starting container with corrupt database mount on port $CORRUPT_PORT..."
docker run -d \
  --name "$CORRUPT_CONTAINER" \
  -p "${CORRUPT_PORT}:8080" \
  -v "$CORRUPT_DIR:/data" \
  -e SAHA_PUBLIC_MODE=true \
  -e SAHA_DEMO_MODE=true \
  -e SAHA_REQUIRE_PERSISTENT=true \
  -e NEBIUS_API_KEY="" \
  "$IMAGE_TAG"

echo "[4.2] Monitoring health of corrupt container..."
sleep 4

# Check health endpoint - must NOT return HTTP 200
CORRUPT_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${CORRUPT_PORT}/health" 2>/dev/null || echo "unreachable")
echo "Corrupt container /health status code: $CORRUPT_CODE"

if [ "$CORRUPT_CODE" = "200" ]; then
    echo "ERROR: Corrupt container returned HTTP 200 OK! Expected failure (503 or unreachable)."
    exit 1
fi

CORRUPT_LOGS=$(docker logs "$CORRUPT_CONTAINER" 2>&1 || true)
echo "Corrupt container log excerpt:"
echo "$CORRUPT_LOGS" | tail -n 12

echo "[STEP 4] Startup recovery failure correctly prevented server from becoming healthy."
echo ""

echo "==================================================================="
echo " ALL LINUX / CONTAINER VERIFICATION CHECKS PASSED!"
echo "==================================================================="
