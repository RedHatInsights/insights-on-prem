# Memory Fragmentation in Long-Running Python Applications

**Date:** 2026-08-10
**Context:** insights-on-prem archive processing service (FastAPI + uvicorn, Python 3.12)

---

## The Problem

After fixing all per-request memory leaks (confirmed by memray — container growth
dropped from +54 MB to +2.4 MB), the application's RSS still didn't drop after
processing stopped. The container peaked at ~499 MB and stayed there despite
`gc.collect()` + `malloc_trim()` being called after each archive.

This is **memory fragmentation**, not a memory leak.

---

## How Python's Memory Allocator Works

Python uses a three-layer allocation system:

```
Layer 3:  Python objects (dicts, lists, Entry trees, etc.)
            |
Layer 2:  pymalloc arena allocator (Python internal)
            - Allocates 256 KB "arenas" from the OS
            - Sub-divides into 4 KB "pools"
            - Pools are further divided into fixed-size "blocks" (8, 16, 24, ... 512 bytes)
            - Only handles allocations <= 512 bytes
            |
Layer 1:  System allocator (glibc malloc / jemalloc)
            - Handles allocations > 512 bytes
            - Manages the heap via brk()/mmap()
            |
Layer 0:  OS kernel (virtual memory pages)
```

### Why RSS doesn't shrink

1. **pymalloc arena pinning:** A 256 KB arena can only be returned to the OS when
   ALL objects in it are freed. If even one small object (an interned string, a
   cached int, a module-level variable) survives in an arena, the entire 256 KB
   stays allocated. After processing thousands of archives, arenas are scattered
   with surviving objects — this is fragmentation.

2. **glibc heap fragmentation:** glibc's `malloc` uses a contiguous heap that grows
   via `brk()`. It can only shrink the heap from the top — if a single allocation
   sits near the top of the heap, everything below it stays mapped. `malloc_trim()`
   helps but can't fix this structural limitation.

3. **RSS is a high-water mark:** The OS reports RSS (Resident Set Size) as the
   amount of physical memory currently mapped. Python's allocators hold freed
   memory for reuse rather than returning it, so RSS only grows during peak
   allocation and never drops back.

---

## Solutions Applied

### 1. `PYTHONMALLOC=malloc` — Bypass pymalloc

**What it does:** Forces Python to use the system allocator (glibc malloc or
jemalloc) directly for ALL allocations, bypassing the pymalloc arena layer.

**Why it helps:** Eliminates the arena pinning problem entirely. When a Python
object is freed, the memory goes directly back to the system allocator, which
has better mechanisms for returning pages to the OS.

**Trade-off:** ~10-20% slower for workloads dominated by small object allocation/
deallocation (creating/destroying many small dicts, strings, tuples). For I/O-bound
workloads like archive processing, the impact is negligible.

```dockerfile
ENV PYTHONMALLOC=malloc
```

### 2. `LD_PRELOAD=libjemalloc.so.2` — Replace glibc malloc with jemalloc

**What it does:** Replaces glibc's `malloc`/`free`/`realloc` with jemalloc, a
modern allocator designed for multi-threaded, long-running applications.

**Why it helps:** jemalloc has several advantages over glibc malloc:
- **Thread-local caches:** Reduces lock contention in multi-threaded apps
- **Dirty page purging:** Background thread returns freed pages to the OS
  automatically (configurable decay time)
- **Better fragmentation resistance:** Uses size-class segregation and
  extent-based allocation that fragments less than glibc's bins
- **madvise(MADV_DONTNEED):** Actively tells the kernel to reclaim pages

Combined with `PYTHONMALLOC=malloc`, this means: Python objects → jemalloc →
OS pages returned automatically.

```dockerfile
RUN microdnf install --nodocs -y jemalloc
ENV LD_PRELOAD=/usr/lib64/libjemalloc.so.2
```

### 3. `MALLOC_CONF` — jemalloc tuning

**What it does:** Configures jemalloc's behavior for aggressive memory return.

```dockerfile
ENV MALLOC_CONF="background_thread:true,dirty_decay_ms:1000,muzzy_decay_ms:1000"
```

- **`background_thread:true`** — Enables a background thread that purges freed
  pages without waiting for the next allocation call. Without this, purging only
  happens lazily during `malloc()`/`free()` calls.

- **`dirty_decay_ms:1000`** — Freed ("dirty") pages are returned to the OS after
  1 second (default: 10 seconds). Lower values mean faster RSS drop after a burst
  of processing.

- **`muzzy_decay_ms:1000`** — "Muzzy" pages (freed but still mapped with
  `MADV_FREE`) are fully unmapped after 1 second. These pages show up in RSS
  on some kernels until fully reclaimed.

### 4. `MALLOC_TRIM_THRESHOLD_` — glibc trim threshold

**What it does:** Controls when glibc's allocator automatically calls
`malloc_trim()` to shrink the heap. Only effective when glibc malloc is used
(fallback if jemalloc is not loaded).

```dockerfile
ENV MALLOC_TRIM_THRESHOLD_=65536
```

Default is 128 KB. Lowering to 64 KB makes glibc trim the heap more aggressively
after freeing memory. Each `free()` that results in more than 64 KB of free space
at the top of the heap triggers an automatic trim.

