# Plan: Memory Leak Patches — Upstream Libraries + App

## Context

The insights-app leaks ~288 MB/hr. Memray `--leaks` shows 239 MB never-freed, but ~132 MB is one-time startup cost (module loading, ProductLifeCycle `@functools.cache`, libmagic, dateutil). The **growing per-request leaks** are:

- **Broker state** (instances, exceptions, tracebacks) — not cleaned up between archive processing runs
- **`traceback.format_exc()`** — Python 3.12 calls `ast.parse()` for caret anchors (~1 MB per traceback), stored in broker
- **`linecache`** — accumulates source lines from every traceback file, never cleared
- **`BLACKLISTED_SPECS`** — module-level list that grows with every `BlacklistedSpec` exception, never cleared

The fix strategy: add cleanup to insights-core's Broker, reduce traceback overhead in plugins.py, and call cleanup from the app after each archive.

## Repos and Files

| Repo | Path | What to change |
|------|------|----------------|
| insights-core | `insights/core/dr.py` | Add `Broker.cleanup()` method |
| insights-core | `insights/core/plugins.py` | Use lighter traceback format in exception handlers |
| ccx-ocp-core | `ccx_ocp_core/context.py` | Optimize `_get_file_root` to reduce allocations |
| ccx-ocp-core | `ccx_ocp_core/utils/mixins.py` | Add `cleanup()` method to release Entry trees |
| insights-on-prem | `app/services/processor_service.py` | Call `broker.cleanup()` + `gc.collect()` after processing |
| insights-on-prem | `app/services/upload_service.py` | Add `gc.collect()` safety net after background task |
| insights-on-prem | `app/database.py` | Add `pool_recycle=3600` |
| insights-on-prem | `app/main.py` | Add `engine.dispose()` on shutdown |
| insights-on-prem | `docker-compose.yml` | Add volume mounts for patched upstream libs |

---

## Patch 1: `insights-core/insights/core/dr.py` — Add `Broker.cleanup()`

Add a `cleanup()` method to the `Broker` class (after `add_exception`, around line 923).

```python
def cleanup(self):
    """Release all broker state to prevent memory accumulation between runs.

    Clears exception __traceback__ references (which keep entire call
    frames alive), then drops all instances, exceptions, tracebacks,
    and timing data.  Also clears the stdlib linecache, which
    accumulates source lines from every file that appeared in any
    traceback.
    """
    import linecache

    for exc_list in self.exceptions.values():
        for exc in exc_list:
            exc.__traceback__ = None
    self.instances.clear()
    self.exceptions.clear()
    self.tracebacks.clear()
    self.exec_times.clear()
    self.missing_requirements.clear()
    linecache.clearcache()
    BLACKLISTED_SPECS.clear()
```

This is the highest-impact single change — it releases ALL per-request memory held by the broker (Entry trees, ContentProviders, parsed YAML, traceback strings, exception frame references).

---

## Patch 2: `insights-core/insights/core/plugins.py` — Lighter traceback storage

Replace all `traceback.format_exc()` calls with a helper that stores only the condensed exception info (type + message + last frame), avoiding the `ast.parse()` caret-anchor overhead.

Add at the top of the file (after existing imports):

```python
def _format_exc_short():
    """Return a condensed traceback: last frame + exception, without caret anchors."""
    import sys
    exc_type, exc_value, exc_tb = sys.exc_info()
    try:
        parts = traceback.format_exception_only(exc_type, exc_value)
        if exc_tb is not None:
            # Include only the innermost frame for context
            frames = traceback.extract_tb(exc_tb)
            if frames:
                last = frames[-1]
                parts.insert(0, f"  File \"{last.filename}\", line {last.lineno}, in {last.name}\n")
        return "".join(parts)
    finally:
        del exc_tb
```

Then replace all `traceback.format_exc()` calls in the file with `_format_exc_short()`:

- `PluginType.invoke` (lines 72, 76)
- `datasource.invoke` (lines 115, 121, 127)
- `parser.invoke` (lines 169, 173, 187, 194, 199, 204)

This avoids `ast.parse()` on every exception (saves ~1 MB per traceback in Python 3.12) and avoids `linecache.updatecache()` accumulation.

---

## Patch 3: `ccx-ocp-core/ccx_ocp_core/context.py` — Optimize `_get_file_root`

