# Memory Improvements - More Fixes #1

**Date:** 2026-08-07
**Branch:** `mzibrick-memleak-fixes` (both repos)
**Based on:** Memray analysis from `monitoring_20260806_133108/memray-profile.bin`
**Report:** See `MEMLEAK-ANALYSIS-REPORT-4-memray.md` for the full memray leak analysis.

---

## Problem

Memray profiling revealed ~60 MB of memory leaked per archive processing cycle.
Three root causes:

1. **Entry tree circular references (~35 MB)** -- `insights.parsr.query.Entry`
   objects form parent-child cycles (`child.parent = self`) that resist garbage
   collection when pinned by broker state or exception tracebacks.  The biggest
   single source is `ProductLifeCycle` (~20 MB of Entry tree from
   `product_lifecycle.json`), plus ~4.7 MB from parser mixins using `from_dict()`.

2. **Broker.cleanup() doesn't unwrap lists** -- Parser components store results
   as lists (`parser.invoke()` returns a list of parser instances at
   `plugins.py:240`).  `Broker.cleanup()` checked `hasattr(instance, 'cleanup')`
   on the **list itself**, not on individual parsers inside it.  So
   `QueryMixinDict.cleanup()` and `QueryMixinList.cleanup()` were **never called**.

3. **libmagic C-extension leak (6.48 MB)** -- `Magic` class in
   `insights/contrib/magic.py` wraps `libmagic` via ctypes.  It has `close()` but
   no `__del__`, so when `Magic` objects are garbage collected the C heap memory
   from `magic_load()` (~830 KB per call) is never freed.

---

## Changes

### insights-core (`/Users/mzibrick/Projects/RH-ccx/insights-core/`)

#### 1. Added `Entry.detach()` method

**File:** `insights/parsr/query/__init__.py`

Added a method that recursively breaks `parent` and `src` references in an
Entry subtree to help garbage collection:

```python
def detach(self):
    self.parent = None
    self.src = None
    for c in self.children:
        if isinstance(c, Entry):
            c.detach()
```

- `parent` creates the circular reference (child.parent -> parent -> child via
  `.children`)
- `src` holds a reference to the Parser instance which can be large
- `isinstance` guard is defensive -- `from_dict()` can put non-Entry values in
  children lists (scalar attrs stored in `attrs` tuple, but list entries at
  line 956/961 can be raw values)
- Tree depth is shallow (~5 levels for product lifecycle JSON), no stack
  overflow risk

#### 2. Fixed `Broker.cleanup()` to unwrap list instances

**File:** `insights/core/dr.py` (inside `Broker.cleanup()`)

**Before:**
```python
for instance in self.instances.values():
    if hasattr(instance, 'cleanup') and callable(instance.cleanup):
        try:
            instance.cleanup()
        except Exception:
            pass
```

**After:**
```python
for instance in self.instances.values():
    items = instance if isinstance(instance, list) else (instance,)
    for item in items:
        if hasattr(item, 'cleanup') and callable(item.cleanup):
            try:
                item.cleanup()
            except Exception:
                pass
```

- `parser.invoke()` (plugins.py:240) returns a `list` of parser instances
- The old code checked the **list** for `cleanup` -- lists don't have it
- The fix unwraps lists so each parser's `cleanup()` is called
- Uses `(instance,)` tuple for the single-instance case to avoid allocating
  a new list

#### 3. Added `Magic.__del__` and made `close()` idempotent

**File:** `insights/contrib/magic.py`

Updated `close()` to guard against double-free:

```python
def close(self):
    """Closes the magic database and deallocates any resources used."""
    if self._magic_t is not None:
        _close(self._magic_t)
        self._magic_t = None
```

Added `__del__` after `close()`:

```python
def __del__(self):
    try:
        if self._magic_t is not None:
            _close(self._magic_t)
    except Exception:
        pass
    self._magic_t = None
```

- Without `__del__`, C memory from `magic_load()` is never freed when the
  Python wrapper is garbage collected
- `try/except` in `__del__` is required: during interpreter shutdown `_close`
  may already be garbage collected
- `self._magic_t = None` guard in both methods prevents double-free

---

### ccx-ocp-core (`/Users/mzibrick/Projects/RH-ccx/ccx-ocp-core/`)

#### 4. Added `ProductLifeCycle.cleanup()`

**File:** `ccx_ocp_core/models/product_lifecycle.py`

**Critical constraint:** `_load_packaged_data()` uses `@functools.cache`.  When
`raw=None`, `self.data` and `self.query` point to **shared cached objects**
(confirmed by `test_cache` at line 75-86 in tests: `lifecycle1.data is
lifecycle2.data`).  Calling `detach()` on the cached Entry tree would corrupt it
for all future invocations.

Added `_from_cache` flag in `__init__`:

