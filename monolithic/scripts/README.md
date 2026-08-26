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
./scripts/reproduce_leak.sh 10 0.3 --memray  # 10 min with memray profiling
```

**Options:**

| Flag             | Default  | Description                                          |
|------------------|----------|------------------------------------------------------|
| `--no-molodec`   | off      | Use self-contained archives instead of molodec        |
| `--parallel N`   | `3`      | Number of parallel upload workers                     |
| `--delay N`      | `0`      | Seconds between uploads per worker                    |
| `--burst`        | off      | Burst mode: 10 min send + 1 min break cycles          |
| `--cooldown N`   | `5`      | Minutes of idle monitoring after load stops            |
| `--max-exceptions-archive` | off | Use `max_exceptions_archive.tar` for uploads (max exceptions per archive) |
| `--memray`       | off      | Profile the app with memray (see below)                |
| `--keep`         | off      | Keep containers running after the test finishes        |
| `--url URL`      | `http://localhost:8000/api/ingress/v1/upload` | Upload endpoint |

Press `Ctrl+C` to stop early — it still prints the summary and stops containers.

To watch live app logs in another terminal while the reproducer runs:

```bash
podman logs -f insights-app
```

### `setup_venv.sh`

Creates a Python venv at `scripts/venv/` with molodec, insights-core,
ccx-rules-ocp, and pyyaml installed from the internal Red Hat PyPI using `uv`.
Called automatically by `reproduce_leak.sh` if the venv doesn't exist yet.

```bash
./scripts/setup_venv.sh        # create venv
rm -rf scripts/venv             # to recreate from scratch
```

If the venv already exists and you need to add the process_archive.py deps:

```bash
source scripts/venv/bin/activate
UV_NATIVE_TLS=1 uv pip install \
    --index-url https://nexus.corp.redhat.com/repository/obsint-pypi/simple \
    --extra-index-url https://pypi.org/simple \
    insights-core ccx-rules-ocp pyyaml
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
| `--max-exceptions-archive PATH` | off | Use a static archive file with max exceptions |

**Archive types:**

- **Valid** (default) — minimal OCP archive with correct JSON. Exercises the
  normal `dr.run_components()` code path.
- **Bad** — corrupted JSON at valid file paths. Triggers parsing exceptions
  in insights-core components, exercising `broker.add_exception()` — the code
  path where the traceback circular reference leak occurs.
- **Molodec** (`--use-molodec`) — realistic OCP archives with rule hits.
  Already installed in the venv by `setup_venv.sh`.
- **Max-exceptions** (`--max-exceptions-archive`) — a static archive designed
  to trigger the maximum number of exceptions per upload. Each upload gets a
  random cluster ID so the app treats them as distinct clusters.

### `process_archive.py`

Runs insights-core archive processing directly against a local archive file —
no HTTP server, no database. Useful for isolating the processing pipeline from
infrastructure, debugging rule-hit output, and measuring per-archive memory
growth in a tight loop.

```bash
# Single run — prints rule hits and RSS to stderr, raw JSON to stdout
python scripts/process_archive.py scripts/test_insights_archive-molodec.tgz

# Memory loop — process the same archive 20 times, watch RSS grow (or not)
python scripts/process_archive.py scripts/test_insights_archive-molodec.tgz --count 20

# Verbose — include insights-core DEBUG logs
python scripts/process_archive.py scripts/test_insights_archive-molodec.tgz --count 5 -v

# Save raw JSON output to a file for inspection
python scripts/process_archive.py scripts/test_insights_archive-molodec.tgz --output /tmp/results.json

# Custom config
python scripts/process_archive.py my.tar.gz --config /path/to/config.yml
```

**Options:**

| Flag          | Default              | Description                                         |
|---------------|----------------------|-----------------------------------------------------|
| `archive`     | (required)           | Path to `.tar` / `.tar.gz` / `.tgz` archive         |
| `--config`    | `monolithic/config.yml` | Path to config.yml                               |
| `--count N`   | `1`                  | Process the archive N times (memory leak testing)   |
| `--output FILE` | stdout             | Write raw JSON results to FILE (last iteration only) |
| `-v`          | off                  | Set log level to DEBUG                              |

**Per-iteration stderr output:**

```
RSS at start: 310.2 MB
[1/20]  cluster=abc-123  rules=7  rss_before=310.2 MB  rss_after=316.8 MB  delta=+6.6 MB  total_growth=+6.6 MB  fds=42 (delta=0)
[2/20]  cluster=abc-123  rules=7  rss_before=316.8 MB  rss_after=317.4 MB  delta=+0.6 MB  total_growth=+7.2 MB  fds=42 (delta=0)
```

After each iteration the script runs `gc.collect()` + `malloc_trim(0)` —
the same post-processing cleanup as the real service — so memory numbers are
directly comparable to production behavior.

Requires the `scripts/venv` to have `insights-core`, `ccx-rules-ocp`, and
`pyyaml` installed (see `setup_venv.sh` above). Run from the `monolithic/`
directory or anywhere — it auto-adjusts the Python path.

### `test_insights_archive-molodec.tgz`

A realistic OCP archive generated with molodec, kept in the scripts directory
as a ready-to-use test fixture for `process_archive.py`. Generate a fresh one
with:

```bash
source scripts/venv/bin/activate
molodec archive generate scripts/test_insights_archive-molodec.tgz
```

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

## Memray Profiling

When `--memray` is passed to `reproduce_leak.sh`, the script:

1. Starts the compose stack normally and waits for it to be ready
2. Installs memray inside the running container (`pip install memray`)
3. Restarts the app under `memray run` (wrapping uvicorn, without `--reload`)
4. Runs the normal warm-up, monitoring, and load generation phases
5. On exit, copies the memray profile out and generates a flamegraph

After the run, the output directory will contain:

```
monitoring_20260729_143000/
├── ...                          # normal monitoring CSVs
├── memray-profile.bin           # raw memray profile
└── memray-flamegraph.html       # open in browser
```

To generate the flamegraph, memray must also be installed locally
(`pip install memray`). If it's not available, the `.bin` file is still
extracted and can be processed later:

```bash
python3 -m memray flamegraph monitoring_*/memray-profile.bin -o flamegraph.html
python3 -m memray stats monitoring_*/memray-profile.bin
python3 -m memray tree monitoring_*/memray-profile.bin
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
