# Memory Improvements - More Fixes #3: One-Time Resident Costs

**Date:** 2026-08-07
**Based on:** Memray profile `monitoring_20260807_113246/memray-profile.bin` (with fixes from round 1 applied)
**Previous reports:** `MEMORY-IMPROVEMENTS-MORE-FIXES-1.md` (round 1 fixes), `MEMLEAK-ANALYSIS-REPORT-4-memray.md`

---

## Background

After round 1 fixes (Entry.detach, Broker.cleanup list unwrapping, Magic.__del__,
ProductLifeCycle.cleanup, QueryMixin.cleanup, _format_exc_short), memray confirmed
that **per-request memory IS being freed** — the leak numbers are identical whether
measured during or after processing 1474 archives.

The remaining "leaks" in the memray report are actually **one-time resident costs**
that never shrink:

| Cost | Size | Root cause |
|------|------|------------|
| ProductLifeCycle `@functools.cache` | 21 MB | Entry tree + JSON dict cached forever, not clearable |
| magic.py singleton | 6.48 MB | 1 `_magic.load()` making 8 internal C mallocs, lives forever |
| ClusterVersionOS parser | 3.67 MB | Manual `from_dict()` call, no cleanup() method |
| namedtuple Entry trees (Events, Claims) | ~2 MB | `from_dict()` in namedtuples held by ModelsHolder |
| Python allocator fragmentation | ~50 MB RSS | pymalloc doesn't return freed pages to OS |

Despite per-request cleanup working, monitoring showed **+53 MB RSS growth** over
16 minutes. This discrepancy is explained by Python's memory allocator: CPython's
`pymalloc` arena allocator does not return freed pages to the operating system.
`gc.collect()` recycles Python objects, but the underlying heap pages stay mapped.

---

## Key finding: magic.py 8 load() calls

Investigation found only 2 import sites for `insights.contrib.magic`:

1. `insights/util/content_type.py:14` — module-level singleton (the intended one)
2. `insights/client/connection.py:893` — legacy upload path (not used by insights-on-prem)

The 8 `load()` calls in the memray profile are NOT 8 separate `magic.open()` instances.
They are **8 internal C-level malloc calls** within a single `magic_load()` invocation
(loading different sections of the magic database). This is a one-time cost of the
module-level singleton — not actionable beyond the `__del__` fix already in place.

---

## Changes

### 1. Added cleanup() to ClusterVersionOS parser

**File:** `ccx-ocp-core/ccx_ocp_core/specs/openshift.py`

`ClusterVersionOS` manually calls `query.from_dict(self.data)` at line 42 and stores
the result in `self.q`. It cannot use `QueryMixinDict` due to MRO/execution order
(noted in the existing comment). Without a `cleanup()` method, `Broker.cleanup()` had
no way to release this Entry tree.

```python
def cleanup(self):
    if self.q is not None:
        if hasattr(self.q, "detach"):
            self.q.detach()
        self.q = None
```

This mirrors the pattern already used in `QueryMixinDict.cleanup()`.
`Broker.cleanup()` calls `instance.cleanup()` on each component, so this is
invoked automatically.

**Impact:** -3.67 MB per archive processing cycle.

### 2. Added malloc_trim() after gc.collect()

**File:** `insights-on-prem/monolithic/app/services/upload_service.py`

`gc.collect()` at line 144 already runs after each archive. It frees Python objects
but does NOT return the freed heap pages to the OS. On Linux with glibc,
`malloc_trim(0)` tells the C allocator to release free pages back to the kernel.
This directly addresses the RSS growth visible in monitoring.

```python
gc.collect()
try:
    import ctypes
    ctypes.CDLL("libc.so.6").malloc_trim(0)
except Exception:
    pass
```

- The `try/except` handles macOS (no `libc.so.6`) and non-glibc systems gracefully
- The container runs Linux/glibc where this will work
- On macOS development the call silently no-ops
- Only affects RSS reporting; does not change Python's internal memory management

**Impact:** Expected ~50 MB RSS reduction during cool-down. RSS should drop after
processing stops instead of staying flat at the high-water mark.

### 3. Converted `@functools.cache` to `@functools.lru_cache(maxsize=1)`

**File:** `ccx-ocp-core/ccx_ocp_core/models/product_lifecycle.py`

Changed `_load_packaged_data()` decorator from `@functools.cache` to
`@functools.lru_cache(maxsize=1)`.

Functionally identical — caches a single result (there's only one call signature:
no arguments). The difference is that `lru_cache` exposes `cache_clear()` and
`cache_info()`, making the cache inspectable and clearable for future use.

