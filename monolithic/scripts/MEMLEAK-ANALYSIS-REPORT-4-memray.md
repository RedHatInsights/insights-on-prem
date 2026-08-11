# Memray Leak Analysis Report #4

**Profile:** `monitoring_20260806_133108/memray-profile.bin`
**Date:** 2026-08-06
**Branch:** `mzibrick-memleak-fixes_and_one_thread`
**Total allocations:** 3,473,073 | **Total allocated:** 9.51 GB | **Peak RSS:** 163.5 MB
**Threads observed:** 2 (main + ThreadPoolExecutor-0_0)

---

## Overview

Memray `--leaks` mode reports allocations that were **never freed** at profile end.
The top-level breakdown:

| Category | Leaked | Notes |
|----------|--------|-------|
| insights-core (components, parsers, broker) | ~518 MB cumulative through-flow | Dominates the call tree |
| ccx_ocp_core (ProductLifeCycle, Entry trees) | ~68 MB | Biggest app-level offender |
| Application code (`/app/`) | ~240 MB through-flow | Per-request growth |
| libmagic C-extension | 6.48 MB | Native heap, never freed |
| SQLAlchemy caches | ~21 MB | Module-level, slow growth |
| Import system (bytecode compile, frozen modules) | ~40 MB | One-time, expected |

> "Through-flow" means the bytes pass through that frame on the call stack; "own"
> means the frame itself performed the allocation.

---

## LEAK #1 (CRITICAL) -- ProductLifeCycle + Entry tree: ~20 MB leaked, grows per request

### What

`ccx_ocp_core/models/product_lifecycle.py` `ProductLifeCycle.__init__()` calls
`_parse_raw_data()` which:

1. `json.loads(raw)` -- parses the bundled `product_lifecycle.json` (~5 MB of dicts)
2. `dict_to_query(data)` / `from_dict()` -- builds a deep tree of
   `insights.parsr.query.Entry` objects (~15 MB)

### Evidence from memray

```
__init__ at product_lifecycle.py:113     -- 20.01 MB (32 allocs)
  _load_packaged_data at :22             -- 20.01 MB (32 allocs)
    _parse_raw_data at :15               -- 15.00 MB (17 allocs) -> Entry tree
    _parse_raw_data at :14               --  5.01 MB (15 allocs) -> json.loads
```

### Why it leaks

- `ProductLifeCycle` is an insights `@component()`. The insights `dr.py` engine
  invokes it **freshly for each archive** via `dr.process() -> invoke()`.
- `_load_packaged_data()` has `@functools.cache` so the raw data is loaded once,
  but `__init__` still assigns `self.data = data` and `self.query = query` on every
  invocation, creating new references from each `ProductLifeCycle` instance.
- The `Entry` tree has **circular parent-child references** (`Entry.parent = self`
  at `insights/parsr/query/__init__.py:87`). Python's cyclic GC can normally
  handle these, but they become pinned when:
  - Exception tracebacks capture stack frames that reference the broker/component
  - The broker retains component results without explicit cleanup
- The profile shows 32 invocations of `__init__` -- each one holds a reference to
  the cached `(data, query)` tuple, preventing the Entry tree from being freed.

### Leak source code

```python
# ccx_ocp_core/models/product_lifecycle.py
def _parse_raw_data(raw):
    data = json.loads(raw)            # line 14: 5.01 MB leaked (15 allocs)
    query = dict_to_query(data)       # line 15: 15.00 MB leaked (17 allocs)
    return (data, query)

@functools.cache
def _load_packaged_data():
    raw = resources.files("ccx_ocp_core.data").joinpath("product_lifecycle.json").read_bytes()
    return _parse_raw_data(raw)       # cached, but callers hold refs to result

@component()
class ProductLifeCycle:
    def __init__(self, raw=None):
        if raw is not None:
            data, query = _parse_raw_data(raw)   # NOT cached path
        else:
            data, query = _load_packaged_data()   # cached path
        self.data = data    # new reference from each instance
        self.query = query  # new reference from each instance
```

### Entry tree circular references

```python
# insights/parsr/query/__init__.py
class Entry(object):
    __slots__ = ("_name", "attrs", "children", "parent", "lineno", "src")

    def __init__(self, name=None, attrs=None, children=None, ...):
        ...
        self.children = children if isinstance(children, (list, tuple)) else []
        self.parent = None
        if set_parents:
            for c in self.children:
                c.parent = self        # <-- circular reference: child.parent -> parent
```

