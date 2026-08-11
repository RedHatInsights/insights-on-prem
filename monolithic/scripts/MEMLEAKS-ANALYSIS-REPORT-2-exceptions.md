# Memory Leak Analysis Report #2 — Exception-Driven Leaks

**Date**: 2026-07-31
**Profile**: `monitoring_20260731_055945-memray_profiler_60min_30cooldown/`
**Duration**: 60 min load + 30 min cooldown
**Archive type**: max-exceptions (max_exceptions_archive.tar)

## Test Conditions

- App: insights-on-prem monolithic (FastAPI + uvicorn, single worker)
- Container memory limit: 4 GB
- insights-core traceback fix: **MISSING** (reproducing the leak)
- Profiler: memray 1.19.3 wrapping uvicorn

## Key Metrics

| Metric | Value |
|--------|-------|
| Container RSS start | 353 MB |
| Container RSS end | 1,241 MB |
| RSS growth | +888 MB over 90 min |
| Growth rate | ~600 MB/hr |
| Peak memory (memray) | 458.6 MB |
| Total allocated (memray) | 95.5 GB |
| Total allocation calls | 35.5M |
| **Leaked (never freed)** | **239.2 MB** |

The gap between leaked memory (239 MB) and RSS growth (888 MB) is caused
by Python memory fragmentation — freed objects leave holes in the heap
that the OS allocator cannot return.

## Cooldown Behavior

Memory during cooldown (no archives sent):

| Time | RSS |
|------|-----|
| 60 min (load stops) | 1,097 MB |
| 65 min | 1,143 MB |
| 82 min | 1,243 MB |
| 87 min (end) | 1,288 MB |

Memory **never drops** — it is retained (and slightly grows as background
tasks finish). Confirms a true leak, not working-set pressure.

## Memray Stats — Top Allocators (by total bytes over entire run)

| Location | Total Allocated | Alloc Count |
|----------|----------------|-------------|
| `ast.parse` (ast.py:52) | 17.0 GB | — |
| `magic.file` (magic.py:122) | 15.1 GB | — |
| `to_pyfunc` (parsr/query/__init__.py:745) | 13.7 GB | 6.7M |
| `get_all_files` (hydration.py:19) | 7.1 GB | — |
| `_iterdir` (glob.py:160) | 6.5 GB | — |
| `_get_file_root` (ccx_ocp_core/context.py:115) | — | 6.8M |

Note: "Total Allocated" is cumulative — it counts every malloc over the
run, not what's held at any one time. The 95.5 GB total means memory is
being allocated and freed repeatedly, but some fraction leaks each cycle.

## Leaked Memory Breakdown (never freed at exit)

### By Category

| Category | Leaked | % of Total |
|----------|--------|------------|
| stdlib (json, importlib, ast) | 97.9 MB | 40.9% |
| other (click, uvicorn, asyncio) | 82.2 MB | 34.4% |
| parsr/query engine | 35.8 MB | 15.0% |
| magic/content_type (libmagic) | 7.5 MB | 3.1% |
| broker/dr (insights-core) | 7.0 MB | 2.9% |
| spec_factory | 5.3 MB | 2.2% |
| traceback | 2.0 MB | 0.8% |
| ccx_ocp_core | 1.4 MB | 0.6% |
| ccx_rules | 0.1 MB | 0.0% |

### Top Leaked Locations (by own bytes, not subtree)

| Own Leaked | Location |
|------------|----------|
| 15.3 MB | `raw_decode` at json/decoder.py:354 |
| 10.0 MB | `raw_decode` at json/decoder.py:354 |
| 6.5 MB | `load` at insights/contrib/magic.py:172 |
| 6.0 MB | `inner` at insights/parsr/query/__init__.py:965 |
| 5.0 MB | `__init__` at insights/parsr/query/__init__.py:86 |
| 4.0 MB | `__init__` at insights/parsr/query/__init__.py:88 |
| 3.7 MB | `__init__` at insights/parsr/query/__init__.py:74 |
| 3.0 MB | `parse` at ast.py:52 |
| 3.0 MB | `__init__` at insights/core/plugins.py:705 |
| 2.0 MB | `load` at insights/core/spec_factory.py:303 |

### Archive Processing Subtrees (per-thread leak)

Multiple background threads each hold leaked memory from archive processing:

| Subtree | Location |
|---------|----------|
| 21.1 MB | `_process_in_background` (thread 1) |
| 20.0 MB | `_process_in_background` (thread 2) |
| 18.0 MB | `_process_in_background` (thread 3) |
| 10.0 MB | `_process_in_background` (thread 4) |
| 7.7 MB | `_process_in_background` (thread 5) |
| **~59 MB** | **Total across all processing threads** |

Each thread's leak follows the same path:
`_process_in_background` -> `process_archive` -> `process_with_insights_core`
-> `run_components` -> `invoke` -> (parsr/query `from_dict`/`inner` or
`broker.add_exception`)

## Root Cause Analysis

### Primary: `broker.add_exception()` + traceback circular references

In `insights/core/dr.py`, `run_components()` catches exceptions and stores
them in the broker:

```python
# dr.py:1113 — on every exception during component execution
except Exception as ex:
    tb = traceback.format_exc()
    broker.add_exception(component, ex, tb)
```