This function is the #1 allocator by count (6.86M calls). Each call creates a regex match object and a string slice via `match.string[:match.start() + 1]`. Optimize by avoiding the intermediate string slice and using `os.path.dirname` on the original string directly via `f[:match.start()]` then finding the last separator.

Replace `_get_file_root` (lines 103-116):

```python
def _get_file_root(f, pattern):
    """Get the root of a marker file."""
    match = pattern.search(f)
    if match is None:
        return None
    # Extract the directory part before the matched marker, avoiding
    # an intermediate string copy via match.string[...].
    idx = match.start()
    return f[:f.rindex(os.sep, 0, idx + 1)]
```

This avoids `os.path.dirname(match.string[:match.start() + 1])` which creates two intermediate strings per call. With 6.86M calls, this saves millions of temporary allocations and reduces allocator pressure.

---

## Patch 4: `ccx-ocp-core/ccx_ocp_core/utils/mixins.py` — Add `cleanup()` to release Entry trees

The `add_query()` method builds an `Entry` tree stored as `self.q`. These trees are the #2 leak component (35.8 MB). Adding a `cleanup()` method allows the broker cleanup to release them.

Add to `QueryMixinDict`:

```python
def cleanup(self):
    """Release the query Entry tree to free memory."""
    self.q = None
    if hasattr(self, "data"):
        self.data = None
```

Add the same to `QueryMixinList`:

```python
def cleanup(self):
    """Release the query Entry tree to free memory."""
    self.q = None
    if hasattr(self, "data"):
        self.data = None
```

Then in `insights-core/insights/core/dr.py` `Broker.cleanup()`, iterate over instances and call cleanup if available:

Add before `self.instances.clear()`:

```python
for instance in self.instances.values():
    if hasattr(instance, 'cleanup') and callable(instance.cleanup):
        try:
            instance.cleanup()
        except Exception:
            pass
```

---

## Patch 5: `app/services/processor_service.py` — Call broker cleanup

Add imports at the top:
```python
import gc
```

After `result = output.read()` (line 165) and before `return cluster_id, result` (line 170), insert:

```python
output.close()
broker.cleanup()
del broker, ctx
gc.collect()
```

---

## Patch 6: `app/services/upload_service.py` — GC after background task

Add import at the top:
```python
import gc
```

In `_process_in_background`, after `db.close()` (line 131), add:
```python
gc.collect()
```

This is a safety net that catches references that survived the broker cleanup (e.g., held by the Formatter or extract context manager).

---

## Patch 7: `app/database.py` — Add pool_recycle

Add `pool_recycle=3600` to the `create_engine` call to prevent connections (and their associated SQLAlchemy compiled-query caches) from living indefinitely:

```python
engine = create_engine(
    database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_recycle=3600,
)
```

---

## Patch 8: `app/main.py` — Dispose engine on shutdown

After the `cleanup_task.cancel()` block (after line 105), add:

```python
app.state.engine.dispose()
logger.info("Database engine disposed")
```

---

## Patch 9: `docker-compose.yml` — Mount patched libs for testing

Add volume mounts for the local upstream repos so patches can be tested without rebuilding the image:

```yaml
volumes:
  - ./app:/app/app
  - ./migrations:/app/migrations
  - ./tests:/app/tests
  - ./config.yml:/app/config.yml
  - temp_uploads:/tmp/insights-uploads
  # Patched upstream libraries for memory leak testing
  - /Users/mzibrick/Projects/RH-ccx/insights-core/insights:/opt/venv/lib64/python3.12/site-packages/insights
  - /Users/mzibrick/Projects/RH-ccx/ccx-ocp-core/ccx_ocp_core:/opt/venv/lib64/python3.12/site-packages/ccx_ocp_core
```

---

## Verification

After applying all patches:

```bash
# Restart with patched libs
podman-compose down && podman-compose up -d

# Run the memray profiler reproducer for 60 min + 30 min cooldown
./run_monitoring.sh --memray --duration 60 --cooldown 30
```

**Success criteria:**
- VmRSS growth rate drops from ~288 MB/hr to < 50 MB/hr
- Memory should plateau or decrease during the 30-min cooldown
- Leaks flamegraph shows significantly reduced never-freed allocations in parsr/query, traceback, and linecache categories
