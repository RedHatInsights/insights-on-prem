# Memory investigation: open file handles

## Hypothesis

Open file descriptors accumulate across archive processing calls and are never
closed, contributing to the observed linear memory growth (~1.32 MB/min, R²=0.98
over 32 min in run `monitoring_20260824_214543`).

File handles keep their underlying pages pinned in RSS — the kernel cannot reclaim
a page mapped by an open fd even if no Python object references the buffer. A
file-handle leak would therefore look identical to a memory leak in `podman stats`
and `/proc/PID/status` VmRSS, and would be invisible to `gc.collect()` or
`malloc_trim()`.

## Why this matters before blaming fragmentation

The growth profile (perfectly linear, R²=0.98, no deceleration) rules out pure
heap fragmentation. Fragmentation grows fast at first then levels off as the heap
reaches a steady shape. A straight line over 32+ minutes means something
accumulates proportionally to every archive processed — either Python objects or
open handles.

`gc.collect()` and `malloc_trim()` cannot touch either:
- open file handles are reference-counted by the kernel, not CPython
- `malloc_trim` only releases free space at the top of the brk heap

## Candidates inside the processing path

```
upload_service._process_in_background()
  └── processor_service.process_archive()
        ├── process_with_insights_core()
        │     ├── extract(archive_path, ...)        # context manager — should close
        │     │     └── tarfile / gzip extraction
        │     ├── initialize_broker(tmp_dir)         # ctx holds file readers
        │     │     └── HostArchiveContext / specs open files per component
        │     ├── dr.run_components(...)             # components may open files
        │     └── broker.cleanup()                   # clears exception refs only
        │         ctx is NOT explicitly cleaned up  ← suspect
        └── os.remove(temp_file_path)               # archive deleted, but…
              if any component still holds an fd to it, the inode stays live
```

Key code location: `processor_service.py` in `process_with_insights_core()`

```python
ctx, broker = initialize_broker(extraction.tmp_dir)
try:
    ...
    dr.run_components(...)
finally:
    if hasattr(broker, 'cleanup'):
        broker.cleanup()   # broker is cleaned up
    # ctx is never explicitly del'd or cleaned up
```

`broker.cleanup()` was added to fix CCXDEV-16176 (circular exception references).
`ctx` received no equivalent treatment. If `ctx` or any component it owns holds
an open fd and has a circular reference, CPython's reference counter will not close
it until a GC cycle runs — and even then only if no module-level cache retains it.

## Changes made

### 1. `monitor.sh` — FD count CSV added

New file produced per run: `insights-app_fd_count.csv`

```
timestamp,elapsed_min,fd_total,fd_sockets,fd_pipes,fd_tmp,fd_files
```

FDs are read from `/proc/<highest-RSS-PID>/fd/` inside the container via
`podman exec`, broken down by type (socket / pipe / /tmp file / other file).

The status line now includes `fds=N`:

```
[  5 min] insights-app   mem=352 MiB  cpu=91%  disk=233 MB  fds=42
[ 20 min] insights-app   mem=378 MiB  cpu=93%  disk=703 MB  fds=89
```

The summary section warns if fd_total grew by more than 50 over the run.

### 2. `processor_service.py` — per-archive FD logging

A `_fd_snapshot()` helper reads `/proc/self/fd`, counts by type, and collects
the target paths of any `/tmp` handles.

Before each archive: logs total, drift from baseline, and per-type breakdown.
After each archive: logs delta; if non-zero, logs which new `/tmp` paths appeared.

```python
_fd_log = logging.getLogger("app.services.processor_service.fds")
```

Default level: WARNING — silent unless drift > 20 or a per-archive delta ≠ 0.
Set `PYTHONUNBUFFERED=1` and grep app logs during a run:

```bash
podman logs -f insights-app 2>&1 | grep "FD"
```

To enable DEBUG (full snapshot every archive):

```python
# temporary, in main.py lifespan or via env:
logging.getLogger("app.services.processor_service.fds").setLevel(logging.DEBUG)
```

## How to interpret results

### FD count climbs linearly with archives processed

```
fds=42 @ min 0  →  fds=89 @ min 20  →  fds=130 @ min 35
```

Handle leak confirmed. The `tmp_paths` list in the WARNING log will identify
the exact files staying open. Most likely culprits:

- insights-core file readers inside `ctx` not closed when `extract()` exits
- A component that opens a file in `__init__` but only closes in `__del__`
- `tarfile.TarFile` objects held in the broker graph via a circular reference
  (if `broker.cleanup()` does not break all cycles)

Fix: explicitly `del ctx` after `broker.cleanup()`, or call `ctx.cleanup()` if
the method exists. Force a GC cycle with `gc.collect()` immediately after.

### FD count stays flat, memory still grows

File handles are not the cause. The 1.32 MB/min growth is pure Python object
accumulation. Next step: `objgraph.show_growth()` between archives to identify
which type is piling up.

```python
import objgraph
# in _process_in_background, every 100 archives:
if archive_count % 100 == 0:
    objgraph.show_growth(limit=20)
```

### FD count flat during load, spikes then drops

Transient leak inside `extract()` that closes properly when the context manager
exits — not the root cause of sustained growth.

## Signals that confirm a handle leak

From `fd_count.csv`, a handle leak looks like:

| elapsed_min | fd_total | fd_tmp |
|-------------|----------|--------|
| 0           | 38       | 2      |
| 5           | 52       | 16     |
| 10          | 67       | 31     |
| 20          | 98       | 62     |

`fd_tmp` climbing at the same rate as `fd_total` points directly to temp files
from previous archive extractions staying open.

From app logs:

```
WARNING FD drift +31 — possible handle accumulation (total=69)
WARNING FD delta after archive: +1 (before=69 after=70) — new tmp=['/tmp/insights-uploads/tmpABC123.tar']
```

## Related runs

| Run | Duration | End mem | Growth rate | Notes |
|-----|----------|---------|-------------|-------|
| `monitoring_20260824_214543` | 32 min (still processing) | 395 MB | 1.32 MB/min, R²=0.98 | baseline for this investigation |
| `monitoring_20260824_115701-memfrag_no_mimalloc_no_insights_fixes` | 25 min | 384 MB | ~1.8 MB/min | |
| `monitoring_20260824_122616-memfrag-mimalloc` | 25 min | 381 MB | ~1.6 MB/min | mimalloc marginal improvement |
