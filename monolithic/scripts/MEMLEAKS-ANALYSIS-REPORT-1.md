# Memory Leak Analysis Report

**Date**: 2026-07-31
**Profile source**: `monitoring_20260731_055945-memray_profiler_60min_30cooldown/memray-profile.bin`
**Duration**: 90 minutes (60 min load + 30 min cooldown)
**Tool**: memray 1.19.3

---

## Executive Summary

The insights-app process leaks approximately **431 MB over 90 minutes** (+288 MB/hr by VmRSS, +599 MB/hr at container level). Memray's `--leaks` filter identifies **239.2 MB of never-freed allocations** across 53,810 allocation sites.

Six distinct leak categories account for the bulk of retained memory. The top three — module re-import, ProductLifecycle JSON parsing, and parser tree retention — together account for **143 MB (60%)** of the total leaked memory.

---

## Profile Overview

| Metric | Value |
|--------|-------|
| Total allocations (lifetime) | 35.5M |
| Total memory allocated (cumulative throughput) | 95.6 GB |
| Peak memory at any instant | 458.7 MB |
| **Never-freed memory (leaked)** | **239.2 MB** |
| VmRSS start → end | 384 MB → 826 MB (+431 MB) |
| Container mem start → end | 353 MB → 1241 MB (+888 MB) |
| Postgres VmRSS delta | 0 MB (no process-level leak) |

> **Note on "Total allocated" vs "Leaked"**: 95.6 GB flowed through the allocator over 90 minutes, but most was freed. The 239.2 MB represents allocations that were never freed by process exit — this is the actual leak.

---

## Leak Breakdown by Component

| # | Component | Leaked | % of Total | Allocs | Primary Sites |
|---|-----------|--------|------------|--------|---------------|
| 1 | importlib (module loading) | 78.1 MB | 32.6% | 30,002 | `_compile_bytecode`, `_call_with_frames_removed` |
| 2 | insights.parsr (query engine) | 35.8 MB | 15.0% | 176 | `Entry.__init__`, `from_dict`, `inner`, `to_pyfunc` |
| 3 | Python stdlib (other) | 33.6 MB | 14.0% | 215,777 | `linecache.updatecache`, `configparser.__init__` |
| 4 | json.decoder | 29.3 MB | 12.3% | 93 | `raw_decode` (ProductLifecycle JSON) |
| 5 | SQLAlchemy | 18.8 MB | 7.9% | 4,677 | `sql/functions`, `orm/path_registry`, `create_for_statement` |
| 6 | insights.core | 16.6 MB | 6.9% | 703 | `plugins.py:705` (ContentException), `spec_factory.load` |
| 7 | insights.contrib.magic | 6.5 MB | 2.7% | 145 | `magic.py:172` (libmagic library load) |
| 8 | YAML parsing | 6.3 MB | 2.6% | 395 | `yaml/reader`, `yaml/scanner`, `yaml/composer` |
| 9 | dateutil | 3.6 MB | 1.5% | 1,304 | `_read_tzfile` (timezone data cached) |
| 10 | ast (stdlib) | 3.0 MB | 1.3% | 12 | `ast.parse` (traceback caret anchors) |

---

## Detailed Findings and Fixes

### Finding 1: Module Re-import on Every Archive

**Leaked: 78.1 MB (32.6%) — 30,002 allocations**

**What happens**: Every call to `process_with_insights_core` triggers import chains through `ccx_ocp_core.core.plugins` -> `ccx_ocp_core.utils.parsers` -> `ccx_ocp_core.specs` -> dozens of rule modules. Each import compiles bytecode (`_compile_bytecode` at `importlib._bootstrap_external:757`) and executes module-level code (`_call_with_frames_removed` at `importlib._bootstrap:488`). The compiled bytecode and module objects accumulate in `sys.modules` and are never evicted.

**Call path**:
```
ccx_ocp_core/core/plugins.py:6 (<module>)
  -> _find_and_load (importlib._bootstrap:1360)
    -> _compile_bytecode (importlib._bootstrap_external:757)
    -> _call_with_frames_removed (importlib._bootstrap:488)
Subtree total: 70.4 MB
```

**Leak evidence**: Multiple distinct subtrees (70.4 MB, 69.1 MB, 37.9 MB) all rooting at `ccx_ocp_core` module imports that fan out through `_find_and_load`, indicating repeated discovery and compilation of the same module graph.

**Proposed fix**: Import all rule modules once at startup and reuse the loaded component registry. Check whether `dr.load_components()` or equivalent is being called per-request — it should be called once at app startup and cached. If `ccx_ocp_core` uses lazy imports via `importlib.import_module()` in a loop, hoist them to module scope or guard with `if module_name not in sys.modules`.

---

### Finding 2: ProductLifecycle JSON Data Re-parsed and Retained

**Leaked: 29.3 MB (12.3%) — 93 allocations**

