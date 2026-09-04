# Memory Leak Analysis Report #5 — C Extensions & insights-core Internals

**Date:** 2026-08-06
**Scope:** ccx-ocp-core + insights-core dependency stack
**Focus:** C extensions, native memory allocators, and structural leak sources

---

## 1. C Extension Audit — ccx-ocp-core

ccx-ocp-core has **no direct C extensions** — no `.pyx`, `.pxd`, `.c`, `.cpp`, or `.so` files
in the repository. All code is pure Python.

However, several runtime dependencies use C extensions under the hood:

| Dependency | C Extension | Used In (ccx-ocp-core) |
|---|---|---|
| `cryptography` | Rust/C via `_openssl` bindings | Listed in deps, not heavily imported directly |
| `pandas` | C extensions via NumPy, Cython-compiled internals | `ccx_ocp_core/filters.py` |
| `PyYAML` (`yaml`) | Optional `libyaml` C backend (`CLoader`/`CSafeLoader`) | `context.py`, all YAML parsers |
| `re` (stdlib) | C-implemented `_sre` engine | `context.py` (regex compilation, ~6.8M calls/archive) |
| `urllib3` | Some C-accelerated parts | `context.py` (Retry for k8s API) |

### 1.1 pandas — `filters.py`

`PodLogFilters.add()` (line 138) uses `pd.concat()` to append rows:

```python
self._filters = pd.concat([self._filters, pd.DataFrame([new_row])], ignore_index=True)
```

