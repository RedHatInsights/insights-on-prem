#!/bin/bash
#
# reproduce_leak.sh — Orchestrate memory leak reproduction for insights-on-prem.
#
# Starts the monolithic docker-compose stack, waits for readiness,
# launches monitoring + load generation, and prints a summary on exit.
#
# Usage:
#   ./reproduce_leak.sh                    # 30 min, molodec, 3 workers
#   ./reproduce_leak.sh 60                 # 60 min
#   ./reproduce_leak.sh 60 0.3             # 60 min, 30% bad archives
#   ./reproduce_leak.sh --help             # show all options

set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'EOF'
Usage: reproduce_leak.sh [DURATION_MIN] [BAD_RATIO] [OPTIONS]

Reproduce insights-core memory leak in the monolithic deployment.

Positional arguments:
  DURATION_MIN    How long to run in minutes (default: 30)
  BAD_RATIO       Fraction of bad archives 0.0-1.0 (default: 0.0)

Options:
  --no-molodec         Use self-contained archives instead of molodec
  --parallel N         Number of parallel upload workers (default: 3)
  --archives-count N   Max archives to send total (default: 0 = unlimited)
  --wait               Wait for app to finish processing all sent archives before exiting
  --delay N            Seconds between uploads per worker (default: 0)
  --burst         Burst mode: 10 min send + 1 min break cycles
  --cooldown N    Minutes of idle monitoring after load stops (default: 5)
  --max-exceptions-archive  Use max_exceptions_archive.tar for uploads
  --memray        Profile the app with memray (installs in container, wraps uvicorn)
  --keep          Keep containers running after the test finishes
  --url URL       Upload endpoint (default: http://localhost:8000/api/ingress/v1/upload)
  -h, --help      Show this help

What it does:
  1. Sets up a Python venv with molodec (if not present)
  2. Tears down any existing containers and volumes
  3. Starts a fresh podman compose stack
  4. Checks whether the insights-core traceback fix is applied
  5. Uploads molodec archives with parallel workers while monitoring
  6. Prints a leak evaluation report (skips first 5 min warm-up)
  7. Stops containers on exit

Examples:
  ./reproduce_leak.sh                        # 30 min, molodec, 3 workers
  ./reproduce_leak.sh 60                     # 60 min
  ./reproduce_leak.sh 60 0.3                 # 60 min, 30% bad archives
  ./reproduce_leak.sh 30 0 --no-molodec      # self-contained archives
  ./reproduce_leak.sh 30 0 --parallel 5      # 5 parallel workers
  ./reproduce_leak.sh 60 0 --burst           # burst mode
  ./reproduce_leak.sh 30 0 --cooldown 10     # 10 min cool-down
  ./reproduce_leak.sh 10 0.3 --memray        # 10 min with memray profiling
  ./reproduce_leak.sh 30 0 --keep            # keep containers running after test

Press Ctrl+C to stop early — summary is still printed.
EOF
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Parse args: extract flags and key=value options, rest is positional
NO_MOLODEC=""
PARALLEL=""
ARCHIVES_COUNT=""
WAIT_FOR_PROCESSED=""
DELAY=""
BURST=""
COOLDOWN_MIN="5"
UPLOAD_URL=""
USE_MEMRAY=""
KEEP_CONTAINERS=""
MAX_EXCEPTIONS_ARCHIVE=""
POSITIONAL=()
while [ $# -gt 0 ]; do
    case "$1" in
        --no-molodec)     NO_MOLODEC="--no-molodec" ;;
        --burst)          BURST="--burst" ;;
        --cooldown)       COOLDOWN_MIN="$2"; shift ;;
        --parallel)       PARALLEL="$2"; shift ;;
        --archives-count) ARCHIVES_COUNT="$2"; shift ;;
        --wait)           WAIT_FOR_PROCESSED=1 ;;
        --delay)          DELAY="$2"; shift ;;
        --memray)     USE_MEMRAY=1 ;;
        --keep)       KEEP_CONTAINERS=1 ;;
        --max-exceptions-archive) MAX_EXCEPTIONS_ARCHIVE="$SCRIPT_DIR/max_exceptions_archive.tar" ;;
        --url)        UPLOAD_URL="$2"; shift ;;
        --help|-h)    ;; # handled above
        *)            POSITIONAL+=("$1") ;;
    esac
    shift
