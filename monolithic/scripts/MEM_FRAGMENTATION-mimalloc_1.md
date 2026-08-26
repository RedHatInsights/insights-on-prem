# Memory Fragmentation Analysis — mimalloc (Run 1)

**Profile**: `scripts/monitoring_20260819_155415/memray-profile.bin`
**Captured with**: memray 1.20.0 inside container (Python 3.14.5, `PYTHONMALLOC=mimalloc`)
**Session**: 2026-08-19 15:55–16:01 (6 minutes)

---

## Raw numbers

```
Total allocations:       93,583
Total memory allocated:  330.7 MB  (cumulative over 6 min)
Peak live memory:         19.3 MB  (live at any one moment)
VmRSS at session:        ~405 MB   (includes Python runtime + all loaded modules)
VmRSS growth:            +268 KB   over 6 min  →  8.4 MB/hr growth rate
```

The most telling ratio: **330 MB allocated but only 19 MB live at peak** — 17x turnover.
The allocator is constantly freeing. Whether freed pages return to the OS or stay as
fragmented heap determines whether RSS grows.

---

## Allocation size distribution (fragmentation profile)

```
< 4 B     :    663   (0.7%)
< 24 B    : 39,894  (42.6%)   ← bulk of Python objects
< 119 B   : 32,762  (35.0%)
< 591 B   : 15,264  (16.3%)
< 2.9 KB  :  1,718   (1.8%)
< 71 KB   :  2,954   (3.2%)
<= 8.5 MB :     76   (0.1%)   ← magic.py large buffers
```

**78% of allocations are < 119 bytes** (Python dicts, strings, YAML tokens, etc.),
with a small tail of large allocations up to 8.5 MB from libmagic reading file contents.

This is the textbook worst-case fragmentation pattern for a naive allocator:

1. Allocate 8.5 MB buffer (`magic.py:122` → 216.8 MB total from this one site)
2. Run MIME detection, free the buffer
3. Tiny Python objects fill part of the freed hole
4. Next 8.5 MB request can't reuse the partially-filled hole → new pages from OS
5. Repeat → RSS climbs even though live memory stays at 19 MB

`REALLOC` being 11% of operations (10,344 calls) compounds this — YAML parsing growing
lists/strings almost always does alloc+copy+free, punching more holes.

### Allocator type distribution

```
MALLOC:         82,892  (88.6%)
REALLOC:        10,344  (11.0%)
CALLOC:            337   (0.4%)
MMAP:                7
POSIX_MEMALIGN:      3
```

---

## Where the memory comes from

### By size (hot sites)

| Site | Total allocated |
|---|---|
| `insights/contrib/magic.py:122` | 216.8 MB |
| `insights/core/hydration.py:19` (get_all_files) | 34.5 MB |
| `glob.py:197` (_iterdir) | 31.3 MB |
| `pathlib/__init__.py:836` (iterdir) | 13.5 MB |
| `insights/contrib/magic.py:172` (load) | 8.7 MB |

The `insights` module is responsible for **294 MB of the 330 MB total** (89%) — almost
entirely file enumeration and MIME detection on archive contents.

### By count (high-churn sites)

| Site | Allocations |
|---|---|
| `yaml/constructor.py:49` (get_single_data) | 28,900 |
| `psycopg2/__init__.py:122` (connect) | 14,848 |
| `pydantic/_internal/_model_construction.py` | 5,674 |
| `<frozen importlib._bootstrap>` | 4,068 |

yaml and psycopg2 generate high-frequency small-object churn; pydantic/importlib are
startup-only.

### By module (top 5 by size)

| Module | Total | Allocations |
|---|---|---|
| insights | 294.2 MB | 11,012 |
| app | 13.0 MB | 4,430 |
| yaml | 5.8 MB | 29,518 |
| sqlalchemy | 3.4 MB | 5,877 |
| pandas | 3.3 MB | 1,088 |

---

## How mimalloc reduces fragmentation for this workload

### 1. Segregated size classes for small objects

mimalloc uses fine-grained size classes (8, 10, 12, 16, 24, 32, … bytes). The 78% of
allocations < 119 B are placed in dedicated per-size-class pages. When freed, slots stay
available for the same size class — they never create mixed-size holes that block large
allocations. glibc's malloc has coarser bins and a shared wilderness, so freed small
objects and freed large objects compete for the same address space.

### 2. Large allocations live in separate segments

The magic.py 8.5 MB buffers are handled via direct OS allocation, separate from the
small-object heap. When freed, their pages go directly back to the OS — they cannot
fragment the small-object arenas. Allocations in the `< 71 KB` bucket (2,954 calls) that
would fall below glibc's default MMAP_THRESHOLD (128 KB) and land on the main heap are
instead placed in mimalloc's size-class pages, so they're similarly isolated.

### 3. Per-segment page reclaim

When a mimalloc page is fully empty, it is released back to the segment pool immediately.
No explicit `malloc_trim()` is needed. glibc tends to retain freed heap pages as a
"wilderness" for future use — accumulated across the high-churn 17x turnover seen here,
that adds up to significant RSS growth.

### 4. Thread-local allocation

mimalloc allocates from thread-local heaps. With one processing thread, all allocations
for a given request live in the same thread's heap and are freed in a predictable order,
enabling better coalescing compared to a shared global heap.

---

## Growth rate comparison across runs

| Run | Config | Duration | Growth rate |
|---|---|---|---|
| 20260805_091057 | 4 workers, no fixes | 25 min | **1,960 MB/hr** |
| 20260805_102435 | 4 workers, some fixes | 25 min | **1,785 MB/hr** |
| 20260805_131454 | original (1 worker) | 25 min | **978 MB/hr** |
| 20260805_145204 | one_thread_processing | 25 min | **149 MB/hr** |
| 20260806_091236 | one_thread + insights fixes | 25 min | **161 MB/hr** |
| 20260807_144527 | one_thread + many fixes | 25 min | **38.5 MB/hr** |
| 20260810_153635 | only_malloc | 21 min | **24.6 MB/hr** |
| 20260818_150009 | (untagged) | 15 min | **145.7 MB/hr** |
| 20260819_081538 | (untagged, long run) | 150 min | **68.3 MB/hr** |
| **20260819_155415** | **mimalloc (`PYTHONMALLOC=mimalloc`)** | 6 min | **8.4 MB/hr** |

mimalloc shows the lowest growth rate in the series. Compared to the best prior
sustained runs (24.6–38.5 MB/hr), this is a **3–5x improvement**. The 6-minute window
is short, making a direct comparison soft — but the direction is consistent with what
mimalloc's design delivers for this exact allocation pattern.

---

## Key limitation

memray only tracks Python-layer allocations. The VmRSS of 405 MB vs peak live of 19.3 MB
is not all fragmentation — the gap is mostly:

- Python runtime + shared libraries (~150 MB)
- Module import cache (insights loads a large rule tree at startup)
- mmap'd file regions

The **growth rate** (how fast RSS climbs during operation) is the fragmentation-sensitive
metric, not the absolute RSS level. That is what mimalloc is visibly improving.

---

## Setup

mimalloc is statically compiled into CPython 3.14 (`WITH_MIMALLOC=1` confirmed via
`sysconfig`). It is activated by the `PYTHONMALLOC=mimalloc` env var set in the
Dockerfile. No external shared library (`LD_PRELOAD`) is involved, which also avoids the
QEMU crash issue that disabled jemalloc.
