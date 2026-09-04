# Plan: Subprocess-Based insights-core Archive Processing

## Context

The monolithic FastAPI app processes Red Hat Insights archives using insights-core in worker threads within the same process. Despite explicit cleanup (`broker.cleanup()`, `del broker/ctx`, `gc.collect()`), the process leaks ~900MB/hour because insights-core internally caches component graphs, broker state, and analysis data that don't get fully released by Python's garbage collector.

**Goal:** Move the insights-core processing into a subprocess per archive. When the subprocess exits, the OS reclaims ALL memory — leaked or not. The main process handles only HTTP serving, DB operations, and subprocess lifecycle.

## Approach: `subprocess.run` with a Standalone Worker Script

Each archive upload spawns a fresh `python -m app.worker` subprocess that:
1. Reads a JSON config from stdin (archive path, plugin packages, format, timeouts, etc.)
2. Loads insights-core components from scratch
3. Processes the archive (extract, broker, run components, format output)
4. Writes a JSON result to stdout (`{status, cluster_id, results}`)
5. Exits — OS reclaims everything

The main process receives the JSON result and does database operations (save report, rule hits, request report) which use SQLAlchemy sessions that can't cross process boundaries anyway.

**Why not `multiprocessing` or `ProcessPoolExecutor`?**
- `subprocess.run` gives the strongest isolation (separate Python interpreter, no shared state at all)
- No pickling concerns — config and results are simple JSON over stdin/stdout
- Timeout handling is straightforward (`subprocess.run(timeout=...)`)
- The Docker container already has Python at a known path (`sys.executable`)
- The per-archive overhead of loading insights-core (a few seconds) is acceptable given the 900MB/hr leak it eliminates

## File Changes

### 1. CREATE `app/worker.py` — Subprocess Worker

New standalone module runnable as `python -m app.worker`. Self-contained — does not import from the main process modules except `config_loader.load_insights_components` logic (inlined to avoid pulling in FastAPI dependencies).

**Input protocol** (JSON on stdin):
```json
{
  "archive_path": "/tmp/insights-uploads/tmpXXXX.tar.gz",
  "format": "ccx_ocp_core.core.formats.json.OCPRecommendationsJsonFormat",
  "target_components": [],
  "extract_timeout_seconds": 300,
  "temp_dir": "/tmp/insights-uploads",
  "unpacked_archive_size_limit": -1,
  "plugin_packages": ["ccx_rules_ocp.external"],
  "plugin_configs": [{"name": "...", "enabled": false}]
}
```

**Output protocol** (JSON on stdout):
```json
{"status": "ok", "cluster_id": "uuid", "results": "<json-string>"}
```
or on error:
```json
{"status": "error", "error": "message"}
```

Implementation:
- `load_components(packages, configs)` — mirrors `config_loader.load_insights_components()`
- `process(config)` — mirrors the current `ProcessorService.process_with_insights_core()` logic: resolve component graphs, extract archive, initialize broker, run components with formatter, return `(cluster_id, results_json)`
- `main()` — reads stdin, calls `process()`, writes JSON to stdout, exits 0/1
- All logging directed to stderr so stdout stays clean for the JSON protocol
- Add `__main__.py` to `app/` package so `python -m app.worker` works, OR use `if __name__ == "__main__": main()` pattern with `-m app.worker` invocation

### 2. MODIFY `app/services/processor_service.py`

**Remove:**
- All insights-core imports (`from insights import dr`, `extract`, `initialize_broker`, `HumanReadableFormat`)
- `__init__` logic that touches `dr` module (Formatter resolution, component graph computation, `self.Formatter`, `self.components_dict`, `self.target_components`)
- Helper methods `_get_component_graphs()`, `_validate_size()`, `get_cluster_id()` — these move to the worker

**Simplify `__init__`:**
```python
def __init__(self, config: AppConfig):
    self.config = config
    self._working_dir = os.getcwd()
```

**Rewrite `process_with_insights_core(archive_path) -> (cluster_id, results_json)`:**
- Build a JSON config dict from `self.config` fields
- Call `subprocess.run([sys.executable, "-m", "app.worker"], input=config_json, capture_output=True, text=True, timeout=..., cwd=self._working_dir)`
- Total timeout = `extract_timeout_seconds + 120` (buffer for component loading)
- Parse JSON from stdout; raise `ProcessingError` on non-zero exit, timeout, or invalid JSON
- Log worker stderr lines at DEBUG level

**Keep unchanged:**
- `extract_rule_hits(results_json)` — runs in main process, pure JSON parsing
- `save_results(db, cluster_id, results_json, request_id)` — runs in main process, DB operations
- `process_archive(db, archive_path, request_id)` — orchestrates `process_with_insights_core` + `save_results`, interface unchanged

### 3. MODIFY `app/main.py`

- Remove `load_insights_components(config)` call from `lifespan()` (line 79)
- Remove `load_insights_components` from the import on line 26: change to `from app.config_loader import load_config`
- Everything else stays: ProcessorService/UploadService initialization, thread pool config, DB setup

The main process no longer loads insights-core at all, reducing its baseline memory footprint.

### 4. NO CHANGES to `app/services/upload_service.py`

`_process_in_background()` calls `self.processor_service.process_archive()` whose interface is unchanged. The semaphore still limits concurrent processing (now concurrent subprocesses instead of concurrent in-process threads). The `gc.collect()` call becomes less important but is harmless to keep.

### 5. UPDATE Tests

**`tests/test_processor_service.py`:**
- Update `test_init_with_valid_config` and `test_init_with_custom_components` for the simplified `__init__` (no more `extract_timeout_seconds`, `extract_tmp_dir`, `unpacked_archive_size_limit` as direct attributes)
- Remove `test_get_cluster_id_*` tests (method moved to worker)
- Keep `test_extract_rule_hits_*` and `test_save_results_*` unchanged — these methods don't change
- Update `test_process_archive_success` and `test_process_archive_*` to mock `subprocess.run` instead of mocking `extract`/`initialize_broker`/`dr`
- Add tests for subprocess error cases: timeout (`subprocess.TimeoutExpired`), non-zero exit code, invalid JSON output

**`tests/test_upload_service.py`:** No changes — already mocks `processor_service.process_archive()`.

## Verification

1. **Unit tests:** Run `pytest monolithic/tests/` — all existing tests should pass after updates
2. **Docker integration test:** Build container, docker-compose up, send a test archive via `scripts/send_archives.py`, verify:
   - HTTP 202 response
   - Report appears in DB (check via `/api/v2/cluster/{id}/reports`)
   - Main process memory stays flat after processing multiple archives
   - Worker subprocess logs visible in container stderr
3. **Memory leak test:** Run `scripts/reproduce_leak.sh` with the subprocess changes and compare memory growth to the baseline ~900MB/hr monitoring data in `scripts/monitoring_*/`
4. **Timeout handling:** Send a very large or corrupt archive and verify the subprocess is killed after the timeout and `ProcessingError` is raised