### 5. `MALLOC_MMAP_THRESHOLD_` — mmap for large allocations

**What it does:** Allocations above this threshold use `mmap()` instead of the
heap. When freed, `mmap`'d memory is immediately returned to the OS via
`munmap()` — no fragmentation possible.

```dockerfile
ENV MALLOC_MMAP_THRESHOLD_=65536
```

Default is 128 KB. Lowering to 64 KB means more allocations (JSON parsing buffers,
Entry tree arrays, archive content) go through `mmap` and are fully returned to
the OS when freed.

**Trade-off:** `mmap`/`munmap` syscalls are slower than heap allocation. Only
matters for workloads that rapidly allocate and free 64 KB+ buffers in a tight loop.

### 6. `PYTHONDONTWRITEBYTECODE=1` — No .pyc file writes

```dockerfile
ENV PYTHONDONTWRITEBYTECODE=1
```

Prevents Python from writing `.pyc` bytecode cache files to the filesystem.
In containers the filesystem is ephemeral and `.pyc` writes waste I/O and
disk space. The bytecode is compiled once at import time and stays in memory
regardless.

### 7. `PYTHONHASHSEED=0` — Deterministic hashing

```dockerfile
ENV PYTHONHASHSEED=0
```

By default, Python randomizes hash seeds on each process start (security against
hash collision attacks). This means dict/set internal table sizes and resize
patterns vary between restarts, creating different fragmentation profiles each
time — making memory behavior harder to reproduce and debug.

Setting it to 0 makes hashing deterministic, so memory patterns are consistent
across restarts. This aids in profiling and ensures that if memory behavior is
good in testing, it will be the same in production.

**Security note:** Only disable hash randomization if the application doesn't
process untrusted dict keys from external input in hot paths. For archive
processing where the key sets are well-known (YAML/JSON field names), this is safe.

---

## Other Techniques (Not Yet Applied)

### Subprocess worker model

Process each archive in a child process that exits after completion. When the
process exits, the OS reclaims 100% of its memory — no fragmentation possible.

The `subprocess_worker.py` already exists in the codebase for this purpose.
Trade-off: process fork/exec overhead (~50-100ms per archive) and no shared
in-memory state between archives (components must be re-imported each time,
or use a pre-forked worker pool).

### GC threshold tuning

```python
import gc
gc.set_threshold(700, 10, 5)
```

Lower thresholds for generation 1 and 2 collection. Default is `(700, 10, 10)`.
More frequent gen-2 collection helps break circular references sooner, reducing
the window where pinned arenas hold fragmented memory.

### `tracemalloc` for debugging

```python
import tracemalloc
tracemalloc.start(10)  # 10-frame deep traces
# ... process archives ...
snapshot = tracemalloc.take_snapshot()
for stat in snapshot.statistics('lineno')[:20]:
    print(stat)
```

Built-in Python tool for tracking memory allocations by source line. Lighter
weight than memray for quick checks, but higher overhead than production
monitoring.

### Memory-mapped file I/O

For large file processing (archive extraction, JSON parsing), using `mmap` to
map files into memory instead of reading into Python buffers. The OS manages
the memory pages directly and reclaims them when the mapping is closed.

---

## Dockerfile Configuration Summary

```dockerfile
ENV HOME=/app \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONMALLOC=malloc \
    LD_PRELOAD=/usr/lib64/libjemalloc.so.2 \
    MALLOC_CONF="background_thread:true,dirty_decay_ms:1000,muzzy_decay_ms:1000" \
    MALLOC_TRIM_THRESHOLD_=65536 \
    MALLOC_MMAP_THRESHOLD_=65536

RUN microdnf install --nodocs -y sqlite postgresql-devel git jemalloc && \
    ...
```

### How the layers interact

```
Python object freed
  |
  v
PYTHONMALLOC=malloc  -->  skip pymalloc, go directly to system allocator
  |
  v
LD_PRELOAD=libjemalloc  -->  jemalloc handles the free()
  |
  v
MALLOC_CONF dirty_decay_ms=1000  -->  background thread purges page after 1s
  |
  v
OS kernel reclaims physical page  -->  RSS drops
```

Without these settings:

```
Python object freed
  |
  v
pymalloc  -->  object freed within 256KB arena, but arena stays allocated
              (other live objects pin it)
  |
  v
RSS never drops  -->  arena fragmentation
```

---

## Expected Impact

| Scenario | Before (pymalloc + glibc) | After (malloc + jemalloc) |
|----------|--------------------------|--------------------------|
| Peak RSS during processing | ~500 MB | ~500 MB (same peak) |
| RSS after processing stops | ~500 MB (stays at peak) | Should drop toward baseline (~330 MB) |
| RSS after cool-down | No change | Drops within 1-2 seconds (dirty_decay_ms) |
| Per-request overhead | Baseline | +5-15% for small object churn |
| Memory return to OS | Only via malloc_trim() | Automatic via jemalloc background thread |

The combination of `PYTHONMALLOC=malloc` + jemalloc should allow RSS to drop
back toward the baseline (~330 MB) after processing stops, instead of staying
pinned at the ~500 MB high-water mark.
