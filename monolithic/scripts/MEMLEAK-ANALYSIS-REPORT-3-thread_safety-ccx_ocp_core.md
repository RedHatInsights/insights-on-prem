# Thread-Safety Analysis of ccx-ocp-core

**Date:** 2026-08-05
**Scope:** ccx-ocp-core + insights-core (>=3.6.3) shared mutable state audit
**Verdict:** Not safe for in-process threading. Use multiprocessing instead.

---

## Executive Summary

The ccx-ocp-core codebase cannot safely run multiple analyses in parallel within a single Python process using threads. The problems exist at multiple layers:

1. **insights-core** — the underlying framework maintains 13+ module-level mutable registries with zero synchronization
2. **ccx-ocp-core** — several critical shared mutable state patterns (global singletons, class-level sets, unsafe caches)
3. **Architecture** — parser/rule registration via decorators is inherently global-stateful

The recommended path for parallelism is **multiprocessing** (`concurrent.futures.ProcessPoolExecutor`), which gives each worker its own copy of all module globals.

---

## Layer 1: insights-core Is Fundamentally Single-Threaded

insights-core's dependency resolver (`insights.core.dr`) maintains module-level mutable dictionaries and sets with no locking:

| Global Variable | Type | Role |
|-----------------|------|------|
| `COMPONENTS` | `defaultdict(defaultdict(set))` | Component registry |
| `DELEGATES` | `dict` | Component type delegates |
| `DEPENDENCIES` | `defaultdict(set)` | Dependency graph |
| `DEPENDENTS` | `defaultdict(set)` | Reverse dependency graph |
| `COMPONENTS_BY_TYPE` | `defaultdict(set)` | Type-indexed components |
| `COMPONENTS_BY_NAME` | `KeyPassingDefaultDict` | Name-indexed components |
| `COMPONENT_IMPORT_CACHE` | `KeyPassingDefaultDict` | Import cache |
| `MODULE_NAMES` | `dict` | Component-to-module mapping |
| `BASE_MODULE_NAMES` | `dict` | Component-to-base-module mapping |
| `TYPE_OBSERVERS` | `defaultdict(set)` | Observer callbacks by type |
| `ENABLED` | `defaultdict(lambda: True)` | Enabled/disabled flags |
| `HIDDEN` | `set` | Hidden components |
| `IGNORE` | `defaultdict(set)` | Skip-trigger dependencies |

All parser/combiner/rule registrations via `@parser`, `@combiner`, `@rule` decorators mutate these globals at **import time**. There are no locks, mutexes, or thread-local storage anywhere in the module.

Additionally, `insights.core.filters` has its own unprotected global state:

| Global Variable | Type | Mutators |
|-----------------|------|----------|
| `FILTERS` | `defaultdict(dict)` | `add_filter()`, `loads()` |
| `_CACHE` | `dict` | `add_filter()` (deletion), `get_filters()` (population) |

**Consequence:** You cannot safely run two insights-core dependency graphs concurrently in one process. The entire framework assumes a single execution flow.

---

## Layer 2: ccx-ocp-core Shared Mutable State

### CRITICAL Issues

#### 2.1 `POD_LOG_FILTERS` Global Singleton

- **Location:** `ccx_ocp_core/filters.py:200`
- **Type:** Module-level `PodLogFilters()` instance wrapping a mutable `pd.DataFrame`
- **Mutation site:** `ccx_ocp_core/parser_factory/_pod_logs.py:278` — `POD_LOG_FILTERS.add(...)`
- **Read site:** `ccx_ocp_core/providers.py:76` — `_get_filters()` method

The `add()` method (line 104-138) performs an unprotected read-modify-write:

```python
self._filters = pd.concat([self._filters, pd.DataFrame([new_row])], ignore_index=True)
```

Race condition: two threads calling `add()` simultaneously both read the same `self._filters`, both concat independently, and the second write overwrites the first — losing one entry.

The `reset()` method (line 63-83) and `get()` method (line 140-179) also access `self._filters` without synchronization, creating additional TOCTOU windows.

