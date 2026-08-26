# Hydration get_all_files — Fragmentation Impact and Fix

## What it was doing

`insights/core/hydration.py:create_context()` always called `list(get_all_files(path))`
unconditionally as the very first step, before knowing the archive type:

```python
def create_context(path, context=None):
    all_files = list(get_all_files(path))   # always — even for cluster archives

    if context:
        return _create_user_defined_context(path, context, all_files)
    else:
        return _create_autodetected_context(path, all_files)
```

`get_all_files` is a recursive generator over `os.scandir` that yields every file path
in the extracted archive tree. `list()` materialises all of them into memory at once.

---

## Why it was wasted work for cluster archives

`_create_autodetected_context` checks for cluster archives first:

```python
def _create_autodetected_context(path, all_files):
    ctx = _create_cluster_archive_context(path)   # uses only os.listdir
    if ctx:
        return ctx                                 # never touches all_files
    ...
```

`_create_cluster_archive_context` detects a cluster archive using only `os.listdir(path)`
— it looks for `.tar.gz` / `.tar.xz` / `.zip` files in the top-level directory. It does
not use `all_files` at all. For cluster archives it returns immediately, making the
preceding `list(get_all_files(path))` entirely wasted.

OCP must-gather archives (the primary workload) **are** cluster archives. Every upload
paid the full recursive scan cost for nothing.

---

## Memory fragmentation impact

From the memray profile (`monitoring_20260819_155415`):

| Site | Total allocated |
|---|---|
| `hydration.py:19` (os.scandir frame) | 34.5 MB |
| `glob.py:197` (_iterdir) | 31.3 MB |
| `pathlib/__init__.py:836` (iterdir) | 13.5 MB |
| **Total** | **~79 MB** |

All three sites are the same operation — the recursive directory walk and path string
construction inside `get_all_files`. They are attributed to different frames because
`os.scandir`, `glob._iterdir`, and `pathlib.iterdir` each appear in the call stack
depending on the archive layout depth.

The 79 MB of path strings live for the entire duration of request processing and are
freed all at once at the end. Their persistence alongside shorter-lived objects (YAML
parse results, component outputs) creates fragmentation holes when those shorter-lived
objects are freed first.

For cluster archives this entire cost is unnecessary — the path strings are never used.

---

## Fix

Restructure `create_context` to attempt cluster detection before the recursive scan:

```python
def create_context(path, context=None):
    if context:
        # ClusterArchiveContext is detected via os.listdir only — no full recursive
        # scan needed. Avoid the expensive get_all_files traversal for that path.
        all_files = [] if context is ClusterArchiveContext else list(get_all_files(path))
        return _create_user_defined_context(path, context, all_files)

    # Try cluster archive first: uses only os.listdir, not a recursive file walk.
    # For cluster archives (common OCP case) this avoids materialising the full
    # file tree, which can be tens of thousands of path strings.
    ctx = _create_cluster_archive_context(path)
    if ctx:
        return ctx

    all_files = list(get_all_files(path))
    return _create_autodetected_context(path, all_files)
```

`_create_autodetected_context` still calls `_create_cluster_archive_context` internally
(a second `os.listdir`) for non-cluster archives that reach it — a negligible cost
compared to the avoided recursive scan.

---

## Files changed

| File | Change |
|---|---|
| `insights/core/hydration.py` | Reorder `create_context` to try cluster detection before `list(get_all_files(path))` |

---

## Expected outcome

For cluster archives (the common OCP upload case): the ~79 MB of path string
allocations are eliminated entirely. `create_context` returns after a single
`os.listdir` call with no heap impact.

For non-cluster archives: behaviour is identical to before — `list(get_all_files(path))`
runs and `_create_autodetected_context` proceeds as usual.