The `ProductLifeCycle.cleanup()` method (added in round 1) already handles the
`_from_cache` flag correctly: it drops instance references but does NOT call
`detach()` on the cached Entry tree. The cached tree stays alive for reuse by
the next archive. If memory pressure becomes critical, a future hook can call
`_load_packaged_data.cache_clear()` to reclaim the 21 MB.

**Impact:** No immediate memory change. Enables future cache management.

### 4. Added cleanup() to ModelsHolder base class

**File:** `ccx-ocp-core/ccx_ocp_core/models/models_base.py`

Several combiners create Entry trees via `query.from_dict()` inside namedtuples
(`Event`, `Claim`). Namedtuples can't have methods, so cleanup must happen at the
container level. `ModelsHolder` is the base class for all model containers.

```python
def cleanup(self):
    """Detach Entry trees held by source items to aid garbage collection."""
    if self.source_data is not None:
        items = self.source_data if isinstance(self.source_data, list) else [self.source_data]
        for item in items:
            q = getattr(item, "q", None)
            if q is not None and hasattr(q, "detach"):
                q.detach()
        self.source_data = None
    if self.query is not None:
        if hasattr(self.query, "detach"):
            self.query.detach()
        self.query = None
    self.models = []
```

This covers all combiners that inherit from or return `ModelsHolder`:
- `Events` (class-based combiner) — creates `Event` namedtuples with `from_dict()`
- `PersistentVolumeClaims` (function-based combiner) — creates `Claim` namedtuples
- Any future combiner using `ModelsHolder`

`Broker.cleanup()` calls `instance.cleanup()` on component results. Since combiners
return `ModelsHolder` instances stored in the broker, the cleanup is automatic.

**Impact:** -2 MB from Event/Claim Entry trees per archive cycle.

---

## Files changed

| Repo | File | Change |
|------|------|--------|
| ccx-ocp-core | `ccx_ocp_core/specs/openshift.py` | Added `cleanup()` to `ClusterVersionOS` |
| insights-on-prem | `app/services/upload_service.py` | Added `malloc_trim(0)` after `gc.collect()` |
| ccx-ocp-core | `ccx_ocp_core/models/product_lifecycle.py` | `@functools.cache` → `@functools.lru_cache(maxsize=1)` |
| ccx-ocp-core | `ccx_ocp_core/models/models_base.py` | Added `cleanup()` to `ModelsHolder` |

## Test results

98 tests passed (product_lifecycle, mixins, specs, version):

```
tests/unit/models/test_product_lifecycle.py   — 13 passed
tests/unit/utils/test_mixins.py               —  6 passed
tests/unit/specs/                             — 72 passed
tests/unit/models/test_version.py             —  7 passed
```

---

## Cumulative fix summary (round 1 + round 3)

### Round 1 fixes (MEMORY-IMPROVEMENTS-MORE-FIXES-1.md)

| Fix | Repo | Impact |
|-----|------|--------|
| `Entry.detach()` method | insights-core | Breaks circular parent refs in Entry trees |
| `Broker.cleanup()` list unwrapping | insights-core | Enables cleanup of parser instances |
| `Magic.__del__` + idempotent `close()` | insights-core | Frees C heap on GC |
| `_format_exc_short()` | insights-core | -1.03 GB total allocations (no ast.parse) |
| `ProductLifeCycle.cleanup()` | ccx-ocp-core | Drops Entry tree refs after use |
| `QueryMixin.cleanup()` with `detach()` | ccx-ocp-core | Detaches parser Entry trees |

### Round 3 fixes (this file)

| Fix | Repo | Impact |
|-----|------|--------|
| `ClusterVersionOS.cleanup()` | ccx-ocp-core | -3.67 MB (orphaned Entry tree) |
| `malloc_trim(0)` after gc.collect | insights-on-prem | ~50 MB RSS reduction expected |
| `lru_cache(maxsize=1)` | ccx-ocp-core | Makes lifecycle cache clearable |
| `ModelsHolder.cleanup()` | ccx-ocp-core | -2 MB (combiner Entry trees) |

### Remaining one-time costs (not actionable)

| Cost | Size | Why it stays |
|------|------|-------------|
| ProductLifeCycle cache | 21 MB | Intentional — reused across all archives |
| magic.py singleton | 6.48 MB | Single module-level instance, lives forever |
| Import system (bytecode) | ~40 MB | One-time module loading at startup |

---

## Verification

Re-run monitoring with `reproduce_leak.sh` to confirm:

1. **malloc_trim effect:** RSS should drop during cool-down instead of staying flat
   at high-water mark. Only visible on Linux (the container), not macOS.
2. **ClusterVersionOS + ModelsHolder cleanup:** Should show ~5.7 MB less in the
   memray `--leaks` report for these components.
3. **lru_cache:** `test_cache` test confirms identical caching behavior.
