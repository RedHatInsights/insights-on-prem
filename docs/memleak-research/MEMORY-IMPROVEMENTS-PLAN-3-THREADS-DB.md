# SQLAlchemy & Background Threads — Deep Dive

**Date**: 2026-08-03
**Container**: insights-app (running 9+ hours, 6,234+ archives processed)
**Load test**: `reproduce_leak.sh 120 0 --cooldown 30 --use-molodec --parallel 10 --keep`

---

## Executive Summary

Investigation into whether SQLAlchemy connections and background threads contribute to the memory leak. **Conclusion: no connection leak exists.** All sessions are properly closed, overflow connections are correctly released, and "idle in transaction" is normal psycopg2 behavior — not a stuck state. The main actionable finding is a **monitoring bug** where `monitor.sh` tracks the wrong PID when `--reload` is used.

---

## Process Architecture

`uvicorn --reload` (set in `docker-compose.yml`) forks a child worker. PID 1 is NOT the real process:

| PID | Role | VmRSS | Threads |
|-----|------|-------|---------|
| 1 | uvicorn master (reload watcher) | 68 MB | 3 |
| 3 | multiprocessing resource_tracker | 40 MB | 2 |
| **6** | **actual FastAPI worker** | **1,108 MB** | **42** |

---

## Background Threads

### How they work

Each upload request schedules a background task via FastAPI's `BackgroundTasks`:

```
HTTP POST /api/ingress/v1/upload
  → upload_archive() accepts, returns 202
  → BackgroundTasks runs _process_in_background()
    → Starlette calls run_in_threadpool()
      → anyio.to_thread.run_sync()
        → anyio worker thread executes the task
```

### Thread pool behavior

- **Pool limit**: anyio default = **40 threads**
- **Current state**: 42 threads (40 pool + main + event loop)
- **All idle threads**: sleeping on `futex_do_wait`
- **Thread lifecycle**: created on demand during load, **never reaped**

The 40 threads were created during the first burst of concurrent archive uploads. anyio's thread pool does not reap idle threads — they persist for the lifetime of the process. This is by design (avoids thread creation overhead on subsequent requests).

### Memory cost

Each thread allocates a stack (default 8 MB on Linux):

```
40 threads × 8 MB stack = ~320 MB virtual memory
```

This shows up in `VmSize` (6.6 GB) but NOT in `VmRSS` — unused stack pages are not resident. The actual RSS cost of idle threads is negligible (~100 KB each for thread-local storage).

### Thread count during load vs idle

| State | Threads (PID 6) | Notes |
|-------|-----------------|-------|
| Startup (no load) | 2-3 | Main + event loop |
| During load (10 workers) | 42 | 40 pool threads created on demand |
| After load stops | 42 | Threads persist, all sleeping |

---

## Database Connections

### Pool configuration

From `database.py:init_db()`:

```python
engine = create_engine(
    database_url,
    pool_pre_ping=True,     # SELECT 1 on checkout to detect stale connections
    pool_size=10,            # permanent connections in pool
    max_overflow=20,         # temporary connections beyond pool_size
    pool_recycle=3600,       # replace connections older than 1 hour (on checkout)
)
```

**Max connections**: pool_size(10) + max_overflow(20) = **30**

### Session creation points (all have proper cleanup)

| Location | Creation | Cleanup | Used by |
|----------|----------|---------|---------|
| `upload_service.py:117` | `self.session_factory()` | `db.close()` in finally | Background archive processing |
| `main.py:114` | `session_factory()` | `db.close()` in finally | Periodic DB cleanup task |
| `database.py:37` | `session_factory()` | `session.close()` in finally | FastAPI route dependency |

**Zero session leaks**: 6,234 archives processed successfully with 0 failures. `db.close()` executes in every code path.

### Overflow connection lifecycle (verified)

Test with a separate engine (pool_size=2, max_overflow=3):

```
Burst: 5 connections checked out
  → checkedout=5, overflow=3

All returned:
  → checkedout=0, checkedin=2, overflow=0
  → PostgreSQL shows only 1 connection (pool_size minus one used for the check query)
  → 3 overflow connections properly CLOSED
```

