#!/bin/bash
#
# monitor.sh — Collect CPU/memory stats for insights-on-prem containers.
#
# Captures podman stats, /proc/1/status (VmRSS, VmSize), and disk usage
# at regular intervals. Outputs CSV files for analysis and a summary.
#
# Usage:
#   ./monitor.sh [duration_minutes] [output_dir]
#   ./monitor.sh 60                          # 60 min, auto-named output dir
#   ./monitor.sh 120 my_run_20260716         # 120 min, custom output dir

set -euo pipefail

CONTAINERS=("insights-app" "insights-postgres")
DURATION_MIN="${1:-60}"
OUTPUT_DIR="${2:-monitoring_$(date +%Y%m%d_%H%M%S)}"
SAMPLE_INTERVAL=10

mkdir -p "$OUTPUT_DIR"

echo "=== insights-on-prem Memory Monitor ==="
echo "Containers:     ${CONTAINERS[*]}"
echo "Duration:       ${DURATION_MIN} minutes"
echo "Sample interval: ${SAMPLE_INTERVAL}s"
echo "Output:         $OUTPUT_DIR/"
echo ""

# Initialize CSV files
for container in "${CONTAINERS[@]}"; do
    echo "timestamp,elapsed_min,cpu_perc,mem_usage_mb,mem_limit_mb,mem_perc" \
        > "$OUTPUT_DIR/${container}_podman_stats.csv"

    echo "timestamp,elapsed_min,vm_size_kb,vm_rss_kb,vm_data_kb,vm_stk_kb" \
        > "$OUTPUT_DIR/${container}_process_memory.csv"

    echo "timestamp,elapsed_min,disk_mb" \
        > "$OUTPUT_DIR/${container}_disk_usage.csv"
done

# Disk paths to measure per container (no associative array — bash 3.2 compat)
disk_paths_for() {
    case "$1" in
        insights-app)      echo "/tmp/insights-uploads /app" ;;
        insights-postgres) echo "/var/lib/postgresql/data" ;;
        *)                 echo "" ;;
    esac
}

START_TIME=$(date +%s)
ITERATION=0

echo "Monitoring started at $(date)"
echo "Press Ctrl+C to stop"
echo ""