#### 2.2 Non-Thread-Safe `Singleton` Metaclass

- **Location:** `ccx_ocp_core/utils/misc.py:55-63`

```python
class Singleton(type):
    _instance = None

    def __call__(self, *args, **kwargs):
        if self._instance is None:                          # Thread A checks
            self._instance = super().__call__(*args, **kwargs)  # Thread B also checks before A writes
        return self._instance
```

Classic check-then-act race. Two threads can both pass the `is None` check and create separate instances. Only the last assignment survives, and the first caller may hold a reference to a different instance.

#### 2.3 Class-Level Mutable `filters` Sets

- **Location:** `ccx_ocp_core/parsers/must_gather/core.py:50` — `EventListsMG.filters = set()`
- **Location:** `ccx_ocp_core/parsers/must_gather_cnv/core.py:35` — `EventListsMGCNV.filters = set()`

Both are mutated via `add_filter()` class methods:

```python
cls.filters |= patterns  # core.py:73
```

The `|=` on a set is not atomic. Concurrent calls can lose updates or corrupt the set during iteration.

#### 2.4 Unprotected `@functools.cache`

- **Location:** `ccx_ocp_core/models/product_lifecycle.py:19-22`

```python
@functools.cache
def _load_packaged_data():
    raw = resources.files("ccx_ocp_core.data").joinpath("product_lifecycle.json").read_bytes()
    return _parse_raw_data(raw)
```

`functools.cache` uses an internal `dict` that is **not thread-safe**. Concurrent first calls can race on populating the cache, potentially corrupting internal state or producing duplicate computations with inconsistent storage.

### MODERATE Issues

#### 2.5 Mutable Default Argument

- **Location:** `ccx_ocp_core/context.py:304`

```python
def __init__(self, root={}, timeout=None, all_files=None):
```

The `root={}` default is shared across all calls that don't pass `root`. This is a bug regardless of threading, but concurrent access makes mutation of the shared dict observable across threads.

#### 2.6 `PodLogProvider` Lazy Init Without Synchronization

- **Location:** `ccx_ocp_core/providers.py` — `content` property

```python
if self._content is None:
    self._content = self._load()
```

Unsynchronized double-check pattern. Two threads can both enter the block and both call `self._load()`, wasting resources. If `_load()` has side effects, the duplication is observable.

### LOW Issues

#### 2.7 `QueryMixin` State Management

- **Location:** `ccx_ocp_core/utils/mixins.py:6-67`

`QueryMixinDict` and `QueryMixinList` store parsed `self.data` and `self.q` attributes. The `cleanup()` method sets these to `None`. If a parser instance were shared across threads (unlikely in normal use, but possible), concurrent access and cleanup would race.

---

## Layer 3: Architectural Constraint

The broker pattern (each "run" gets its own broker instance passed via `__call__(self, broker)`) is per-invocation and safe in isolation. **However**, the broker relies on the global DR registries (`COMPONENTS`, `DEPENDENCIES`, `DELEGATES`, etc.) to resolve its dependency graph. Even with separate brokers per thread, all threads read and write the same module-level registries.

The `@parser`/`@rule`/`@combiner` decorator registration model is inherently global-stateful — registration happens at import time by mutating `dr.COMPONENTS`, `dr.DEPENDENCIES`, etc. There is no mechanism to scope registrations to a thread or execution context.

---

## GIL Considerations

The CPython GIL does **not** make these patterns safe:

- `pd.concat` + reassignment is not atomic — the GIL can release between computing the concat result and storing it
- `dict.update` on `defaultdict` with custom factories involves multiple bytecode operations
- `set |=` involves reading, computing the union, and writing back — not a single bytecode operation
- Any operation that calls into C extensions (pandas, NumPy) may release the GIL during computation

---

## Recommendations

### Preferred: Use Multiprocessing

```python
from concurrent.futures import ProcessPoolExecutor

with ProcessPoolExecutor(max_workers=N) as pool:
    results = pool.map(analyze_archive, archive_paths)
```

