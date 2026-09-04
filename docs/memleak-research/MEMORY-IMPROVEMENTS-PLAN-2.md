# Memory Leak Findings Evaluation — Live Verification Results

## Context

Two analysis reports were produced from a 90-minute memray profiling run (60 min load + 30 min cooldown) using `max_exceptions_archive.tar`. The goal is to determine which findings have **real, measurable impact** on memory savings by cross-referencing against the live running containers.

**Live container state**: running 9+ hours, VmRSS ~872 MB, container-level ~986 MB, 5,165+ archives processed.

**Live burst test**: Sending 50 `max_exceptions_archive.tar` caused **+44 MB permanent growth** (831→876 MB VmRSS, never recovered). That's ~880 KB/archive of permanent leak.

---

## Verdict by Finding (ranked by real impact)

### HIGHEST IMPACT: `broker.add_exception()` + `__traceback__` circular references
- **Report 2, Fix 1** — **Estimated savings: ~400 MB RSS**
- **CONFIRMED in live code**: `add_exception()` at `dr.py:918` does NOT clear `ex.__traceback__`. Zero occurrences of `__traceback__` in the file.
- This is the **root cause**. The exception's `__traceback__` holds a stack frame → local variables → broker → all parsed data (Entry trees, exceptions, tracebacks). CPython's refcount cannot break this cycle. The cyclic GC can, but under heavy load it doesn't run frequently enough.
- **Proof from live test**: 50 archives → +44 MB permanent growth, never recovered even after 3 minutes idle. This matches the 600 MB/hr growth rate from the profiling run.
- **Fix**: 1 line — `ex.__traceback__ = None` in `add_exception()`

### HIGH IMPACT: Clear broker state after processing
- **Report 1, Finding 3 / Report 2, Fix 2** — **Estimated savings: ~350 MB RSS (overlaps with above)**
- **CONFIRMED**: `processor_service.py` does zero cleanup after `dr.run_components()`. No `broker.exceptions.clear()`, no `broker.instances.clear()`, no `gc.collect()`. The broker relies entirely on Python GC to detect and free circular references.
- Even with `__traceback__` cleared, explicitly clearing broker state is good defense-in-depth.
- **Fix**: 5 lines in `processor_service.py` after the `with self.Formatter(...)` block

### MEDIUM IMPACT: `gc.collect()` after each archive
- **Report 2, Fix 3** — **Estimated savings: ~120 MB RSS**
- **CONFIRMED**: `_process_in_background()` in `upload_service.py` has no `gc.collect()` call. The cooldown monitoring data shows a sawtooth pattern (~14 MB oscillations) suggesting periodic GC does collect some cycles, but not aggressively enough.
- **Fix**: 1 line in `upload_service.py` finally block

### MEDIUM IMPACT: Limit `traceback.format_exc()` depth
- **Report 2, Fix 4** — **Estimated savings: ~80 MB RSS (fragmentation reduction)**
- **CONFIRMED**: All 5 `format_exc()` calls in `dr.py` (lines 1095, 1105, 1110) use unlimited depth. Memray shows `ast.parse` consumed 17 GB cumulative — Python 3.12 generates caret anchors for every traceback frame, creating massive allocation churn. Most is freed, but it fragments the heap.
- The max_exceptions_archive triggers hundreds of exceptions per archive, amplifying this.
- **Fix**: Change to `traceback.format_exc(limit=3)` at 4 call sites

### LOW IMPACT / ALREADY FIXED: ProductLifecycle JSON
- **Report 1, Finding 2** — **ALREADY FIXED in current code**
- `_load_packaged_data()` has `@functools.cache` decorator. The 29 MB "leak" reported is a one-time allocation, not per-request. The report was based on the first invocation.
- **No action needed**

### LOW IMPACT (one-time): Module re-import
- **Report 1, Finding 1** — **78 MB listed, but it's one-time startup cost**
- Once `ccx_ocp_core` and `ccx_rules_ocp` modules are loaded into `sys.modules`, they stay there. This is not per-archive growth. The report confused initial module loading with a recurring leak.
- The 78 MB is real memory but does not accumulate. After 5,000+ archives, the modules are still the same 78 MB.
- **No action needed** (unless you want to reduce baseline memory)

### LOW IMPACT (one-time): libmagic / spec_factory
- **Report 1, Finding 6** — **6.5 MB libmagic is one-time, spec_factory covered by broker cleanup**
- libmagic loads its database once (6.5 MB fixed cost). spec_factory ContentProviders are held by the broker and freed when broker is cleaned up.
- **Covered by broker cleanup fix above**

### LOW IMPACT: SQLAlchemy ORM growth
- **Report 1, Finding 5** — **18.8 MB listed**
- `db.close()` IS called in `upload_service.py`'s finally block. The reported 18.8 MB is likely from compiled statement caching and identity map, which grows slowly with unique query patterns. The app uses a small set of fixed queries.
- **Low priority** — not the primary growth driver