**QueuePool correctly closes overflow connections on return.** The 20 overflow connections observed during load are actively checked out, not leaked.

### "idle in transaction" explained

This was the most suspicious-looking symptom: 30 connections showing "idle in transaction" in `pg_stat_activity`.

**Root cause**: psycopg2 with `autocommit=False` (the default) wraps every SQL statement in an implicit `BEGIN` transaction. Between SQL calls — while Python code runs insights-core processing or iterates through rule hits — PostgreSQL reports the connection as "idle in transaction".

Verified with a test engine:

```
Test D: Connection-level lifecycle
  After conn.execute(SELECT 1), before close:
    → PostgreSQL state: "idle in transaction"    ← BEGIN was sent implicitly
  After conn.close():
    → Pool sends rollback-on-return
    → PostgreSQL state: "idle"                   ← ROLLBACK cleared it
```

```
Test E: Raw psycopg2
  autocommit=False (default)
  After SELECT 1: state=active (in transaction)
  After COMMIT: state=active (new implicit transaction from the check query)
```

**"idle in transaction" is normal operational state**, not a leak. It means connections are checked out and between SQL statements within an active processing thread.

### Connection count during load vs idle

| State | Connections | "idle in transaction" | Notes |
|-------|-------------|----------------------|-------|
| During load (10 workers) | 30 | 27-30 | All actively in use |
| After load stops | 10 | 0 | Only pool_size remains, all "idle" |

### Pool recycle behavior

`pool_recycle=3600` only triggers **at checkout time**, not proactively. If no one checks out a connection, stale connections persist until the next checkout. This is expected SQLAlchemy behavior.

After terminating all 30 connections from PostgreSQL side:
- Connections re-created on demand as processing threads check out from pool
- `pool_pre_ping=True` detects dead connections and creates replacements

---

## Monitoring Bug: Wrong PID

### The problem

`monitor.sh:104` hardcodes `/proc/1/status` for VmRSS readings:

```bash
grep -E "VmSize|VmRSS|VmData|VmStk" /proc/1/status
```

This is correct when:
- `--memray` is used (memray wraps uvicorn, PID 1 = actual process)

This is **WRONG** when:
- `--reload` is in the uvicorn command (PID 1 = master, PID 6 = worker)

### Impact

The current `reproduce_leak.sh` run (without `--memray`) produces a `process_memory.csv` showing:

```csv
timestamp,elapsed_min,vm_size_kb,vm_rss_kb,vm_data_kb,vm_stk_kb
2026-08-03 11:17:39,144,394880,68644,179752,132    ← PID 1: uvicorn master
```

Actual worker (PID 6): VmRSS = **1,108,380 KB** (1.08 GB)

**All VmRSS data from non-memray runs is invalid.** The flat 68 MB line is the uvicorn reload master, not the worker.

### Fix

Replace the hardcoded PID 1 with the highest-RSS process in the container:

```bash
# Find the process with the largest RSS (the actual worker)
TARGET_PID=$(for p in /proc/[0-9]*/status; do
    pid=$(echo "$p" | cut -d/ -f3)
    rss=$(grep VmRSS "$p" 2>/dev/null | awk '{print $2}')
    [ -n "$rss" ] && echo "$pid $rss"
done | sort -k2 -n | tail -1 | awk '{print $1}')

grep -E "VmSize|VmRSS|VmData|VmStk" "/proc/$TARGET_PID/status"
```

---

## Recommendations

### No action needed

- **SQLAlchemy sessions**: properly managed, no leaks
- **Connection pool**: correctly sized for the workload, overflow works as expected
- **"idle in transaction"**: normal psycopg2 behavior, not a problem
- **Thread count**: 42 threads is expected, RSS cost is negligible

### Minor improvements

