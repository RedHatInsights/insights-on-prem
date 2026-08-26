# YAML Parsing — Fragmentation Impact and Fix

## What it was doing

`insights/core/__init__.py` defined a Python subclass of `CSafeLoader` to work around
a PyYAML bug where `=` is treated as a merge key instead of a plain string:

```python
class _PatchedSafeLoader(SafeLoader):          # SafeLoader = CSafeLoader
    yaml_implicit_resolvers = SafeLoader.yaml_implicit_resolvers.copy()
    yaml_implicit_resolvers.pop("=", None)
```

This class was used as the loader for every `yaml.load()` call in `YAMLParser.parse_content()`,
which runs once per YAML file per uploaded archive.

---

## Why subclassing CSafeLoader is a problem

PyYAML's C extension (`_yaml` / `CSafeLoader`) has two layers:
- **C scanner/tokenizer** — always runs in C regardless of subclassing
- **C constructor** — only runs in C when the loader class IS `CSafeLoader` directly

When you subclass `CSafeLoader` in Python, the C constructor fast path is disabled.
Python's MRO dispatch takes over for every YAML node construction: each scalar, dict
key, list entry, and mapping generates a Python function call and a new Python object.

For a typical OCP must-gather YAML file (clusterversions, namespaces, etc.), this
produced approximately **28,900 Python object allocations per parse**.

---

## Memory fragmentation impact

From the memray profile (`monitoring_20260819_155415`):

| Metric | Value |
|---|---|
| Allocations from `yaml/constructor.py:49` | **28,900 per request** |
| Total yaml module allocation | 5.8 MB / 29,518 allocations |
| Allocation size | < 119 bytes (small objects) |

These 28,900 small objects are the interleaving layer that fragments the heap:
they are allocated between larger allocations (path strings, archive buffers)
and have shorter lifetimes, leaving holes when freed that the larger allocations
cannot reclaim.

After removing libmagic (the dominant large-allocation source), yaml becomes the
primary driver of the large-vs-small interleaving pattern.

---

## Fix

Patch `yaml_implicit_resolvers` directly on `SafeLoader` at import time instead of
subclassing. This keeps the `=` fix while leaving `CSafeLoader` as-is so the C
constructor fast path stays active:

```python
# insights/core/__init__.py — after the try/except SafeLoader import

SafeLoader.yaml_implicit_resolvers = {
    k: v for k, v in SafeLoader.yaml_implicit_resolvers.items() if k != "="
}

# Alias for test-shim compatibility (tests/__init__.py swaps this to
# plain SafeLoader for coverage instrumentation compatibility).
_PatchedSafeLoader = SafeLoader
```

The `_PatchedSafeLoader` class definition is removed entirely. `parse_content` still
references `_PatchedSafeLoader` by name, so the existing test mechanism
(`insights.core._PatchedSafeLoader = SafeLoader`) keeps working without any changes
to test files.

---

## Test compatibility

The test infrastructure relies on being able to swap the loader at runtime:

| File | What it does |
|---|---|
| `insights/tests/__init__.py` | Sets `insights.core._PatchedSafeLoader = SafeLoader` (pure Python) so YAML parsing works under coverage instrumentation — CSafeLoader is incompatible with coverage |
| `insights/tests/test_yaml_parser.py` | Temporarily restores `_PatchedSafeLoader` to verify `=` parses as a plain string |

Both continue to work unchanged because `_PatchedSafeLoader` is still a module-level
name in `insights.core` — it is just a direct alias for `SafeLoader` instead of a
subclass.

---

## Files changed

| File | Change |
|---|---|
| `insights/core/__init__.py` | Patch `SafeLoader.yaml_implicit_resolvers` at import; remove `_PatchedSafeLoader` class; add `_PatchedSafeLoader = SafeLoader` alias |

No changes to test files, `parse_content`, or any callers.

---

## Expected outcome

The ~28,900 per-request Python allocations from YAML token construction are eliminated
from the Python heap (handled internally by the C extension instead). This removes
the primary source of small-object interleaving fragmentation after the libmagic fix.