---

## Summary: What to implement (in priority order)

| Priority | Fix | Where | Lines | Est. Savings |
|----------|-----|-------|-------|-------------|
| **1** | `ex.__traceback__ = None` | `insights/core/dr.py:920` | 1 | ~400 MB |
| **2** | Clear `broker.exceptions/tracebacks/instances` | `app/services/processor_service.py:160` | 5 | ~350 MB (overlaps #1) |
| **3** | `gc.collect()` in finally block | `app/services/upload_service.py:_process_in_background` | 1 | ~120 MB |
| **4** | `traceback.format_exc(limit=3)` | `insights/core/dr.py` (4 sites) | 4 | ~80 MB frag. |

Fixes 1+2+3 together: **~550 MB savings** (from ~1.2 GB down to ~650 MB after 60 min load).

**Findings that DON'T matter**:
- ProductLifecycle: already cached with `@functools.cache`
- Module imports: one-time cost, not accumulating
- libmagic: one-time 6.5 MB, not growing
- SQLAlchemy: minor, sessions are properly closed

---

## SQLAlchemy & Background Threads — Deep Dive

### Threads (PID 6 = actual worker, NOT PID 1)

`uvicorn --reload` forks a child worker. The real process is **PID 6**, not PID 1:

| PID | Role | VmRSS | Threads |
|-----|------|-------|---------|
| 1 | uvicorn master (reload watcher) | 68 MB | 3 |
| 3 | multiprocessing resource_tracker | 40 MB | 2 |
| **6** | **actual FastAPI worker** | **1,108 MB** | **42** |

Thread breakdown in PID 6:
- **40 threads**: anyio/Starlette thread pool (default limit = 40)
- **2 threads**: main event loop + asyncio internals
- All 40 pool threads created during load and **never reaped** — anyio keeps threads alive for reuse
- All idle threads sleep on `futex_do_wait`, consuming ~8 MB stack each → **~320 MB permanent thread stack overhead**

Background task flow: Each upload → `BackgroundTasks.add_task(_process_in_background)` → `run_in_threadpool()` → anyio worker thread.

### DB Connections

Pool config in `database.py`:
```
pool_size=10, max_overflow=20, pool_pre_ping=True, pool_recycle=3600
```

**Connection lifecycle is correct** — verified:
- All 3 session creation points (`database.py:37`, `main.py:114`, `upload_service.py:117`) have matching `db.close()` in finally blocks
- QueuePool **does** close overflow connections on return (test confirmed: 5 checked out → all returned → only pool_size=2 kept)
- `reset_on_return=rollback` properly sends ROLLBACK when connections are returned
- 0 failed background tasks out of 6,234+ — `db.close()` always executes

**"idle in transaction" explained**: psycopg2 with `autocommit=False` (default) wraps every statement in an implicit `BEGIN`. Between SQL calls — while Python code runs `process_with_insights_core()` or between `RuleHit.upsert()` calls — PostgreSQL shows the connection as "idle in transaction". This is **normal operational state**, not a leak. Verified with a test engine: after `conn.close()`, pool sends `rollback-on-return` and PostgreSQL shows "idle".

Connection count during load (10 parallel workers):
- 30 connections = pool_size(10) + max_overflow(20) — all actively checked out
- After processing completes and sessions close: overflow connections are released, count drops to pool_size(10)

### Monitoring Bug: Wrong PID

`monitor.sh:104` hardcodes `/proc/1/status` for VmRSS readings. This is correct for `--memray` runs (PID 1 = actual process) but **wrong for non-memray runs** where `--reload` creates a child worker at PID 6.

**Impact**: The current run's `insights-app_process_memory.csv` shows a flat 68 MB (uvicorn master) instead of the actual 1,108 MB worker. All VmRSS growth data from non-memray runs is invalid.

**Fix for monitor.sh**: Find the largest-RSS child process instead of hardcoding PID 1:
```bash
# Instead of: /proc/1/status
# Use:
TARGET_PID=$(ls -d /proc/[0-9]*/status 2>/dev/null \
  | xargs -I{} sh -c 'grep VmRSS {} 2>/dev/null | awk -v p=$(echo {} | cut -d/ -f3) "{print p, \$2}"' \
  | sort -k2 -n | tail -1 | awk '{print $1}')
```

### Recommendations for pool tuning

No connection leak exists. The current config is reasonable for the workload. Minor improvements:

| Setting | Current | Suggested | Why |
|---------|---------|-----------|-----|
| `max_overflow` | 20 | 5 | 10 parallel workers don't need 30 total connections. Excess overflow causes unnecessary postgres memory |
| `pool_timeout` | 30 | 10 | Fail fast instead of holding threads waiting for a connection |

---

## Verification

After applying fixes, re-run:
```bash
./reproduce_leak.sh 60 0 --max-exceptions-archive --cooldown 30 --memray
```
Success criteria: VmRSS growth < 50 MB/hr (vs current ~288 MB/hr).