```python
if raw is not None:
    data, query = _parse_raw_data(raw)
    self._from_cache = False
else:
    data, query = _load_packaged_data()
    self._from_cache = True
```

Added `cleanup()` method:

```python
def cleanup(self):
    """Release references to help garbage collection.

    When the data was parsed from a raw string, the Entry tree is
    detached to break circular parent references.  Cached trees are
    left intact for reuse by future invocations.
    """
    if not self._from_cache and self.query is not None:
        if hasattr(self.query, 'detach'):
            self.query.detach()
    self.query = None
    self.data = None
```

- For cached data: only drops instance references (cache keeps its own)
- For non-cached data: calls `detach()` to break circular refs before dropping
- `hasattr(self.query, 'detach')` guard for backward compatibility with
  un-patched insights-core
- `Broker.cleanup()` calls `instance.cleanup()` on each component, so this
  is invoked automatically during broker teardown

#### 5. Updated QueryMixin `cleanup()` to call `detach()`

**File:** `ccx_ocp_core/utils/mixins.py`

Updated both `QueryMixinDict.cleanup()` and `QueryMixinList.cleanup()`:

```python
def cleanup(self):
    """Release the query Entry tree to free memory."""
    if hasattr(self, "q") and self.q is not None:
        if hasattr(self.q, "detach"):
            self.q.detach()
        self.q = None
    if hasattr(self, "data"):
        self.data = None
```

- QueryMixin trees are always freshly created per parser instance (never
  cached), so `detach()` is always safe
- `hasattr(self.q, 'detach')` guard decouples deployment ordering -- works
  even with un-patched insights-core
- `hasattr(self, 'q')` handles the case where `add_query()` was never called
  (e.g. if `parse_content` raised before `add_query()`)

---

## How the fixes work together

The cleanup flow for each archive processing request:

```
processor_service.py: process_with_insights_core()
  -> dr.run_components(target_components, components_dict, broker)
     # Components execute, results stored in broker.instances
  -> broker.cleanup()                              # [existing call at line 178]
     -> Clears exception.__traceback__ on all stored exceptions
     -> Iterates broker.instances.values():
        -> Unwraps lists (FIX #2)                  # [NEW]
        -> Calls item.cleanup() on each:
           -> ProductLifeCycle.cleanup() (FIX #4)  # [NEW]
              -> Calls query.detach() (FIX #1)     # [NEW] -- breaks Entry cycles
           -> QueryMixinDict.cleanup() (FIX #5)    # [UPDATED]
              -> Calls q.detach() (FIX #1)         # [NEW] -- breaks Entry cycles
     -> Clears broker.instances, exceptions, tracebacks, exec_times
     -> Clears linecache and BLACKLISTED_SPECS
```

Meanwhile, `Magic.__del__` (FIX #3) runs independently whenever a `Magic` object
is garbage collected, freeing the C heap memory from `magic_load()`.

---

## Test results

All tests pass with no regressions:

| Test suite | Result |
|------------|--------|
| insights-core `insights/parsr/query/tests/` | 21 passed |
| insights-core `insights/tests/core/test_dr.py` | 6 passed |
| insights-core `test_dr_run.py::test_run_command` | 1 failed (pre-existing, unrelated) |
| ccx-ocp-core `tests/unit/models/test_product_lifecycle.py` | 13 passed |
| ccx-ocp-core `tests/unit/utils/test_mixins.py` | 6 passed |

---

## Expected memory impact

| Fix | Leak addressed | Expected savings per archive |
|-----|---------------|------------------------------|
| Entry.detach() + ProductLifeCycle.cleanup() | Entry tree from product_lifecycle.json | ~20 MB |
| Entry.detach() + QueryMixin.cleanup() | Entry trees from YAML/JSON parsers | ~4.7 MB |
| Broker.cleanup() list unwrapping | Enables fixes above to actually run | (enabler) |
| Magic.__del__ | libmagic C heap memory | ~6.5 MB (one-time) |
| **Total** | | **~31 MB per archive** |

---

## Files changed

### insights-core
- `insights/parsr/query/__init__.py` -- Added `Entry.detach()` method
- `insights/core/dr.py` -- Fixed `Broker.cleanup()` list unwrapping
- `insights/contrib/magic.py` -- Added `Magic.__del__()`, made `close()` idempotent

### ccx-ocp-core
- `ccx_ocp_core/models/product_lifecycle.py` -- Added `_from_cache` flag and `cleanup()`
- `ccx_ocp_core/utils/mixins.py` -- Updated `cleanup()` to call `detach()`

---

## Next steps

- Re-run memray profiling on insights-on-prem to confirm memory reduction
- Check if `functools.cache` on `_load_packaged_data()` should be switched to
  an LRU cache or manually cleared between runs (currently caches indefinitely)
- Investigate remaining ~25 MB of per-request broker state that may not be fully
  covered by cleanup (exception traceback pinning, linecache accumulation)