**What happens**: `ccx_ocp_core/models/product_lifecycle.py` calls `_load_packaged_data()` -> `_parse_raw_data()` -> `json.loads()` on every instantiation. The parsed JSON objects (10-16 MB per call) are then converted to `insights.parsr.query.Entry` trees via `from_dict()`, and neither the raw JSON nor the Entry trees are freed.

**Call path**:
```
product_lifecycle.py:113 (__init__)
  -> _load_packaged_data:22
    -> _parse_raw_data:14
      -> json/__init__.py:346 (loads)
        -> json/decoder.py:354 (raw_decode) — 10.01 MB OWN
    -> _parse_raw_data:15
      -> from_dict (insights/parsr/query/__init__.py:968)
        -> Entry.__init__ — 16.00 MB subtree
```

**Proposed fix**: Make `ProductLifecycle` a singleton or cache `_load_packaged_data()` at the class level. The lifecycle data is static and identical across instances:

```python
class ProductLifecycle:
    _cached_data = None

    def __init__(self):
        if ProductLifecycle._cached_data is None:
            ProductLifecycle._cached_data = self._load_packaged_data()
        self.data = ProductLifecycle._cached_data
```

Alternatively, use `functools.lru_cache(maxsize=1)` on `_load_packaged_data` if it's a standalone function.

---

### Finding 3: insights.parsr Entry Trees Retained Per Request

**Leaked: 35.8 MB (15.0%) — 176 allocations**

**What happens**: Each parsed config/YAML file creates a tree of `Entry` objects via `from_dict()` -> `inner()` (at `parsr/query/__init__.py:954-965`). These `Entry` nodes hold lists of children (`:86`, `:88`), name/value/attributes (`:74`, `:81`), and compiled query functions via `to_pyfunc` (`:745`). They are attached to the insights component result objects held by the broker and never explicitly freed.

**Top own-bytes sites within this component**:
```
6.00 MB  inner at parsr/query/__init__.py:965       (recursive tree builder)
5.01 MB  __init__ at parsr/query/__init__.py:86      (Entry children list)
4.00 MB  __init__ at parsr/query/__init__.py:88      (Entry attributes)
3.67 MB  __init__ at parsr/query/__init__.py:74      (Entry name/value)
2.00 MB  __init__ at parsr/query/__init__.py:81      (Entry metadata)
```

**Call path (per-request)**:
```
process_with_insights_core (processor_service.py:160)
  -> run_components (dr.py:1090)
    -> parse_content (ccx_ocp_core/utils/mixins.py:22)
      -> add_query (mixins.py:27)
        -> from_dict (parsr/query/__init__.py:968)
          -> inner:954 (recursive, builds full Entry tree)
```

**Proposed fix**: After extracting rule results from `run_components()`, explicitly clear the broker/context state:

```python
results = broker.get(...)  # extract what you need
broker.clear()             # release Entry trees and parsed content
gc.collect()               # force cycle collection on Entry tree references
```

If `insights.core.dr` caches results globally (not per-broker), investigate whether `dr.CACHE` or `dr.RESULTS` needs manual cleanup. An alternative is to run `run_components()` in a subprocess and let the OS reclaim all memory.

---

### Finding 4: Tracebacks + AST Caret Anchors + linecache

**Leaked: ~6.0 MB combined**

**What happens**: When rule evaluation fails (`insights/core/plugins.py:115`), `traceback.format_exc()` captures the full traceback string. Python 3.12's enhanced error reporting calls `ast.parse()` to generate caret anchors (the `^^^` under the error line) — the AST nodes are never freed.

Separately, `linecache.updatecache()` caches source lines of every file that appeared in any traceback. Over 90 minutes of processing, this accumulates ~1 MB of cached source code that is never evicted.

The `ContentException` at `plugins.py:705` stores the full formatted traceback string (3 MB own bytes).

**Call path**:
```
plugins.py:115 (invoke, exception handler)
  -> traceback.format_exc:184
    -> format_exception:140
      -> format:986
        -> _extract_caret_anchors_from_line_segment:593
          -> ast.parse:52 — 3.00 MB OWN
plugins.py:705 (__init__, ContentException)
  -> plugins.py:449 (__init__) — 3.00 MB OWN (stored traceback string)
```

**Proposed fix**:
1. Don't store full `traceback.format_exc()` strings in exception objects — store just the exception type and message:
   ```python
   # Instead of:
   self.traceback = traceback.format_exc()
   # Use:
   self.traceback = f"{type(e).__name__}: {e}"
   ```
2. Clear linecache after each archive processing cycle:
   ```python
   import linecache
   linecache.clearcache()
   ```
3. If full tracebacks are needed for debugging, log them immediately rather than storing them in the object.

---

### Finding 5: SQLAlchemy ORM Metadata Growth

**Leaked: 18.8 MB (7.9%) — 4,677 allocations**

