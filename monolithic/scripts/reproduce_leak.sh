#!/bin/bash
#
# reproduce_leak.sh — Orchestrate memory leak reproduction for insights-on-prem.
#
# Starts the monolithic docker-compose stack, waits for readiness,
# launches monitoring + load generation, and prints a summary on exit.
#
# Usage:
#   ./reproduce_leak.sh                # 60 min, 30% bad archives
#   ./reproduce_leak.sh 120            # 120 min, 30% bad archives
#   ./reproduce_leak.sh 120 0.5        # 120 min, 50% bad archives
#   ./reproduce_leak.sh 60 0.0         # 60 min, all valid archives
#   ./reproduce_leak.sh 60 0.3 --burst # 60 min, burst mode

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DURATION_MIN="${1:-60}"
BAD_RATIO="${2:-0.3}"
EXTRA_ARGS="${3:-}"
OUTPUT_DIR="${SCRIPT_DIR}/monitoring_$(date +%Y%m%d_%H%M%S)"
MONITOR_PID=""
SEND_PID=""

cleanup() {
    echo ""
    echo "=== Shutting down ==="

    if [ -n "$SEND_PID" ] && kill -0 "$SEND_PID" 2>/dev/null; then
        echo "Stopping load generator (PID $SEND_PID)..."
        kill "$SEND_PID" 2>/dev/null || true
        wait "$SEND_PID" 2>/dev/null || true
    fi

    if [ -n "$MONITOR_PID" ] && kill -0 "$MONITOR_PID" 2>/dev/null; then
        echo "Stopping monitor (PID $MONITOR_PID)..."
        kill "$MONITOR_PID" 2>/dev/null || true
        wait "$MONITOR_PID" 2>/dev/null || true
    fi

    echo ""
    echo "=== Memory Summary ==="
    if [ -d "$OUTPUT_DIR" ]; then
        for csv in "$OUTPUT_DIR"/*_podman_stats.csv; do
            [ -f "$csv" ] || continue
            container=$(basename "$csv" | sed 's/_podman_stats.csv//')
            awk -F',' -v name="$container" 'NR>1 && $4+0>0 {
                if(!first) { first=$4+0; first_t=$2 }
                last=$4+0; last_t=$2
            } END {
                if(first && last_t>first_t) {
                    hours=(last_t-first_t)/60
                    rate=(last-first)/hours
                    printf "  %-20s start=%.1f MB  end=%.1f MB  delta=%+.1f MB  rate=%+.2f MB/hr\n", name, first, last, last-first, rate
                }
            }' "$csv"
        done

        if [ -f "$OUTPUT_DIR/SUMMARY.txt" ]; then
            echo ""
            cat "$OUTPUT_DIR/SUMMARY.txt"
        fi

        echo ""
        echo "Monitoring data: $OUTPUT_DIR/"
    fi

    echo ""
    read -r -p "Stop containers? (podman compose down) [y/N] " answer
    if [[ "$answer" =~ ^[Yy] ]]; then
        echo "Stopping containers..."
        podman compose -f "$COMPOSE_DIR/docker-compose.yml" down
    else
        echo "Containers left running. Stop with:"
        echo "  podman compose -f $COMPOSE_DIR/docker-compose.yml down"
    fi
}

trap cleanup EXIT INT TERM

# --- Pre-flight checks ---
echo "=== Pre-flight checks ==="

if [ ! -f "$COMPOSE_DIR/docker-compose.yml" ]; then
    echo "ERROR: docker-compose.yml not found at $COMPOSE_DIR"
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/monitor.sh" ]; then
    echo "ERROR: monitor.sh not found in $SCRIPT_DIR"
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/send_archives.py" ]; then
    echo "ERROR: send_archives.py not found in $SCRIPT_DIR"
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found"
    exit 1
fi

echo "  Compose dir: $COMPOSE_DIR"
echo "  Duration:    ${DURATION_MIN} minutes"
echo "  Bad ratio:   ${BAD_RATIO}"
echo "  Output:      $OUTPUT_DIR"
echo ""

# --- Start services ---
echo "=== Starting services ==="
podman compose -f "$COMPOSE_DIR/docker-compose.yml" up -d

echo ""
echo "Waiting for app to be ready..."

MAX_WAIT=120
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/health 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        echo "  App is ready (HTTP 200 on /health)"
        break
    fi
    sleep 5
    WAITED=$((WAITED + 5))
    echo "  Waiting... (${WAITED}s, last status: ${HTTP_CODE})"
done

if [ $WAITED -ge $MAX_WAIT ]; then
    echo "ERROR: Timed out waiting for app to be ready"
    echo "Check: podman compose -f $COMPOSE_DIR/docker-compose.yml logs"
    exit 1
fi

# --- Check insights-core fix status ---
echo ""
echo "=== Checking insights-core fix status ==="
FIX_CHECK=$(podman exec insights-app python3 -c "
import inspect, insights.core.dr as dr
src = inspect.getsource(dr.run)
if '__traceback__ = None' in src or '__traceback__=None' in src:
    print('PRESENT')
else:
    print('MISSING')
" 2>/dev/null || echo "UNKNOWN")

if [ "$FIX_CHECK" = "PRESENT" ]; then
    echo "  insights-core fix: PRESENT (ex.__traceback__ = None in dr.run)"
elif [ "$FIX_CHECK" = "MISSING" ]; then
    echo "  insights-core fix: MISSING — this run should reproduce the leak"
else
    echo "  insights-core fix: could not determine"
fi
echo ""

# --- Start monitoring ---
echo "=== Starting monitoring ==="
bash "$SCRIPT_DIR/monitor.sh" "$DURATION_MIN" "$OUTPUT_DIR" &
MONITOR_PID=$!
echo "  Monitor PID: $MONITOR_PID"

sleep 3

# --- Start load generation ---
echo ""
echo "=== Starting load generation ==="

SEND_ARGS=(
    --duration "$DURATION_MIN"
    --bad-ratio "$BAD_RATIO"
)

if [ "$EXTRA_ARGS" = "--burst" ]; then
    SEND_ARGS+=(--burst)
    echo "  Mode: burst (10min send + 1min break)"
else
    echo "  Mode: continuous"
fi

echo "  Duration: ${DURATION_MIN} min"
echo "  Bad ratio: ${BAD_RATIO}"
echo ""

python3 "$SCRIPT_DIR/send_archives.py" "${SEND_ARGS[@]}" &
SEND_PID=$!
echo "  Load generator PID: $SEND_PID"

echo ""
echo "=== Running ==="
echo "  Monitor:  $MONITOR_PID"
echo "  Load gen: $SEND_PID"
echo "  Duration: ${DURATION_MIN} minutes"
echo "  Press Ctrl+C to stop early"
echo ""

# Wait for load generator to finish
wait "$SEND_PID" 2>/dev/null || true
SEND_PID=""

echo ""
echo "Load generation complete. Waiting for monitor to finish..."
sleep 15

if [ -n "$MONITOR_PID" ] && kill -0 "$MONITOR_PID" 2>/dev/null; then
    kill "$MONITOR_PID" 2>/dev/null || true
    wait "$MONITOR_PID" 2>/dev/null || true
fi
MONITOR_PID=""

echo "Done."