Each process gets its own copy of all module globals, completely avoiding all shared state issues. This is the only approach that works without modifying insights-core.

### If Threading Is Required

The following changes would be needed (in priority order):

1. **Cannot fix upstream:** insights-core's DR registries and filters are out of scope. This alone blocks safe threading.
2. **`POD_LOG_FILTERS`:** Wrap all `add()`, `get()`, `reset()`, and `__iter__()` with `threading.Lock`
3. **`Singleton` metaclass:** Use `threading.Lock` in `__call__` (double-checked locking with lock)
4. **`EventListsMG.filters` / `EventListsMGCNV.filters`:** Replace `set |=` with lock-protected update, or use an immutable pattern (rebuild frozenset on each add)
5. **`@functools.cache`:** Replace with `@functools.lru_cache` + explicit `threading.Lock` guard, or pre-warm the cache before spawning threads
6. **`MustGatherContext.__init__`:** Change `root={}` to `root=None` with `root = root or {}` inside the method
7. **`PodLogProvider.content`:** Add `threading.Lock` around the lazy init

### Alternative: Pre-Warm + Read-Only Threading

If all registration (imports, `add_filter()`, `POD_LOG_FILTERS.add()`) completes before threads are spawned, and threads only **read** the registries without mutation, threading may be safe in practice — but this relies on implementation details of CPython dict/set reads and is not guaranteed by the language specification.

---

## Summary Table

| Issue | Location | Severity | Fixable in ccx-ocp-core? |
|-------|----------|----------|--------------------------|
| DR global registries (13+ dicts/sets) | `insights.core.dr` | CRITICAL | No (upstream) |
| insights-core `FILTERS` / `_CACHE` | `insights.core.filters` | CRITICAL | No (upstream) |
| `POD_LOG_FILTERS` DataFrame | `filters.py:200` | CRITICAL | Yes |
| `Singleton` metaclass | `utils/misc.py:55` | CRITICAL | Yes |
| `EventListsMG.filters` set | `parsers/must_gather/core.py:50` | CRITICAL | Yes |
| `EventListsMGCNV.filters` set | `parsers/must_gather_cnv/core.py:35` | CRITICAL | Yes |
| `@functools.cache` | `models/product_lifecycle.py:19` | CRITICAL | Yes |
| Mutable default `root={}` | `context.py:304` | MODERATE | Yes |
| `PodLogProvider` lazy init | `providers.py` | MODERATE | Yes |
| `QueryMixin` state | `utils/mixins.py` | LOW | Yes |

---

## Appendix: FastAPI / insights-on-prem Monolithic Service Analysis

The `insights-on-prem/monolithic` service runs ccx-ocp-core analysis inside a FastAPI application. This section assesses whether the current architecture is thread-safe and what risks exist.

### Current Architecture

```
uvicorn (single worker, ASGI)
  |
  +-- FastAPI async event loop (handles HTTP requests)
  |
  +-- ThreadPoolExecutor(max_workers=1)
  |     |
  |     +-- _process_in_background()  -->  ProcessorService.process_with_insights_core()
  |           |
  |           +-- extract(archive)
  |           +-- initialize_broker(tmp_dir)
  |           +-- dr.run_components(target_components, components_dict, broker=broker)
  |
  +-- asyncio.to_thread()  (used for Thanos queries, shutdown drain)
```

**Key files:**
- `app/main.py:58-110` — lifespan: creates services, executor, task tracker
- `app/main.py:73` — `load_insights_components(config)` — populates DR registries at startup
- `app/services/upload_service.py:164` — `executor.submit(_process_in_background, ...)`
- `app/services/processor_service.py:127-174` — `process_with_insights_core()` runs DR in-process
- `app/config_loader.py:52-72` — `load_insights_components()` calls `dr.load_components()`

### Why `max_workers=1` Makes It Currently Safe

With `ThreadPoolExecutor(max_workers=1)`, only one background thread ever runs `dr.run_components()` at a time. Combined with the fact that component registration (`load_insights_components`) completes during startup **before** any requests are served (lifespan line 73, before `yield` on line 93), the execution pattern is:

