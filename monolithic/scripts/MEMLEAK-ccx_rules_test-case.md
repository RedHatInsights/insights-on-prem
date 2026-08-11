# Memory Leak Analysis Report 4 — memray

## Framework-Level (insights-core DR system) — HIGH impact

### 1. `dr.load_components()` never cleaned up

- **Location:** `tests/conftest.py:47`, `tests/structural/rule_repo.py:54`
- **Issue:** Loads all rule components into global registries (`dr.COMPONENTS_BY_TYPE`, `dr.DELEGATES`). These are never cleared — no `pytest_unconfigure()` or teardown hook exists.

### 2. `run_input_data()` broker accumulation

- **Location:** `tests/rules/integration.py:80-83`
- **Issue:** Called once per parametrized test case (~603 times). Each call creates a new broker holding all resolved component outputs and intermediate results. Cleanup relies entirely on GC — no explicit `broker.close()` or reset between tests.

### 3. `filters.add_filter()` monkey-patch never restored

- **Location:** `tests/conftest.py:66`
- **Issue:** Replaced globally, never restored. The `ADDED_FILTERS` defaultdict accumulates for the entire session.

### 4. Session-scoped `RuleRepoValidator`

- **Location:** `tests/structural/conftest.py:14`
- **Issue:** Holds `ContentManager` + all component references for the entire session. Acknowledged as "heavy" in comments but never cleaned up.

## Rule-Level — MODERATE impact

### 5. Log object retention in `pods_with_unexpected_on_disk_state`

- **Location:** `ccx_rules_ocp/common/mcp_unexpected_on_disk_state.py:115-121`
- **Issue:** The `pods_with_unexpected_on_disk_state` incident stores full `log` parser objects in the result dict. Child incidents (`pods_with_content_mismatch`, `pods_with_mode_mismatch`) do `del simple_pod["log"]` before returning, but the parent incident's result still holds them in the broker's cache for the component's lifetime.

## Rule-Level — LOW / non-issues

- No `@lru_cache` or `functools.cache` usage found anywhere in `ccx_rules_ocp/`
- No `global` mutations (only string constants)
- Module-level regex and `PodLogs` are configuration objects — correct pattern, not leaks
- `defaultdict` usage in rules is local to function scope — cleaned up on return

## Best test case for reproducing a memleak

The unit test suite (`pytest tests/rules`) is the best target, because:

- It runs `run_input_data()` ~603 times, each creating a broker with full DR resolution
- The broker holds all parsed data, component outputs, and exceptions
- No cleanup between runs — if the broker or DR system leaks, it compounds across all 603 test cases

### memray commands

```bash
# Full suite — best for finding cumulative leaks
memray run -o full.bin -m pytest tests/rules -x -q

# Single large rule — isolate per-invocation overhead
memray run -o single.bin -m pytest tests/rules -k "mcp_unexpected_on_disk_state" -x -q

# Generate reports
memray flamegraph full.bin
memray stats full.bin
memray tree full.bin
```

The `mcp_unexpected_on_disk_state` test is a particularly good single-rule target since it's the one case where large parser objects (`log`) are stored in the incident result dict and could survive in the broker cache.