Each call creates a **new DataFrame** backed by NumPy C arrays and discards the old one.
The C allocator (not CPython's) may not return memory to the OS promptly, causing RSS growth
even when Python sees the old DataFrame as garbage. The global `POD_LOG_FILTERS` instance
(line 200) lives for the entire process lifetime.

### 1.2 PyYAML — YAML parsers

When `libyaml` is available, `yaml.safe_load()` uses the C-based `CSafeLoader`. The C loader
itself doesn't leak, but the Python objects it produces (dicts, lists) are then wrapped in
`Entry` trees (see Section 3), which do leak if not cleaned up.

### 1.3 re (stdlib) — `context.py`

`_glob_to_regex()` (line 100) calls `re.compile()` for each glob pattern. The `re` module
internally caches compiled patterns (bounded at 512 entries). Not a leak concern.

The more significant issue was `_get_file_root()`, which previously created two intermediate
strings per call across ~6.8M calls per archive, causing allocator fragmentation. This was
fixed with the `str.rindex()` approach (lines 118-124).

### 1.4 Kubernetes `ApiClient` — `context.py`

`PenDriveContext._create_api_client()` (line 393) creates a `client.ApiClient` with urllib3
connection pools (C-level socket buffers, SSL/TLS state from `cryptography`). If
`PenDriveContext` instances are created but the `ApiClient` is never explicitly closed, the
connection pool and its native resources stay open.

---

## 2. insights-core Library Summary

### 2.1 No C Extensions

insights-core is **100% pure Python**. The repository contains no `.pyx`, `.pxd`, `.c`, or
`.so` files. `setup.py` and `pyproject.toml` have no `ext_modules` or Cython build
configuration. Any `.so` files in virtualenvs come from dependencies (PyYAML, msgpack, etc.).

### 2.2 Architecture Overview

| Layer | Key Classes | Purpose |
|---|---|---|
| Specs/Providers | `ExecutionContext`, `@fs_root` | Detect archive type, provide file access |
| Parsers | `YAMLParser`, `JSONParser`, `CommandParser` | Parse files into `self.data` |
| Query | `Entry`, `Result`, `from_dict()` | Tree-structured query language over parsed data |
| Dependency Resolver | `dr.py`, `Broker` | Wires components together, caches results |
| Plugins | `@rule`, `@combiner`, `@condition` | Business logic consuming parsed data |

### 2.3 Data Lifecycle

```
Archive files
  → ExecutionContext.handles(files)
  → Broker runs dependency graph
  → Providers load raw file content
  → Parsers build self.data + query.from_dict() → Entry tree
  → Combiners/Rules consume parsed data
  → Results cached in Broker.instances
  → *** broker.cleanup() must be called explicitly ***
```

---

## 3. Key Memory Leak Vectors

### 3.1 Entry Tree Reference Cycles (HIGH RISK)

**File:** `insights/parsr/query/__init__.py`

The `Entry` class uses bidirectional parent↔child references:

```python
# Entry.__slots__: ("_name", "attrs", "children", "parent", "lineno", "src")
# Constructor sets c.parent = self for all children — creating reference cycles
```

`from_dict()` recursively converts entire YAML/JSON dicts into deeply nested `Entry` trees.
There is **no `cleanup()`, `__del__()`, or `__exit__()`** on `Entry` or `Result` classes.
Cycles rely entirely on CPython's cyclic GC.

**Impact:** If any external reference (a parser, a rule result, the Broker cache) holds even
one `Entry` node, the **entire tree** stays in memory.

**Usage sites in ccx-ocp-core:**

| Location | Cleanup? |
|---|---|
| `utils/mixins.py:27` (`QueryMixinDict`) | Yes — `cleanup()` method at line 31 |
| `utils/mixins.py:59` (`QueryMixinList`) | Yes — `cleanup()` method at line 63 |
| `specs/openshift.py:42` | **No** — no `cleanup()` method |
| `models/events.py:79` | **No** — trees built per-event, retained in combiner |
| `models/proxy.py:46` | **No** — tree stored in mapping dict |
| `parsers/must_gather_logging/*.py` | **No** — trees stored in parser results |
| `parsers/must_gather/core.py:238` | **No** — tree returned from method |
| `models/product_lifecycle.py` | **No** — tree built from dict |

### 3.2 Parser Data Retention (MEDIUM RISK)

**File:** `insights/core/__init__.py`

Base parser classes hold parsed data indefinitely:

- **`YAMLParser`**: Sets `self.data = yaml.load(...)` — never clears it
- **`JSONParser`**: Sets `self.data = json.loads(...)` — never clears it
- **`XMLParser`**: Sets `self.data = {}` and populates via `self.parse_dom()`
- **`LegacyItemAccess`** mixin: Assumes persistent `self.data` access

Parser instances persist in the Broker's `self.instances` dict after execution. Each parser
holds the entire parsed data structure in memory.

### 3.3 Global Module-Level Caches (HIGH RISK)

**File:** `insights/core/dr.py`

Six global dicts accumulate component registrations for the **lifetime of the Python process**:

```python
DEPENDENCIES = defaultdict(set)         # dependency graph
DEPENDENTS = defaultdict(set)           # reverse dependency graph
COMPONENTS = defaultdict(...)           # component registry
COMPONENTS_BY_TYPE = defaultdict(set)   # type index
DELEGATES = {}                          # component delegates
COMPONENT_IMPORT_CACHE = ...            # imported components
```

These are **never cleared** between runs. In a long-running service processing many archives,
they grow monotonically.

### 3.4 Broker Instance Cache (HIGH RISK)

**File:** `insights/core/dr.py`

The `Broker` stores every executed component result in `self.instances`. This is the main cache
that holds all parsed data, combiner results, and rule outputs.

The **only cleanup** is an explicit `broker.cleanup()` call, which:
- Clears `self.instances`
- Calls `.cleanup()` on instances that implement it (most don't)
- Clears traceback references and `linecache`

If `broker.cleanup()` is not called between archive processing runs, everything accumulates.

### 3.5 FSRoots Global List

**File:** `insights/core/context.py`

Every `@fs_root`-decorated `ExecutionContext` subclass is appended to a module-level
`FSRoots = []`. This grows with each class registration and is never cleared.

---

## 4. Cleanup Mechanisms Inventory

| Mechanism | Scope | Automatic? | Gaps |
|---|---|---|---|
| `broker.cleanup()` | Clears instance cache, calls instance `.cleanup()` | **No** — explicit call required | Most instances don't implement `.cleanup()` |
| `QueryMixinDict.cleanup()` / `QueryMixinList.cleanup()` | Sets `self.q = None`, `self.data = None` | **No** — explicit call required | Only 2 of ~10 `from_dict()` call sites have this |
| CPython cyclic GC | Collects `Entry` tree cycles | Yes, but delayed | Unreliable under memory pressure; cannot reclaim native allocator memory |
| **Nothing** | Global caches (`COMPONENTS`, `DELEGATES`, etc.) | Never cleared | Grows monotonically over process lifetime |
| **Nothing** | `FSRoots` list | Never cleared | Accumulates `ExecutionContext` subclasses |

---

## 5. Conclusions

1. **No C extensions exist in ccx-ocp-core or insights-core.** All code is pure Python.
   C extensions come only from dependencies (pandas/NumPy, PyYAML/libyaml, cryptography).

2. **The primary leak vector is structural, not native.** `Entry` tree reference cycles,
   parser `self.data` retention, and the Broker instance cache create a chain where parsed
   data lives for the entire processing run (or longer if `broker.cleanup()` isn't called).

3. **pandas DataFrame churn** in `filters.py` causes C-allocator fragmentation but is not a
   true leak — it's RSS bloat from the native allocator not returning memory to the OS.

4. **The `_get_file_root` allocator pressure** (6.8M string allocations per archive) was
   already fixed with the `str.rindex()` approach.

5. **Recommended focus areas for fixes:**
   - Ensure `broker.cleanup()` is called after every archive processing run
   - Add `cleanup()` methods to parsers that use `from_dict()` but lack one
     (especially `specs/openshift.py`, `models/events.py`, `models/proxy.py`)
   - Consider breaking `Entry` parent references in `broker.cleanup()` at the
     insights-core level
   - Replace `pd.concat` append pattern with a list-based accumulator converted to
     DataFrame only when needed