while true; do
    CURRENT_TIME=$(date +%s)
    ELAPSED=$((CURRENT_TIME - START_TIME))
    ELAPSED_MIN=$((ELAPSED / 60))

    if [ "$ELAPSED_MIN" -ge "$DURATION_MIN" ]; then
        break
    fi

    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

    for container in "${CONTAINERS[@]}"; do
        if ! podman ps --format '{{.Names}}' | grep -q "^${container}$"; then
            if [ $((ITERATION % 3)) -eq 0 ]; then
                echo "  [SKIP] $container — not running"
            fi
            continue
        fi

        # Podman stats
        STATS=$(podman stats --no-stream --format \
            "{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}}" "$container" 2>/dev/null) || continue

        CPU_PERC=$(echo "$STATS" | cut -d',' -f1 | sed 's/%//')
        # Convert memory values to MiB regardless of unit
        to_mib() {
            local raw="$1"
            local val=$(echo "$raw" | sed 's/[A-Za-z]*//g')
            if echo "$raw" | grep -qi 'GB'; then
                echo "$val" | awk '{printf "%.1f", $1 * 1024}'
            elif echo "$raw" | grep -qi 'kB'; then
                echo "$val" | awk '{printf "%.1f", $1 / 1024}'
            else
                echo "$val"
            fi
        }
        MEM_USAGE_RAW=$(echo "$STATS" | cut -d',' -f2 | awk '{print $1}')
        MEM_LIMIT_RAW=$(echo "$STATS" | cut -d',' -f2 | awk '{print $3}')
        MEM_USAGE=$(to_mib "$MEM_USAGE_RAW")
        MEM_LIMIT=$(to_mib "$MEM_LIMIT_RAW")
        MEM_PERC=$(echo "$STATS" | cut -d',' -f3 | sed 's/%//')

        echo "${TIMESTAMP},${ELAPSED_MIN},${CPU_PERC},${MEM_USAGE},${MEM_LIMIT},${MEM_PERC}" \
            >> "$OUTPUT_DIR/${container}_podman_stats.csv"

        # /proc/1/status inside container
        PROC_MEM=$(podman exec "$container" sh -c '
            grep -E "VmSize|VmRSS|VmData|VmStk" /proc/1/status 2>/dev/null \
                | awk "{print \$2}" | tr "\n" ","
        ' 2>/dev/null) || true

        if [ -n "$PROC_MEM" ]; then
            # Strip trailing comma
            PROC_MEM="${PROC_MEM%,}"
            echo "${TIMESTAMP},${ELAPSED_MIN},${PROC_MEM}" \
                >> "$OUTPUT_DIR/${container}_process_memory.csv"
        fi

        # Disk usage inside container
        DISK_MB=""
        PATHS=$(disk_paths_for "$container")
        if [ -n "$PATHS" ]; then
            DISK_MB=$(podman exec "$container" sh -c "du -sm $PATHS 2>/dev/null | awk '{s+=\$1} END{print s+0}'" 2>/dev/null) || true
        fi
        if [ -n "$DISK_MB" ]; then
            echo "${TIMESTAMP},${ELAPSED_MIN},${DISK_MB}" \
                >> "$OUTPUT_DIR/${container}_disk_usage.csv"
        fi

        # Status line every minute (~6 iterations at 10s interval)
        if [ $((ITERATION % 3)) -eq 0 ]; then
            DISK_DISPLAY="${DISK_MB:-?}"
            printf "  [%3d min] %-20s mem=%s MiB  cpu=%s%%  disk=%s MB\n" \
                "$ELAPSED_MIN" "$container" "$MEM_USAGE" "$CPU_PERC" "$DISK_DISPLAY"
        fi
    done

    ITERATION=$((ITERATION + 1))
    sleep "$SAMPLE_INTERVAL"
done

echo ""
echo "Monitoring completed at $(date)"

# Generate summary
{
    echo "# Monitoring Summary"
    echo "Start: $(date -r "$START_TIME" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -d "@$START_TIME" '+%Y-%m-%d %H:%M:%S')"
    echo "End:   $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Duration: ${ELAPSED_MIN} minutes"
    echo ""

    for container in "${CONTAINERS[@]}"; do
        CSV="$OUTPUT_DIR/${container}_podman_stats.csv"
        PROC_CSV="$OUTPUT_DIR/${container}_process_memory.csv"
        [ -f "$CSV" ] || continue

        echo "## $container"

        # Podman stats memory
        awk -F',' 'NR>1 && $4+0>0 {
            sum+=$4; n++;
            if(n==1||$4+0>max) max=$4+0;
            if(n==1||$4+0<min) min=$4+0;
            if(n==1) { first=$4+0; first_t=$2 }
            last=$4+0; last_t=$2
        } END {
            if(n>0) {
                printf "  Container mem: start=%.1f MB  end=%.1f MB  min=%.1f MB  max=%.1f MB  delta=%+.1f MB\n", first, last, min, max, last-first
                hours = (last_t - first_t) / 60
                if(hours > 0) printf "  Growth rate:   %+.2f MB/hr\n", (last-first)/hours
            }
        }' "$CSV"

        # VmRSS from /proc
        if [ -f "$PROC_CSV" ] && [ "$(wc -l < "$PROC_CSV")" -gt 1 ]; then
            awk -F',' 'NR>1 && $4+0>0 {
                n++;
                if(n==1) first=$4+0;
                last=$4+0;
            } END {
                if(n>0) printf "  VmRSS:         start=%.0f KB  end=%.0f KB  delta=%+.0f KB (%+.1f MB)\n", first, last, last-first, (last-first)/1024
            }' "$PROC_CSV"
        fi

        # Disk usage
        DISK_CSV="$OUTPUT_DIR/${container}_disk_usage.csv"
        if [ -f "$DISK_CSV" ] && [ "$(wc -l < "$DISK_CSV")" -gt 1 ]; then
            awk -F',' 'NR>1 && $3+0>=0 {
                n++;
                if(n==1) first=$3+0;
                last=$3+0;
            } END {
                if(n>0) printf "  Disk:          start=%d MB  end=%d MB  delta=%+d MB\n", first, last, last-first
            }' "$DISK_CSV"
        fi

        echo ""
    done
} > "$OUTPUT_DIR/SUMMARY.txt"

cat "$OUTPUT_DIR/SUMMARY.txt"
echo ""
echo "Data saved to: $OUTPUT_DIR/"