| Item | Current | Suggested | Why |
|------|---------|-----------|-----|
| `max_overflow` | 20 | 5-10 | 10 parallel workers rarely need 30 total connections. Reduces postgres memory by ~100 MB |
| `pool_timeout` | 30s | 10s | Fail fast if pool is exhausted instead of blocking threads |
| `--reload` in docker-compose | enabled | remove for production/testing | Eliminates the master/worker PID confusion; saves 68 MB from master process |

### Must fix

| Item | File | Impact |
|------|------|--------|
| **monitor.sh PID detection** | `scripts/monitor.sh:104` | Without this fix, all non-memray monitoring runs produce garbage VmRSS data |

---

## Threading Architecture — Full Map

### Process Model

Uvicorn runs a **single worker process** with **one asyncio event loop** on the main thread.
No `--workers` flag is set — not in `docker-compose.yml` (line 55) and not in `start_server()` (`main.py:449`).

```
Process (single uvicorn worker, mem_limit: 4g)
│
├── Main Thread (asyncio event loop)
│   ├── HTTP request handling (all async def endpoints)
│   ├── _cleanup_old_records loop  ← BLOCKS event loop (sync DB on async)
│   └── asyncio.gather (fans out Thanos queries to thread pool)
│
├── anyio Thread Pool (default cap: 40)
│   ├── [0..40] archive processing threads (_process_in_background)
│   │   each: extract tar → insights-core rules → DB save → gc.collect
│   ├── [0..100] Thanos query threads (httpx.get, queued if >40)
│   └── [0..1]  shutdown drain (task_tracker.wait_until_idle)
│
├── SQLAlchemy Connection Pool (10 + 20 overflow = 30 max)
│   └── shared by all threads
│
└── BackgroundTaskTracker
    └── Lock + Event for graceful shutdown coordination
```

### All endpoints are `async def`

Every endpoint runs directly on the event loop. None are sync `def`, so FastAPI never auto-offloads endpoint handlers to threads. The only things that run in the thread pool are explicitly offloaded via `BackgroundTasks` or `asyncio.to_thread()`.

### What runs on the event loop (main thread)

| Code | File:Line | Behavior |
|------|-----------|----------|
| `upload_archive()` | `main.py:176` | Accepts upload, saves file (async chunked), schedules background task, returns 202 |
| `get_cluster_report_v2()` | `main.py:226` | Sync SQLAlchemy query **on the event loop** (blocks briefly) |
| `upgrade_risks_prediction_batch()` | `main.py:273` | Fans out Thanos queries via `asyncio.gather` + `asyncio.to_thread` |
| `get_request_status()` | `main.py:340` | Sync SQLAlchemy query **on the event loop** |
| `get_request_report()` | `main.py:372` | Sync SQLAlchemy query + JSON parsing **on the event loop** |
| `_cleanup_old_records()` | `main.py:111` | **Sync DB deletes + commits directly on the event loop** |

### What runs in the thread pool

| Code | File:Line | How offloaded |
|------|-----------|---------------|
| `_process_in_background()` | `upload_service.py:113` | `BackgroundTasks.add_task()` → Starlette's `run_in_threadpool()` → `anyio.to_thread.run_sync()` |
| `thanos_service.query_cluster_metrics()` | `main.py:306` | `asyncio.to_thread()` |
| `task_tracker.wait_until_idle(270)` | `main.py:94` | `asyncio.to_thread()` (shutdown only) |

### Background archive processing — per-thread execution flow

```
_process_in_background()                         upload_service.py:113
  ├── task_tracker.start()                        # acquire lock, increment counter
  ├── session_factory()                           # new DB session (from pool of 10+20)
  ├── processor_service.process_archive()         processor_service.py:297
  │     ├── process_with_insights_core()          processor_service.py:128
  │     │     ├── extract()                       # untar to /tmp, up to 300s timeout
  │     │     ├── _validate_size()                # walk extracted dir, sum file sizes
  │     │     ├── get_cluster_id()                # read config/id file
  │     │     ├── initialize_broker()             # insights-core broker setup
  │     │     ├── dr.run_components()             # run all insights rules (CPU-heavy)
  │     │     ├── broker.cleanup()                # cleanup broker
  │     │     └── gc.collect()                    # 1st GC — inside `with extract` block
  │     └── save_results()                        processor_service.py:224
  │           ├── Report.upsert()
  │           ├── RuleHit.delete_for_cluster() + upsert per hit
  │           ├── RequestReport.create()
  │           └── db.commit()
  ├── db.close()
  ├── gc.collect()                                # 2nd GC — after DB close
  ├── os.remove(temp_file)                        # cleanup temp archive
  └── task_tracker.finish()                       # decrement counter, signal idle if 0
```