**What happens**: SQLAlchemy accumulates compiled SQL statements, path registry entries, and function metadata in internal caches. These are ORM-level caches that grow with each unique query pattern encountered and are never evicted.

**Top sites**:
```
1.00 MB  <module> at sqlalchemy/sql/functions.py:1964
1.00 MB  <module> at sqlalchemy/orm/path_registry.py:767
1.00 MB  create_for_statement at sqlalchemy/orm/context.py:282
```

**Proposed fix**:
- Ensure every `Session` is properly closed after use (use context managers or `try/finally`).
- Set `pool_recycle` on `create_engine()` to cycle connections periodically.
- Call `session.expire_all()` after commits to release identity map entries.
- If sessions are created per-request, verify they are closed even on error paths (use FastAPI dependency injection with `yield`).
- Check for ORM queries built with dynamic string interpolation — each unique SQL string creates a new compiled cache entry.

---

### Finding 6: spec_factory File Content and libmagic Buffers

**Leaked: ~8.5 MB combined**

**What happens**: `spec_factory.py` (`load` at lines 296 and 303) reads file contents from extracted archives into memory. These content strings are held by `ContentProvider` objects that outlive the request. Separately, `insights/contrib/magic.py:172` loads the libmagic shared library and its magic database (6.5 MB), which is loaded once and never unloaded.

**Call path**:
```
_handle_content (insights/core/__init__.py:89)
  -> content (spec_factory.py:126, property)
    -> load (spec_factory.py:296) — 2.00 MB OWN

magic.py:172 (load, ctypes.cdll.LoadLibrary) — 6.48 MB OWN
```

**Proposed fix**:
- For spec_factory: cleaning up the broker (as described in Finding 3) addresses this, since `ContentProvider` objects are held by the broker.
- For libmagic: this is a one-time fixed cost (6.5 MB), loaded once when the library is first used. This is acceptable overhead and does not grow over time. No action needed unless you want to skip file-type detection entirely.

---

## Additional Observations

### YAML Parsing (6.3 MB)
YAML parser objects (`yaml/reader`, `yaml/scanner`, `yaml/composer`) retain internal buffers. These are freed when the broker is cleared (same fix as Finding 3).

### dateutil Timezone Data (3.6 MB)
`dateutil.tz._read_tzfile` caches timezone transition data. This is a one-time cost and does not grow. No action needed.

### ccx-rules-ocp Rule Modules (~0.5 MB)
Individual rule module `<module>` allocations are tiny (< 0.01 MB each) but appear across 50+ rule files. These are module-level constants and class definitions — they are normal and not a leak concern.

---

## Prioritized Fix Recommendations

| Priority | Fix | Expected Savings | Effort | Where to Change |
|----------|-----|------------------|--------|-----------------|
| 1 | Cache ProductLifecycle as singleton | ~29 MB per instance | Low | `ccx_ocp_core/models/product_lifecycle.py` |
| 2 | Clear broker after `run_components()` + `gc.collect()` | ~36 MB per cycle | Low | `app/services/processor_service.py` |
| 3 | Import rule modules once at startup | ~78 MB growth | Medium | `ccx_ocp_core/core/plugins.py`, app startup |
| 4 | Truncate stored tracebacks + clear linecache | ~6 MB | Low | `insights/core/plugins.py`, `processor_service.py` |
| 5 | Ensure SQLAlchemy sessions are properly closed | ~19 MB growth | Medium | DB session management in app |
| 6 | Clean up spec_factory ContentProviders (via broker.clear) | ~5 MB per cycle | Low | Covered by fix #2 |

**Quick wins** (Fixes 1, 2, 4) are small code changes that together address ~71 MB of leaks.

**Medium effort** (Fixes 3, 5) require understanding the component discovery and DB session lifecycle but address ~97 MB of leaks.

---

## How to Verify Fixes

After applying fixes, re-run the memray profiler with the same workload:

```bash
# Run with memray attached
memray run --aggregate -o memray-profile-fixed.bin <app_command>

# Generate leaks flamegraph
memray flamegraph --leaks -o flamegraph-leaks-fixed.html memray-profile-fixed.bin

# Compare stats
memray stats memray-profile-fixed.bin
```

Use `--aggregate` flag to reduce the .bin file size (the current 532 MB file took a long time to process).

**Success criteria**: VmRSS growth rate should drop from ~288 MB/hr to < 50 MB/hr. The leaks flamegraph should show significantly reduced never-freed allocations, particularly in the `json.decoder`, `parsr/query`, and `importlib` categories.

---

## Appendix: Generated Reports

The following interactive HTML reports were generated from the profile and can be opened in a browser:

- `/tmp/memray-flamegraph-leaks.html` — Shows only leaked (never-freed) allocations
- `/tmp/memray-flamegraph-temporal.html` — Shows memory usage over time
- `/tmp/memray-flamegraph-peak.html` — Shows memory composition at peak usage
- `/tmp/memray-table.html` — Sortable table of all allocation sites
