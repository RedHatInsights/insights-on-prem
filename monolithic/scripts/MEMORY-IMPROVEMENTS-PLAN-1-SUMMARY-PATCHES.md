# Memory Improvements Plan 1 — Applied Patches Summary

**Date**: 2026-08-03

All 9 patches from `MEMORY-IMPROVEMENTS-PLAN-1.md` have been applied across 3 repositories.

---

## insights-core (2 files changed)

### `insights/core/dr.py`

**Patch 1 — `Broker.cleanup()` method** (after `add_exception`, ~line 924):

Added a new method that releases all broker state to prevent memory accumulation between runs:

- Clears `__traceback__` on all stored exception objects to break frame reference chains that keep entire call stacks alive in memory.
- Calls `cleanup()` on broker instances that support it (e.g., parsers using `QueryMixinDict`/`QueryMixinList` from ccx-ocp-core), releasing their Entry trees before dropping the references.
- Clears `self.instances`, `self.exceptions`, `self.tracebacks`, `self.exec_times`, and `self.missing_requirements`.
- Calls `linecache.clearcache()` to release accumulated source lines from tracebacks.
- Calls `BLACKLISTED_SPECS.clear()` to prevent the module-level list from growing indefinitely.

**Patch 2a — `_format_exc_short()` helper** (module level, after imports):

Added a condensed traceback formatter that avoids Python 3.12's `ast.parse()` caret-anchor overhead. Uses `traceback.format_exception_only()` + manually formatted innermost frame instead of `traceback.format_exc()`. Saves ~1 MB per traceback by skipping AST parsing and avoids `linecache.updatecache()` accumulation.

Replaced all 3 `traceback.format_exc()` calls in `run_components()` (lines 1123, 1133, 1138) with `_format_exc_short()`.

### `insights/core/plugins.py`

**Patch 2b — `_format_exc_short()` helper + replacements**:

Added the same condensed traceback helper at module level.

Replaced all 11 `traceback.format_exc()` calls with `_format_exc_short()`:

| Class | Method | Lines affected |
|-------|--------|----------------|
| `PluginType` | `invoke` | 2 calls (ContentException, CalledProcessError) |
| `datasource` | `invoke` | 3 calls (ContentException, CalledProcessError, TimeoutException) |
| `parser` | `invoke` | 6 calls (ContentException x3, CalledProcessError x2, generic Exception) |

---

## ccx-ocp-core (2 files changed)

### `ccx_ocp_core/context.py`

**Patch 3 — Optimized `_get_file_root`** (lines 103-116):

Replaced:
```python
match = pattern.search(f)
return os.path.dirname(match.string[: match.start() + 1]) if match else None
```

With:
```python
match = pattern.search(f)
if match is None:
    return None
idx = match.start()
return f[:f.rindex(os.sep, 0, idx + 1)]
```

This avoids two intermediate string allocations per call (`match.string[:...]` + `os.path.dirname` result). With 6.86M calls per profiling run, this reduces allocator pressure and memory fragmentation.

### `ccx_ocp_core/utils/mixins.py`

**Patch 4 — Added `cleanup()` to `QueryMixinDict` and `QueryMixinList`**:

Both classes received a `cleanup()` method that sets `self.q = None` and `self.data = None`, releasing the `insights.parsr.query.Entry` trees built by `add_query()`. These Entry trees are the #2 leak component (35.8 MB in the profiling run).

The `Broker.cleanup()` method (Patch 1) calls `instance.cleanup()` on all stored instances, so parsers inheriting from these mixins automatically release their Entry trees during broker cleanup.

---

## insights-on-prem (4 files changed + docker-compose)

### `app/services/processor_service.py`

**Patch 5 — Broker cleanup after archive processing**:

Added `import gc` at the top.

After reading results from the StringIO output (line 165), added:
```python
output.close()
broker.cleanup()
del broker, ctx
gc.collect()
```

This is the primary call site that triggers all upstream cleanup (Broker.cleanup -> instance.cleanup -> QueryMixin.cleanup).

### `app/services/upload_service.py`

**Patch 6 — GC after background task**:

Added `import gc` at the top.

After `db.close()` in `_process_in_background`, added `gc.collect()` as a safety net to catch any references that survived the broker cleanup (e.g., held by the Formatter or extract context manager).

### `app/database.py`

**Patch 7 — SQLAlchemy pool_recycle**:

Added `pool_recycle=3600` to the `create_engine()` call. This cycles database connections every hour, preventing long-lived connections from accumulating compiled-query caches and identity map residue in SQLAlchemy's internal structures.

### `app/main.py`

**Patch 8 — Engine disposal on shutdown**:

Added `app.state.engine.dispose()` after the cleanup task cancellation in the lifespan shutdown handler. This explicitly returns all pooled connections and releases associated ORM metadata during graceful shutdown.

### `docker-compose.yml`

**Patch 9 — Volume mounts for patched upstream libs**:

Added volume mounts for the local patched repos so changes can be tested without rebuilding the Docker image:
```yaml
- /Users/mzibrick/Projects/RH-ccx/insights-core/insights:/opt/venv/lib64/python3.12/site-packages/insights
- /Users/mzibrick/Projects/RH-ccx/ccx-ocp-core/ccx_ocp_core:/opt/venv/lib64/python3.12/site-packages/ccx_ocp_core
```

---

## Expected Impact

| Leak source | Before | After | Mechanism |
|-------------|--------|-------|-----------|
| Broker instances (Entry trees, ContentProviders, YAML) | ~36 MB/cycle, accumulating | Freed after each archive | `broker.cleanup()` → `instances.clear()` |
| Traceback strings + AST nodes | ~1 MB per exception, accumulating | ~1 KB per exception | `_format_exc_short()` avoids `ast.parse()` |
| linecache source lines | Growing indefinitely | Cleared each cycle | `linecache.clearcache()` in `broker.cleanup()` |
| Exception frame references | Keeping call stacks alive | Broken each cycle | `exc.__traceback__ = None` |
| BLACKLISTED_SPECS list | Growing indefinitely | Cleared each cycle | `BLACKLISTED_SPECS.clear()` |
| QueryMixin Entry trees | Held until GC | Explicitly released | `QueryMixin.cleanup()` via broker |
| `_get_file_root` temporaries | 6.86M allocs/run | Reduced by ~50% | Eliminated intermediate string copies |
| SQLAlchemy connection caches | Growing indefinitely | Recycled hourly | `pool_recycle=3600` |

**Target**: VmRSS growth rate drops from ~288 MB/hr to < 50 MB/hr.

---

## Verification

```bash
podman-compose down && podman-compose up -d
./run_monitoring.sh --memray --duration 60 --cooldown 30

# After run, generate leaks flamegraph and compare
memray flamegraph --leaks -o flamegraph-leaks-after.html memray-profile.bin
memray stats memray-profile.bin
```
