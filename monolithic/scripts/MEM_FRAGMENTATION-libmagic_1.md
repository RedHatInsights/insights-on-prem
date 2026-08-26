# libmagic — Fragmentation Impact and Removal

## What it was doing

`insights/util/content_type.py` used the `python-magic` / `libmagic` C library to detect
the MIME type of uploaded archives. The result was used in a single branch in
`insights/core/archives.py:86`:

```python
content_type = content_type_from_file(path)
if content_type == "application/zip":
    extractor = ZipExtractor(...)
else:
    extractor = TarExtractor(...)   # everything else
```

The library was opened with `MAGIC_CONTINUE` flag:

```python
_magic = magic.open(mime_flag | magic.CONTINUE)
```

`MAGIC_CONTINUE` instructs libmagic to decompress the archive and inspect its inner
layers (e.g., peer inside a `.tar.gz` to see the embedded tar). This is what caused
the large buffer allocations.

---

## Memory fragmentation impact

From the memray profile (`monitoring_20260819_155415`):

| Metric | Value |
|---|---|
| Largest single allocation | 8.5 MB |
| Total allocated from `magic.py:122` | **216.8 MB** (single call site) |
| Share of total session allocation | **65.6%** of 330.7 MB |

libmagic was called **once per uploaded archive** at `archives.py:86`. The decompression
buffer it allocated (up to 8.5 MB) was freed after MIME detection, but by then YAML
parsing had placed ~28,900 small Python objects into the heap. The freed large block
became a fragmented hole that could not be reclaimed as a contiguous region.

This repeated on every upload, progressively worsening heap fragmentation and driving
RSS growth even though peak live memory was only 19.3 MB.

---

## Security issues

libmagic is a C library that parses untrusted binary input. Running it with
`MAGIC_CONTINUE` on every upload means attacker-controlled bytes are fed deeply into
a C decompression and format-parsing pipeline. This is a well-established attack
surface:

| CVE | Description |
|---|---|
| CVE-2019-18218 | Stack buffer overflow in `cdf_read_property_info` |
| CVE-2022-48554 | Buffer overflow in `file_copystr` |
| CVE-2017-1000249 | Stack buffer overflow |

The irony: libmagic with `MAGIC_CONTINUE` is more dangerous than not using it, because
it decompresses archives, giving the attacker a much deeper path through the C parser.

---

## Why the check was unnecessary

`upload_service.py` already validates the file extension before the archive ever reaches
`extract()`:

```python
# upload_service.py
endswith(".tar.gz"), ".tgz", ".tar"   # accepted — all route to TarExtractor
```

None of those extensions are zip files. By the time `content_type_from_file(path)` was
called, it was guaranteed to not be a zip archive. The libmagic call was fully redundant
for the app's processing path.

`cluster.py` (a separate CLI tool, not in the app's code path) also called `extract()`
in a per-sub-archive loop, which would have made things worse had it been used.

---

## Replacement

`content_type.py` was rewritten to use a 262-byte header read and fixed magic byte
signatures — no C library, no decompression, no external process:

```python
_MAGIC_SIGNATURES = [
    (b"PK\x03\x04",       0,   "application/zip"),
    (b"PK\x05\x06",       0,   "application/zip"),
    (b"PK\x07\x08",       0,   "application/zip"),
    (b"\x1f\x8b",         0,   "application/gzip"),
    (b"\xfd7zXZ\x00",     0,   "application/x-xz"),
    (b"BZh",              0,   "application/x-bzip2"),
    (b"\x28\xb5\x2f\xfd", 0,   "application/zstd"),
    (b"ustar",            257, "application/x-tar"),
]
_HEADER_BYTES = 262
```

All archive format signatures fit within 262 bytes (gzip: 2 bytes, zip: 4 bytes, xz:
6 bytes, tar POSIX: offset 257 + 5 bytes). Max allocation per call: 262 bytes instead
of 8.5 MB.

---

## Files changed

| File | Change |
|---|---|
| `insights/contrib/magic.py` | **Deleted** |
| `insights/util/content_type.py` | Rewritten — magic bytes check, libmagic removed |
| `insights/client/connection.py` | `_legacy_upload_archive` — replaced 9-line try/except magic block with `from_file()` |
| `insights/tests/client/test_platform.py` | Removed `MockMagic` class, updated `@patch` to target `insights.util.content_type.from_file` |

All files are in `insights-core` which is volume-mounted, so changes take effect on
container restart without rebuilding the image.

---

## Expected outcome

The 216.8 MB hot site at `magic.py:122` is eliminated. Per-upload large-buffer
allocation drops from up to 8.5 MB to 262 bytes, removing the dominant source of
heap fragmentation in the profiled session.
