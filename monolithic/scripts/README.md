# Memory Leak Reproducer Scripts

Tools for reproducing and measuring memory leaks in the insights-on-prem
monolithic service. The leak originates in `insights-core`'s
`dr.run_components()` — the broker accumulates exception tracebacks with
circular references that the garbage collector cannot free.

## Prerequisites

- `podman` with `podman-compose`
- [`uv`](https://docs.astral.sh/uv/) for Python venv setup
- The monolithic `docker-compose.yml` built and ready

## Quick Start

```bash
cd monolithic
./scripts/reproduce_leak.sh
```

That single command does everything: sets up a Python venv (if needed),
tears down any previous run, starts a clean compose stack, uploads bad
archives at max speed for 30 minutes, prints CPU/memory/disk every 30s,
and finishes with a leak evaluation report that excludes the warm-up period.

## Scripts

### `reproduce_leak.sh`

One-command orchestrator. Cleans up previous containers/volumes, starts a
fresh compose stack, waits for readiness, auto-detects whether the
insights-core traceback fix is present, then runs monitoring and load
generation in parallel. Prints CPU/mem/disk every minute during the run.
On exit, prints a leak evaluation that skips the first 5 minutes of
warm-up (component loading) and evaluates steady-state memory growth.
Fully non-interactive.

```bash
./scripts/reproduce_leak.sh                # 30 min, 100% bad archives, max speed
./scripts/reproduce_leak.sh 120            # 2 hours
./scripts/reproduce_leak.sh 120 0.5        # 2 hours, 50% bad archives
```

Press `Ctrl+C` to stop early — it still prints the summary and stops containers.

To watch live app logs in another terminal while the reproducer runs:

```bash
podman logs -f insights-app
```

### `setup_venv.sh`

Creates a Python venv at `scripts/venv/` with molodec installed from the
internal Red Hat PyPI using `uv`. Called automatically by `reproduce_leak.sh`
if the venv doesn't exist yet.

```bash
./scripts/setup_venv.sh        # create venv
rm -rf scripts/venv             # to recreate from scratch
```

### `send_archives.py`

Load generator. Uploads archives to `POST /api/ingress/v1/upload` in a loop.

```bash
# Continuous mode (default) — 60 min, 2 uploads/sec, 30% bad archives
python3 scripts/send_archives.py

# Custom settings
python3 scripts/send_archives.py --duration 120 --delay 0.2 --bad-ratio 0.5

# Burst mode — 10 min sending + 1 min break, repeat
python3 scripts/send_archives.py --duration 60 --burst

# Use molodec for realistic OCP archives (requires separate install)
python3 scripts/send_archives.py --use-molodec --duration 60
```

**Options:**

| Flag             | Default  | Description                                          |
|------------------|----------|------------------------------------------------------|
| `--duration`     | `60`     | Duration in minutes                                  |
| `--delay`        | `0`      | Seconds between uploads per worker                   |
| `--parallel`     | `3`      | Number of parallel upload workers                    |
| `--bad-ratio`    | `0`      | Fraction of archives with corrupted JSON (0.0–1.0)   |
| `--url`          | `http://localhost:8000/api/ingress/v1/upload` | Upload endpoint |
| `--burst`        | off      | Burst mode: 10 min send + 1 min break cycles         |
| `--use-molodec`  | on       | Use molodec for archive generation (default)          |
| `--no-molodec`   | off      | Use self-contained archives instead of molodec        |

**Archive types:**

- **Valid** (default) — minimal OCP archive with correct JSON. Exercises the
  normal `dr.run_components()` code path.
- **Bad** — corrupted JSON at valid file paths. Triggers parsing exceptions
  in insights-core components, exercising `broker.add_exception()` — the code
  path where the traceback circular reference leak occurs.
- **Molodec** (`--use-molodec`) — realistic OCP archives with rule hits.
  Already installed in the venv by `setup_venv.sh`.

### `monitor.sh`

Collects CPU and memory stats every 10 seconds for `insights-app` and
`insights-postgres` containers. Prints a status line every minute and
outputs CSV files plus a summary report.

```bash
# Monitor for 60 minutes
bash scripts/monitor.sh 60

# Custom output directory
bash scripts/monitor.sh 120 my_run_label
```

**Data collected:**

| Source                        | Metrics                                       |
|-------------------------------|-----------------------------------------------|
| `podman stats`                | CPU %, memory usage (MiB), memory limit, mem % |
| `/proc/1/status` in container | VmSize, VmRSS, VmData, VmStk (KB)             |
| `du -sm` in container         | Disk usage (MB) of data directories            |

**Output files** (in the timestamped output directory):

```
monitoring_20260716_143000/
├── insights-app_podman_stats.csv
├── insights-app_process_memory.csv
├── insights-app_disk_usage.csv
├── insights-postgres_podman_stats.csv
├── insights-postgres_process_memory.csv
├── insights-postgres_disk_usage.csv
└── SUMMARY.txt
```

## Interpreting Results

The final report skips the first 5 minutes of warm-up (insights-core
loading ~900 MiB of components) and evaluates steady-state memory growth:

| Steady-state rate | Verdict                                    |
|-------------------|--------------------------------------------|
| < 1 MiB/hr        | `STABLE` — no leak detected               |
| 1–10 MiB/hr       | `POSSIBLE LEAK` — moderate growth          |
| > 10 MiB/hr       | `LEAK DETECTED` — significant growth       |

**Signs of the leak:**
- Steady-state RSS grows continuously with no plateau
- Memory does not drop during burst-mode breaks
- Growth rate scales with `--bad-ratio` (more exceptions = faster leak)

**Healthy behavior (fix applied):**
- RSS stabilizes after warm-up
- Memory drops during idle periods in burst mode
- Steady-state growth rate near zero regardless of bad-ratio

## Background

These scripts are adapted from the
[CCXDEV-15098 investigation](../../CCXDEV-15098-mem_leak_investig/) which
targeted the Kafka-based multi-container deployment. This version is
simplified for the monolithic FastAPI + Postgres stack.