The `from_dict()` recursive `inner()` function builds nested `Entry` objects:

```python
def from_dict(orig, src=None):
    def inner(d):
        result = []
        for k, v in d.items():
            if isinstance(v, dict):
                result.append(Entry(name=k, children=inner(v)))     # line 954
            elif isinstance(v, list):
                res = [Entry(name=k, children=inner(i)) if isinstance(i, dict) else i for i in v]
                ...
                result.extend(res)                                   # line 956
            else:
                result.append(Entry(name=k, attrs=(v,)))             # line 965
        return tuple(result)
    return Entry(children=inner(orig), src=src)                      # line 968
```

Leaf allocations in the Entry tree:

| Line | What | Own leaked | Allocs |
|------|------|-----------|--------|
| 88 | `__init__` attrs tuple | 5.00 MB | 5 |
| 965 | `inner` -- `Entry(name=k, attrs=(v,))` | 4.00 MB | 12 |
| 74 | `__init__` -- `sys.intern(name)` | 3.67 MB | 1 |
| 81 | `__init__` -- children list | 2.00 MB | 2 |
| 966 | `inner` -- `Entry(name=k)` | 2.00 MB | 2 |

---

## LEAK #2 (SIGNIFICANT) -- libmagic C-extension: 6.48 MB leaked

### What

`insights/contrib/magic.py:172` calls `_load()` which is a `ctypes` binding to
`libmagic`'s `magic_load()` C function. This loads the magic database
(`/usr/share/misc/magic.mgc`, ~830 KB per call) into **C heap memory**.

### Evidence from memray

```
load at insights/contrib/magic.py:172  -- 6.48 MB (8 allocs)
  -> 8 allocs x ~830 KB = 6.48 MB of C heap
```

### Why it leaks

The `Magic` class has a `close()` method but **no `__del__` destructor**:

```python
# insights/contrib/magic.py
class Magic(object):
    def __init__(self, ms):
        self._magic_t = ms          # raw ctypes pointer to C struct

    def close(self):
        _close(self._magic_t)       # frees C memory -- but must be called explicitly

    def load(self, filename=None):
        return _load(self._magic_t, filename)   # allocates ~830KB of C heap
    # NO __del__ -- C memory is never freed if close() is not called
```

The module-level singleton in `content_type.py` is fine (one instance, lives
forever). But the profile shows **8 `load()` calls** -- meaning `magic.open()` +
`magic.load()` was called 8 times, likely from multiple threads or re-initialization.
Each call allocates C heap memory that is **never freed** because:

1. `close()` is never called on prior instances
2. There is no `__del__` to clean up when the Python wrapper is GC'd
3. The C heap memory is invisible to Python's garbage collector

**This is a genuine C-extension memory leak.**

---

## LEAK #3 (MODERATE) -- Per-request growth in process_archive: ~25.8 MB over run

### What

Each call to `_process_in_background` → `process_archive` → `process_with_insights_core`
leaks memory that is not recovered between requests.

### Evidence from memray

```
_process_in_background at upload_service.py:130   -- 26.84 MB (225 allocs)
  process_archive at processor_service.py:317     -- 25.82 MB (206 allocs)
    process_with_insights_core at :161            -- 25.78 MB  (47 allocs)
      dr.run_components → dr.process → invoke    -- 25.71 MB  (46 allocs)
        ProductLifeCycle.__init__                 -- 20.01 MB  (32 allocs)  [LEAK #1]
        insights/core/plugins.py:182 (parsers)   --  4.67 MB   (3 allocs)
          ccx_ocp_core/utils/mixins.py:22         --  3.67 MB   (1 alloc)  [Entry tree]
          yaml load                               --  1.00 MB   (1 alloc)
    save_results at :320                          --  1.01 MB  (19 allocs)
      RuleHit.upsert → SQLAlchemy excluded cols  --  1.00 MB   (1 alloc)
```

### Sub-leaks

**a) ccx_ocp_core/utils/mixins.py:22 `parse_content` → `add_query` → `from_dict`**

Another `from_dict()` call building an Entry tree from YAML-parsed content. Same
circular reference problem as Leak #1. 3.67 MB leaked from a single invocation.

**b) insights/core/dr.py:791 `get_missing_dependencies`**

Accumulates into global state: 1 MB leaked. The dependency graph metadata grows
but is never pruned between requests.

