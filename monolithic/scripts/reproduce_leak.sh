#!/bin/bash
#
# reproduce_leak.sh — Orchestrate memory leak reproduction for insights-on-prem.
#
# Starts the monolithic docker-compose stack, waits for readiness,
# launches monitoring + load generation, and prints a summary on exit.
#
# Usage:
#   ./reproduce_leak.sh                # 30 min, 100% bad archives, max speed
#   ./reproduce_leak.sh 60             # 60 min
#   ./reproduce_leak.sh 120 0.5        # 120 min, 50% bad archives

set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'EOF'
Usage: reproduce_leak.sh [DURATION_MIN] [BAD_RATIO] [--use-molodec]

Reproduce insights-core memory leak in the monolithic deployment.

  DURATION_MIN    How long to run in minutes (default: 30)
  BAD_RATIO       Fraction of bad archives 0.0-1.0 (default: 1.0)
  --use-molodec   Use molodec for realistic OCP archives instead of
                  self-contained bad archives (requires venv with molodec)

What it does:
  1. Sets up a Python venv with molodec (if not present)
  2. Tears down any existing containers and volumes
  3. Starts a fresh podman compose stack
  4. Checks whether the insights-core traceback fix is applied
  5. Uploads archives at max speed while monitoring CPU/mem/disk
  6. Prints a leak evaluation report (skips first 5 min warm-up)
  7. Stops containers on exit

Examples:
  ./reproduce_leak.sh                    # 30 min, 100% bad archives
  ./reproduce_leak.sh 60                 # 60 min
  ./reproduce_leak.sh 120 0.5            # 2 hours, 50% bad archives
  ./reproduce_leak.sh 30 0.3 --use-molodec  # molodec archives, 30% bad

Press Ctrl+C to stop early — summary is still printed.
EOF
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Parse args: strip flags first, then assign positional
USE_MOLODEC=""
POSITIONAL=()
for arg in "$@"; do
    case "$arg" in
        --use-molodec) USE_MOLODEC="--use-molodec" ;;
        --help|-h)     ;; # handled above
        *)             POSITIONAL+=("$arg") ;;
    esac
done
DURATION_MIN="${POSITIONAL[0]:-30}"
BAD_RATIO="${POSITIONAL[1]:-1.0}"
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

    # Report for insights-app (skip first 5 min warm-up)
    WARMUP_MIN=5
    CSV="$OUTPUT_DIR/insights-app_podman_stats.csv"
    DISK_CSV="$OUTPUT_DIR/insights-app_disk_usage.csv"

    if [ -f "$CSV" ] && [ "$(wc -l < "$CSV")" -gt 1 ]; then
        echo ""
        awk -F',' -v warmup="$WARMUP_MIN" '
        NR>1 && $4+0>0 {
            if(!got_first) { first=$4+0; got_first=1 }
            if($2+0 >= warmup && !got_steady) { steady=$4+0; steady_t=$2+0; got_steady=1 }
            last=$4+0; last_t=$2+0
        } END {
            if(!got_steady) { steady=first; steady_t=0; got_steady=1 }
            mins = last_t - steady_t
            if(mins < 1) mins = 1
            rate = (last - steady) / (mins / 60)

            printf "=== Report (insights-app) ===\n"
            printf "  Warm-up:  %.1f -> %.1f MiB (first %d min, excluded)\n", first, steady, warmup
            printf "  Steady:   %.1f -> %.1f MiB  delta=%+.1f MiB  over %d min\n", steady, last, last-steady, mins
            printf "  Rate:     %+.1f MiB/hr\n", rate

            if(rate < 1)
                printf "  Verdict:  STABLE — no leak detected\n"
            else if(rate < 10)
                printf "  Verdict:  POSSIBLE LEAK — moderate growth (%.1f MiB/hr)\n", rate
            else
                printf "  Verdict:  LEAK DETECTED — significant growth (%.1f MiB/hr)\n", rate
        }' "$CSV"

        # Disk usage
        if [ -f "$DISK_CSV" ] && [ "$(wc -l < "$DISK_CSV")" -gt 1 ]; then
            awk -F',' 'NR>1 && $3+0>=0 {
                n++;
                if(n==1) first=$3+0;
                last=$3+0;
            } END {
                if(n>0) printf "  Disk:     %d -> %d MB (%+d MB)\n", first, last, last-first
            }' "$DISK_CSV"
        fi

        echo ""
        echo "Full data: $OUTPUT_DIR/"
    fi

    echo "Stopping containers..."
    podman compose -f "$COMPOSE_DIR/docker-compose.yml" down 2>/dev/null || true
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

# --- Set up venv if needed ---
VENV_DIR="$SCRIPT_DIR/venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "=== Setting up Python venv ==="
    bash "$SCRIPT_DIR/setup_venv.sh"
    echo ""
fi

PYTHON="$VENV_DIR/bin/python3"
if [ ! -x "$PYTHON" ]; then
    echo "ERROR: python3 not found at $PYTHON"
    echo "Run: ./scripts/setup_venv.sh"
    exit 1
fi

echo "  Compose dir: $COMPOSE_DIR"
echo "  Duration:    ${DURATION_MIN} minutes"
echo "  Bad ratio:   ${BAD_RATIO}"
echo "  Output:      $OUTPUT_DIR"
echo ""

# --- Clean up previous run ---
echo "=== Cleaning up previous run ==="
podman compose -f "$COMPOSE_DIR/docker-compose.yml" down --volumes 2>/dev/null || true
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

echo "  Mode: continuous, max speed"
echo "  Duration: ${DURATION_MIN} min"
echo "  Bad ratio: ${BAD_RATIO}"
if [ -n "$USE_MOLODEC" ]; then
    echo "  Archives: molodec (realistic OCP)"
fi
echo ""

"$PYTHON" "$SCRIPT_DIR/send_archives.py" \
    --duration "$DURATION_MIN" \
    --bad-ratio "$BAD_RATIO" \
    --delay 0 \
    $USE_MOLODEC &
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