done
DURATION_MIN="${POSITIONAL[0]:-30}"
BAD_RATIO="${POSITIONAL[1]:-0.0}"
OUTPUT_DIR="${SCRIPT_DIR}/monitoring_$(date +%Y%m%d_%H%M%S)"
MONITOR_PID=""
SEND_PID=""
PROGRESS_PID=""

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

    if [ -n "$PROGRESS_PID" ] && kill -0 "$PROGRESS_PID" 2>/dev/null; then
        kill "$PROGRESS_PID" 2>/dev/null || true
        wait "$PROGRESS_PID" 2>/dev/null || true
    fi

    # Report for insights-app (monitoring started after warm-up)
    CSV="$OUTPUT_DIR/insights-app_podman_stats.csv"
    DISK_CSV="$OUTPUT_DIR/insights-app_disk_usage.csv"

    if [ -f "$CSV" ] && [ "$(wc -l < "$CSV")" -gt 1 ]; then
        echo ""
        awk -F',' -v load_min="$DURATION_MIN" '
        NR>1 && $4+0>0 {
            if(!n++) { first=$4+0; first_t=$2+0 }
            last=$4+0; last_t=$2+0
            if($2+0 <= load_min) { at_load_end=$4+0; at_load_end_t=$2+0 }
        } END {
            if(n>0) {
                mins = last_t - first_t
                if(mins < 1) mins = 1
                rate = (last - first) / (mins / 60)

                printf "=== Report (insights-app) ===\n"
                printf "  Start:    %.1f MiB\n", first
                if(at_load_end+0 > 0) {
                    printf "  At load stop: %.1f MiB  (after %d min)\n", at_load_end, at_load_end_t
                    printf "  After cool-down: %.1f MiB  (after %d min)\n", last, last_t
                    cooldown_delta = last - at_load_end
                    printf "  Cool-down delta: %+.1f MiB\n", cooldown_delta
                    if(cooldown_delta < -5)
                        printf "  Cool-down:  RELEASED — memory dropped after load stopped\n"
                    else if(cooldown_delta > 5)
                        printf "  Cool-down:  STILL GROWING — memory rose even without load\n"
                    else
                        printf "  Cool-down:  RETAINED — memory stayed flat (not released)\n"
                } else {
                    printf "  End:      %.1f MiB\n", last
                }
                printf "  Delta:    %+.1f MiB over %d min\n", last-first, mins
                printf "  Rate:     %+.1f MiB/hr\n", rate

                if(rate < 1)
                    printf "  Verdict:  STABLE — no leak detected\n"
                else if(rate < 10)
                    printf "  Verdict:  POSSIBLE LEAK — moderate growth (%.1f MiB/hr)\n", rate
                else
                    printf "  Verdict:  LEAK DETECTED — significant growth (%.1f MiB/hr)\n", rate
            }
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

    # Extract memray profile before stopping containers
    if [ -n "$USE_MEMRAY" ]; then
        echo ""
        echo "=== Extracting memray profile ==="
        if podman cp insights-app:/tmp/memray-profile.bin "$OUTPUT_DIR/memray-profile.bin" 2>/dev/null; then
            echo "  Profile: $OUTPUT_DIR/memray-profile.bin"
            if python3 -m memray flamegraph "$OUTPUT_DIR/memray-profile.bin" \
                -o "$OUTPUT_DIR/memray-flamegraph.html" 2>/dev/null; then
                echo "  Flamegraph: $OUTPUT_DIR/memray-flamegraph.html"
            else
                echo "  (install memray locally to generate flamegraph: pip install memray)"
            fi
        else
            echo "  WARNING: could not extract memray profile from container"
        fi
    fi

    if [ -n "$KEEP_CONTAINERS" ]; then
        echo "Containers kept running (--keep). Stop manually with:"
        echo "  podman compose -f $COMPOSE_DIR/docker-compose.yml down"
    else
        echo "Stopping containers..."
        podman compose -f "$COMPOSE_DIR/docker-compose.yml" down 2>/dev/null || true
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

TOTAL_MONITOR_MIN=$((DURATION_MIN + COOLDOWN_MIN + 60))

echo "  Compose dir: $COMPOSE_DIR"
echo "  Duration:    ${DURATION_MIN} minutes (+ ${COOLDOWN_MIN} min cool-down)"
echo "  Bad ratio:   ${BAD_RATIO}"
echo "  Memray:      $([ -n "$USE_MEMRAY" ] && echo enabled || echo disabled)"
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

# --- Enable memray profiling ---
if [ -n "$USE_MEMRAY" ]; then
    echo ""
    echo "=== Setting up memray profiling ==="
    echo "  Installing memray in container..."
    if ! podman exec --user root insights-app /usr/local/bin/pip3 install memray; then
        echo "ERROR: failed to install memray in container"
        exit 1
    fi

    echo "  Committing container with memray installed..."
    podman commit insights-app insights-app-memray:latest

    NETWORK_NAME=$(podman inspect --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' insights-postgres 2>/dev/null)

    # Capture bind-mount volumes from the running compose container
    # BEFORE stopping it.  The podman-run below bypasses compose,
    # so compose-defined volumes (e.g. patched insights-core and
    # ccx-ocp-core overlays) would be silently lost without this.
    COMPOSE_VOLUMES=()
    while IFS= read -r vol; do
        [ -n "$vol" ] && COMPOSE_VOLUMES+=(-v "$vol")
    done < <(podman inspect --format '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}:{{.Destination}}{{println}}{{end}}{{end}}' insights-app 2>/dev/null)

    podman compose -f "$COMPOSE_DIR/docker-compose.yml" stop app 2>/dev/null || true
    podman rm insights-app 2>/dev/null || true

    echo "  Restarting app under memray..."
    podman run -d \
        --name insights-app \
        --network "$NETWORK_NAME" \
        -p 8000:8000 -p 8443:8443 \
        -e POSTGRES_HOST=postgres \
        -e POSTGRES_PORT=5432 \
        -e POSTGRES_DB=insights \
        -e POSTGRES_USER=insights \
        -e POSTGRES_PASSWORD=insights \
        -e MAX_FILE_SIZE=104857600 \
        -e TEMP_UPLOAD_DIR=/tmp/insights-uploads \
        -e PYTHONUNBUFFERED=1 \
        "${COMPOSE_VOLUMES[@]}" \
        --memory 4g \
        insights-app-memray:latest \
        python -m memray run --output /tmp/memray-profile.bin \
        /usr/local/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

    echo "  Waiting for app to restart..."
    WAITED=0
    while [ $WAITED -lt $MAX_WAIT ]; do
        HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/health 2>/dev/null || echo "000")
        if [ "$HTTP_CODE" = "200" ]; then
            echo "  App ready under memray"
            break
        fi
        sleep 5
        WAITED=$((WAITED + 5))
    done
    if [ $WAITED -ge $MAX_WAIT ]; then
        echo "ERROR: App failed to start under memray"
        exit 1
    fi
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

# --- Warm-up: upload 3 archives to load components ---
echo "=== Warm-up (uploading 3 archives to load insights-core components) ==="
"$PYTHON" "$SCRIPT_DIR/send_archives.py" \
    --duration 999 \
    --bad-ratio "$BAD_RATIO" \
    --parallel 1 \
    $( [ -n "$NO_MOLODEC" ] && echo "--no-molodec" ) \
    $( [ -n "$MAX_EXCEPTIONS_ARCHIVE" ] && echo "--max-exceptions-archive $MAX_EXCEPTIONS_ARCHIVE" ) \
    $( [ -n "$UPLOAD_URL" ] && echo "--url $UPLOAD_URL" ) &
WARMUP_PID=$!

# Wait for 3 archives to be processed (watch the logs)
WARMUP_COUNT=0
WARMUP_TIMEOUT=120
WARMUP_WAITED=0
while [ "$WARMUP_COUNT" -lt 3 ] && [ "$WARMUP_WAITED" -lt "$WARMUP_TIMEOUT" ]; do
    sleep 2
    WARMUP_WAITED=$((WARMUP_WAITED + 2))
    WARMUP_COUNT=$(podman logs insights-app 2>&1 | grep -c "Starting archive processing" || echo 0)
    echo "  Warm-up: ${WARMUP_COUNT}/3 archives processed..."
done

kill "$WARMUP_PID" 2>/dev/null || true
wait "$WARMUP_PID" 2>/dev/null || true

if [ "$WARMUP_COUNT" -ge 3 ]; then
    echo "  Warm-up complete — components loaded"
else
    echo "  WARNING: only ${WARMUP_COUNT}/3 warm-up archives processed (timeout)"
fi

# Capture baseline memory after warm-up
BASELINE_MEM=$(podman stats --no-stream --format "{{.MemUsage}}" insights-app 2>/dev/null | awk '{print $1}' | sed 's/[A-Za-z]*//g')
echo "  Baseline memory: ${BASELINE_MEM} MiB"
BASELINE_PROCESSED=$(podman logs insights-app 2>&1 | grep -c "Starting archive processing" || echo 0)
echo ""

# --- Start monitoring (after warm-up, so baseline is clean) ---
echo "=== Starting monitoring ==="
bash "$SCRIPT_DIR/monitor.sh" "$TOTAL_MONITOR_MIN" "$OUTPUT_DIR" &
MONITOR_PID=$!
echo "  Monitor PID: $MONITOR_PID"

sleep 3

# --- Start load generation ---
echo ""
echo "=== Starting load generation ==="

echo "  Duration: ${DURATION_MIN} min"
echo "  Bad ratio: ${BAD_RATIO}"
echo "  Workers:  ${PARALLEL:-3}"
if [ -n "$BURST" ]; then
    echo "  Mode:     burst (10min send + 1min break)"
else
    echo "  Mode:     continuous"
fi
if [ -n "$MAX_EXCEPTIONS_ARCHIVE" ]; then
    echo "  Archives: max-exceptions ($MAX_EXCEPTIONS_ARCHIVE)"
elif [ -n "$NO_MOLODEC" ]; then
    echo "  Archives: self-contained"
else
    echo "  Archives: molodec (realistic OCP)"
fi
echo ""

SEND_ARGS=(
    --duration "$DURATION_MIN"
    --bad-ratio "$BAD_RATIO"
)
[ -n "$PARALLEL" ]       && SEND_ARGS+=(--parallel "$PARALLEL")
[ -n "$ARCHIVES_COUNT" ] && [ "$ARCHIVES_COUNT" -ne 0 ] && SEND_ARGS+=(--archives-count "$ARCHIVES_COUNT")
[ -n "$DELAY" ]          && SEND_ARGS+=(--delay "$DELAY")
[ -n "$BURST" ]      && SEND_ARGS+=(--burst)
[ -n "$NO_MOLODEC" ]            && SEND_ARGS+=(--no-molodec)
[ -n "$MAX_EXCEPTIONS_ARCHIVE" ] && SEND_ARGS+=(--max-exceptions-archive "$MAX_EXCEPTIONS_ARCHIVE")
[ -n "$UPLOAD_URL" ]            && SEND_ARGS+=(--url "$UPLOAD_URL")

"$PYTHON" "$SCRIPT_DIR/send_archives.py" "${SEND_ARGS[@]}" &
SEND_PID=$!
echo "  Load generator PID: $SEND_PID"

(
    while true; do
        sleep 30
        COUNT=$(podman logs insights-app 2>&1 | grep -c "Starting archive processing" 2>/dev/null || echo 0)
        DELTA=$((COUNT - BASELINE_PROCESSED))
        [ "$DELTA" -lt 0 ] && DELTA=0
        echo "  [$(date +%H:%M:%S)] App processed: $DELTA archives"
    done
) &
PROGRESS_PID=$!

echo ""
echo "=== Running ==="
echo "  Monitor:  $MONITOR_PID"
echo "  Load gen: $SEND_PID"
echo "  Duration: ${DURATION_MIN} min load + ${COOLDOWN_MIN} min cool-down"
echo "  Press Ctrl+C to stop early"
echo ""

# Wait for load generator to finish
wait "$SEND_PID" 2>/dev/null || true
SEND_PID=""

# Kill progress reporter; switch to inline output
kill "$PROGRESS_PID" 2>/dev/null || true
wait "$PROGRESS_PID" 2>/dev/null || true
PROGRESS_PID=""
FINAL_COUNT=$(podman logs insights-app 2>&1 | grep -c "Starting archive processing" 2>/dev/null || echo 0)
FINAL_COUNT=$((FINAL_COUNT - BASELINE_PROCESSED))
[ "$FINAL_COUNT" -lt 0 ] && FINAL_COUNT=0
echo "  Archives processed by app at load stop: $FINAL_COUNT"
echo ""

# Cooldown timer starts now; drain tracking runs inside the window.
# --wait extends beyond the timer if processing isn't stable yet.
COOLDOWN_END=$((SECONDS + COOLDOWN_MIN * 60))
LAST_CD=-1
STABLE_CD=0
DRAINED=0

if [ "$COOLDOWN_MIN" -gt 0 ]; then
    echo "=== Cool-down (${COOLDOWN_MIN} min, no archives sent — watching memory + processing) ==="
elif [ -n "$WAIT_FOR_PROCESSED" ]; then
    echo "=== Waiting for app to finish processing all archives ==="
fi

while [ "$SECONDS" -lt "$COOLDOWN_END" ] || { [ -n "$WAIT_FOR_PROCESSED" ] && [ "$DRAINED" -eq 0 ]; }; do
    sleep 10
    COUNT=$(podman logs insights-app 2>&1 | grep -c "Starting archive processing" 2>/dev/null || echo 0)
    DELTA=$((COUNT - BASELINE_PROCESSED))
    [ "$DELTA" -lt 0 ] && DELTA=0

    if [ "$DELTA" -eq "$LAST_CD" ]; then
        STABLE_CD=$((STABLE_CD + 10))
        if [ "$STABLE_CD" -ge 30 ] && [ "$DRAINED" -eq 0 ]; then
            DRAINED=1
            echo "  [$(date +%H:%M:%S)] App processed: $DELTA archives — processing complete"
        fi
    else
        STABLE_CD=0
        LAST_CD="$DELTA"
        DRAINED=0
    fi

    REMAINING=0
    [ "$SECONDS" -lt "$COOLDOWN_END" ] && REMAINING=$(( (COOLDOWN_END - SECONDS + 59) / 60 ))

    if [ "$DRAINED" -eq 0 ]; then
        echo "  [$(date +%H:%M:%S)] App processed: $DELTA archives (still processing, ${REMAINING}min remaining)"
    elif [ "$REMAINING" -gt 0 ]; then
        echo "  [$(date +%H:%M:%S)] App processed: $DELTA archives (${REMAINING}min remaining)"
    fi
done

# Kill monitor explicitly (started with a buffer duration)
if [ -n "$MONITOR_PID" ] && kill -0 "$MONITOR_PID" 2>/dev/null; then
    kill "$MONITOR_PID" 2>/dev/null || true
    wait "$MONITOR_PID" 2>/dev/null || true
fi
MONITOR_PID=""

echo ""
echo "Done."