**c) SQLAlchemy `excluded` column setup (lazy property)**

`models.py:135` → `sqlalchemy/dialects/postgresql/dml.py:101` → column collection
setup: 1 MB. This is a lazy-initialized property that gets cached permanently on
the `Insert` statement object.

---

## LEAK #4 (MINOR) -- insights/core/dr.py global state

### Evidence from memray

```
get_missing_dependencies at dr.py:791  -- 1.00 MB (own allocation, not pass-through)
```

The `dr` module maintains global dictionaries (`DELEGATES`, `COMPONENTS`,
dependency graphs) that accumulate entries. These are module-level and never
cleaned up between archive processing runs.

---

## LEAK #5 (MINOR) -- SQLAlchemy annotation type cache: ~157 KB growing

### What

`sqlalchemy/sql/annotation.py:557` `_new_annotation_type` creates dynamic
annotation subclasses cached in a module-level dict. Each new query pattern
adds a new type that is never evicted.

### Evidence from memray

```
_new_annotation_type at annotation.py:557  -- 157.52 KB (own)
```

This is a known SQLAlchemy behavior and unlikely to cause problems unless the
application generates many distinct query patterns.

---

## C-Extension Summary

| Library | Leak? | Mechanism | Size |
|---------|-------|-----------|------|
| `libmagic` (via `insights/contrib/magic.py`) | **YES** | `magic_load()` allocates C heap, no `__del__` or automatic `close()` | 6.48 MB |
| `libpq` (PostgreSQL via SQLAlchemy/psycopg2) | No | Connection pooling handles cleanup | -- |
| `libyaml` (via PyYAML) | No | Properly freed after parsing | -- |
| `libz` / archive extraction | No | Context manager (`extract()`) handles cleanup | -- |

The only C-extension leak is `libmagic`. All other native libraries appear to
clean up properly.

---

## Variables Overwritten and Never Released

No classic "variable overwritten, old value leaked" pattern was found. The leaks
are caused by:

1. **Circular references** in the `Entry` tree (`parent` ↔ `children`) that resist
   GC when pinned by exception tracebacks or broker state
2. **C-extension memory** from `libmagic` allocated via `ctypes` without a destructor
3. **Module-level caches** (`functools.cache`, SQLAlchemy annotation types) that
   accumulate over time
4. **Insights broker** retaining component results between invocations -- the
   `broker.cleanup()` call at `processor_service.py:177-178` addresses this but
   the Entry trees' circular references may still prevent full cleanup

---

## Recommendations

### High priority

1. **ProductLifeCycle Entry tree** -- The biggest single leak. Options:
   - Patch `Entry.__init__` to use `weakref` for `parent` references
   - Add explicit `Entry.detach()` that clears parent/children before discarding
   - Ensure `broker.cleanup()` breaks all references to component results including
     nested Entry trees
   - Consider making `ProductLifeCycle` return the cached instance directly instead
     of creating a new wrapper each time

2. **libmagic C-extension** -- Add `__del__` to `Magic` class:
   ```python
   class Magic(object):
       def __del__(self):
           if self._magic_t is not None:
               _close(self._magic_t)
               self._magic_t = None
   ```
   Or ensure only one `magic.open()` + `load()` call ever happens (the
   `content_type.py` singleton pattern is correct, but something is creating
   additional instances).

### Medium priority

3. **Broker cleanup** -- Verify that `broker.cleanup()` is effectively breaking all
   reference chains. The current implementation at `processor_service.py:177` only
   runs if the patched insights-core is installed (`hasattr(broker, 'cleanup')`).

4. **Exception traceback pinning** -- Ensure exception handlers use
   `sys.exc_clear()` or `del` on exception variables to prevent tracebacks from
   pinning Entry trees and broker state.

### Low priority

5. **SQLAlchemy annotation cache** -- Not actionable; this is SQLAlchemy internals.
   Monitor for unexpected growth.

6. **dr.py global state** -- Consider whether `DELEGATES`, `COMPONENTS` dicts can
   be scoped per-request rather than accumulated globally.

---

## How to reproduce

```bash
# Generate the profile
memray run -o memray-profile.bin -- uvicorn app.main:app

# Analyze leaks (allocations never freed)
memray flamegraph --leaks -o leaks.html memray-profile.bin

# Get stats
memray stats memray-profile.bin

# Get top allocators tree
memray tree --biggest-allocs 30 memray-profile.bin
```