1. **Startup (main thread):** `dr.load_components()` populates all DR globals, `POD_LOG_FILTERS.add()` runs for all rules — all registration completes
2. **Runtime (single background thread):** `dr.run_components()` reads the registries (now stable) and creates per-invocation brokers
3. **No concurrent writes** to DR globals, `POD_LOG_FILTERS`, `EventListsMG.filters`, etc. after startup

This is effectively the "Pre-Warm + Read-Only Threading" pattern described in the recommendations, enforced by the single-worker constraint.

### What Breaks If `max_workers` Is Increased

Raising `max_workers` above 1 would allow concurrent `dr.run_components()` calls, which is **unsafe** because:

1. **`dr.run_components()` mutates broker state** — each call creates its own broker, but some parsers/combiners store intermediate results on class instances or call into code that mutates module-level state (e.g., `insights.core.filters._CACHE`)
2. **`insights.core.filters.get_filters()` populates `_CACHE`** — a module-level dict mutated during execution, not just at import time. Two concurrent `dr.run_components()` calls would race on `_CACHE` reads/writes
3. **`functools.cache` on `_load_packaged_data()`** — first concurrent call races on cache population
4. **`PodLogProvider.content` lazy init** — per-instance but without synchronization; concurrent runs with the same provider instance would race

### Risks Even With `max_workers=1`

Even with the current single-worker setup, there are latent risks:

| Risk | Description | Severity |
|------|-------------|----------|
| **`asyncio.to_thread()` overlap** | `app/main.py:303` runs Thanos queries via `asyncio.to_thread()`, which uses a *separate* default thread pool. If any of that code path touches insights-core state, it would race with the background worker. Currently safe because Thanos queries don't touch insights-core. | LOW |
| **Memory accumulation** | Running `dr.run_components()` in-process means leaked references from exception tracebacks accumulate in the long-lived process. The docstring of `subprocess_worker.py` explicitly calls this out: *"all memory — including leaked references from exception tracebacks in dr.run_components() — is returned to the OS when the process exits (CCXDEV-16176)"* | MODERATE |
| **Future refactoring risk** | The single-worker constraint is implicit (`max_workers=1` on line 76). No comment or assertion guards this. A future developer could increase it to improve throughput without understanding the thread-safety implications. | HIGH |

### The Unused `subprocess_worker.py`

The codebase contains `app/subprocess_worker.py` — a standalone worker that processes archives in a **separate process**, specifically designed to address memory leaks (CCXDEV-16176). It:

- Runs as `python -m app.subprocess_worker <config_path> <archive_path>`
- Loads insights components in the child process
- Processes one archive, writes JSON result to stdout, and exits
- All memory is reclaimed by the OS on exit

This module exists but is **not wired into the upload flow**. The current flow uses in-process `ProcessorService.process_with_insights_core()` instead.

### Recommendations for the Monolithic Service

1. **Do not increase `max_workers` beyond 1** without addressing all thread-safety issues in both ccx-ocp-core and insights-core. Add a code comment or assertion to guard this.

2. **Switch to `subprocess_worker.py`** for archive processing. This solves both thread safety and memory leaks:
   - Each archive runs in an isolated process with its own DR registry copy
   - Memory is fully reclaimed on process exit
   - Multiple archives can be processed in parallel safely via `ProcessPoolExecutor`
   - The worker already exists and handles the full processing pipeline

3. **If higher throughput is needed**, use `ProcessPoolExecutor` spawning `subprocess_worker.py` (or equivalent) rather than threading:

   ```python
   from concurrent.futures import ProcessPoolExecutor

   executor = ProcessPoolExecutor(max_workers=N)
   executor.submit(subprocess_worker._process_archive, archive_path, config)
   ```

4. **Guard the single-worker constraint** if keeping in-process execution:

   ```python
   MAX_ANALYSIS_WORKERS = 1  # DO NOT increase — insights-core is not thread-safe
   executor = ThreadPoolExecutor(max_workers=MAX_ANALYSIS_WORKERS)
   ```