### Thread synchronization: BackgroundTaskTracker (`task_tracker.py`)

The only explicit synchronization primitive in the app:

- `threading.Lock` guards a counter of active background tasks
- `threading.Event` signals when all tasks are done (count hits 0)
- On shutdown (`main.py:94`): `await asyncio.to_thread(task_tracker.wait_until_idle, 270)` — blocks a thread pool slot for up to **270 seconds** waiting for processing to finish

### Thanos query fan-out (`main.py:304-328`)

```python
async def predict_for_cluster(cluster_id: str) -> ClusterPrediction:
    console_url, alerts, focs = await asyncio.to_thread(
        thanos_service.query_cluster_metrics, cluster_id
    )
    ...

predictions = await asyncio.gather(*[predict_for_cluster(c) for c in clusters])
```

Each cluster in a batch request spawns a thread for a blocking `httpx.get()` call. With `max_batch_size=100`, up to 100 `to_thread` calls compete with archive processing for the same 40-thread pool.

---

## Concurrency Observations & Risks

### 1. No processing concurrency limit

The only cap on concurrent archive processing is the 40-thread anyio pool. There is no semaphore, queue, or rate limiter. If 40 archives arrive simultaneously, all 40 process in parallel — each extracting archives + running insights-core + holding memory.

**Impact on memory**: Each concurrent archive holds extracted files + insights-core broker state + StringIO output + DB session. With 10 parallel workers sending archives, the peak memory footprint is ~10× the single-archive footprint.

### 2. insights-core runs in-process, no isolation

`dr.run_components()` at `processor_service.py:161-163` runs all insights rules inside the same Python process. If insights-core or any rule plugin leaks (C extensions, global caches, `dr.DELEGATES` accumulation, `dr.COMPONENTS` growth), `gc.collect()` won't reclaim it — and there's no process recycling to flush leaked memory.

### 3. Two `gc.collect()` calls per archive

- **1st** (`processor_service.py:171`): inside the `with extract` block, after `broker.cleanup()` and `del broker, ctx`. At this point the extraction context manager still holds references to the extracted directory.
- **2nd** (`upload_service.py:133`): after `db.close()`, before temp file cleanup. This should catch objects freed by closing the DB session.

### 4. Thanos queries can starve archive processing

A batch upgrade-risks-prediction request with 100 clusters queues 100 `asyncio.to_thread()` calls. These compete with `_process_in_background` tasks for the same 40-thread pool. Archive processing could stall waiting for thread availability, prolonging memory holding time for partially-processed archives.

### 5. Cleanup task blocks the event loop

`_cleanup_old_records` (`main.py:111-138`) runs synchronous SQLAlchemy operations directly in an `async def`:
- `model.delete_older_than(db, cutoff)` — sync DB query
- `db.commit()` — sync commit
- `db.rollback()` — sync rollback

None of these are offloaded via `asyncio.to_thread()`. During cleanup, **the event loop cannot serve any HTTP requests** — including health checks. If the tables are large and deletion is slow, this becomes a noticeable stall.

### 6. Route handlers do sync DB on the event loop

Several endpoints use sync SQLAlchemy via the `Depends(get_db)` dependency:
- `get_cluster_report_v2` (`main.py:226`)
- `get_request_status` (`main.py:340`)
- `get_request_report` (`main.py:372`)

These are `async def` endpoints calling sync `db.query()` methods. Each call briefly blocks the event loop. Under low load this is negligible, but under concurrent read traffic it serializes all queries onto the single event loop thread.