`add_exception()` (dr.py:920) stores:
- `self.exceptions[component].append(ex)` — exception objects accumulate
- `self.tracebacks[ex] = tb` — traceback strings keyed by exception

The exception's `__traceback__` attribute holds a reference to the stack
frame, which holds local variables (including the broker), creating a
**circular reference** that CPython's reference counting cannot collect.
The GC can detect these cycles but may not run frequently enough under
heavy load.

### Secondary: Parsed data retained by broker

The broker's `instances` dict holds all component results (parsed YAML,
JSON documents built as `parsr.query` trees). These are 35.8 MB of
parsr/query objects that stay alive as long as the broker exists. Since
the broker is referenced from exception tracebacks (circular reference),
it cannot be freed.

### Tertiary: libmagic C buffers

`magic.load()` (magic.py:172) loads the libmagic database (6.5 MB) into
C-allocated memory. If the magic handle is not explicitly closed, this
memory is never returned to the OS.

## Fix Proposals with Estimated Savings

### Fix 1: Clear `ex.__traceback__` in `add_exception()` (PR #4763)

```python
# insights/core/dr.py — add_exception()
def add_exception(self, component, ex, tb=None):
    if isinstance(ex, MissingRequirements):
        self.missing_requirements[component] = ex.requirements
    else:
        ex.__traceback__ = None  # Break circular reference
        self.exceptions[component].append(ex)
        self.tracebacks[ex] = tb
```

- **Leaked saved**: ~59 MB (breaks the cycle holding broker + all parsed data)
- **Fragmentation saved**: ~350 MB (freed objects reduce heap holes)
- **Estimated RSS savings**: **~400 MB**
- **Effort**: 1 line change in insights-core

### Fix 2: Clear broker state after extracting results

```python
# app/services/processor_service.py — after process_with_insights_core()
broker.exceptions.clear()
broker.tracebacks.clear()
broker.instances.clear()
broker.missing_requirements.clear()
broker.exec_times.clear()
```

- **Leaked saved**: ~59 MB (same memory, freed from the other end)
- **Fragmentation saved**: ~300 MB
- **Estimated RSS savings**: **~350 MB**
- **Effort**: 5 lines in processor_service.py
- **Note**: Overlaps with Fix 1. Combined effect ~400 MB, not additive.

### Fix 3: Force `gc.collect()` after each archive

```python
# app/services/upload_service.py — _process_in_background() finally block
import gc
gc.collect()
```

- **Leaked saved**: ~20 MB (cycles missed by heuristic GC scheduling)
- **Fragmentation saved**: ~100 MB
- **Estimated RSS savings**: **~120 MB**
- **Effort**: 1 line

### Fix 4: Limit `traceback.format_exc()` depth

```python
# insights/core/dr.py — run_components(), change all format_exc() calls
broker.add_exception(component, ex, traceback.format_exc(limit=3))
```

- **Leaked saved**: ~4 MB (AST parse + linecache for frame source)
- **Fragmentation saved**: ~80 MB (reduces 17 GB AST churn to ~3 GB)
- **Estimated RSS savings**: **~80 MB**
- **Effort**: Change 4 call sites in dr.py

### Fix 5: Reuse/close libmagic handle

Reuse a single magic instance instead of creating one per file, or
ensure `close()` is called after each archive.

- **Leaked saved**: ~7.5 MB (magic.load C buffer + file() results)
- **Fragmentation saved**: ~10 MB
- **Estimated RSS savings**: **~17 MB**
- **Effort**: Low

### Fix 6: Subprocess isolation for archive processing

```python
# app/services/processor_service.py
import multiprocessing
with multiprocessing.Pool(1) as pool:
    result = pool.apply(self._do_process, (archive_path,))
```

- **Leaked saved**: All 59 MB from processing -> 0
- **Fragmentation saved**: All ~650 MB -> 0
- **Estimated RSS savings**: **~700 MB** (caps RSS at ~400 MB)
- **Effort**: Medium (IPC serialization, ~30ms overhead per archive)

## Combined Fix Estimates

| Combination | RSS Savings | RSS After Fix (est.) |
|-------------|-------------|---------------------|
| Fix 1 alone | ~400 MB | ~840 MB |
| Fix 1 + 2 | ~400 MB | ~840 MB (overlap) |
| Fix 1 + 2 + 3 | ~550 MB | ~690 MB |
| Fix 1 + 2 + 3 + 4 | ~600 MB | ~640 MB |
| Fix 1 + 2 + 3 + 4 + 5 | ~620 MB | ~620 MB |
| Fix 6 alone | ~700 MB | ~540 MB |

**Recommended**: Fixes 1 + 2 + 3 (quick wins, ~550 MB savings).
Fix 6 as a fallback if insights-core cannot be patched.

## Generated Reports

All reports are in the monitoring output directory:

- `memray-profile.bin` — raw profile (507 MB)
- `memray-flamegraph.html` — all allocations flamegraph
- `memray-leaks-flamegraph.html` — leaked allocations only (most useful)
- `memray-leaks-table.html` — sortable table of leaked locations
- `memray-table.html` — sortable table of all allocations
- `insights-app_podman_stats.csv` — container CPU/memory over time
- `insights-app_process_memory.csv` — /proc VmRSS/VmData over time
- `SUMMARY.txt` — monitoring summary
