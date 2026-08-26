#!/usr/bin/env python3
"""Standalone archive processor for debugging memory and rule-hit behavior.

Fully self-contained — only requires insights-core, pyyaml, and the standard
library. No app.* imports, no database, no HTTP server.

Usage:
    python scripts/process_archive.py archive.tar.gz
    python scripts/process_archive.py archive.tar.gz --count 20 -v
    python scripts/process_archive.py archive.tar.gz --output /tmp/results.json
    python scripts/process_archive.py archive.tar.gz --count 10 --config /path/to/config.yml
"""

import argparse
import gc
import json
import logging
import os
import resource
import sys
from dataclasses import dataclass, field, fields as dc_fields
from io import StringIO
from pathlib import Path

import yaml
from insights import apply_configs, apply_default_enabled, dr
from insights.core.archives import extract
from insights.core.hydration import initialize_broker
from insights.formats.text import HumanReadableFormat

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class _Config:
    format: str = "insights.formats._json.JsonFormat"
    target_components: list = field(default_factory=list)
    temp_upload_dir: str = "/tmp/insights-uploads"
    extract_timeout_seconds: int = 300
    unpacked_archive_size_limit: int = -1
    plugin_packages: list = field(default_factory=list)
    plugin_configs: list = field(default_factory=list)


def _load_config(config_path: str) -> _Config:
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    plugins = raw.get("plugins", {})
    service = raw.get("service", {})
    valid = {f.name for f in dc_fields(_Config)}
    relevant = {k: v for k, v in service.items() if k in valid}
    return _Config(
        **relevant,
        plugin_packages=plugins.get("packages", []),
        plugin_configs=plugins.get("configs", []),
    )


def _load_components(config: _Config) -> None:
    for package in config.plugin_packages:
        logger.info(f"Loading package: {package}")
        dr.load_components(package, continue_on_error=False)
    plugins = {"packages": config.plugin_packages, "configs": config.plugin_configs}
    apply_default_enabled(plugins)
    apply_configs(plugins)


# ---------------------------------------------------------------------------
# Processing (inlined from ProcessorService, DB methods excluded)
# ---------------------------------------------------------------------------

def _fd_snapshot() -> dict:
    fd_dir = f"/proc/{os.getpid()}/fd"
    counts = {"total": 0, "socket": 0, "pipe": 0, "tmp": 0, "file": 0, "other": 0}
    tmp_paths = []
    try:
        for name in os.listdir(fd_dir):
            counts["total"] += 1
            try:
                target = os.readlink(f"{fd_dir}/{name}")
            except OSError:
                continue
            if target.startswith("socket:"):
                counts["socket"] += 1
            elif target.startswith("pipe:"):
                counts["pipe"] += 1
            elif target.startswith("/tmp"):
                counts["tmp"] += 1
                tmp_paths.append(target)
            elif target.startswith("/"):
                counts["file"] += 1
            else:
                counts["other"] += 1
    except OSError:
        pass
    counts["tmp_paths"] = tmp_paths
    return counts


def _validate_size(extraction_path: str, limit: int) -> bool:
    if limit < 0:
        return True
    total_size = sum(p.stat().st_size for p in Path(extraction_path).rglob("*"))
    if total_size >= limit:
        logger.warning(f"Unpacked archive exceeds limit: {total_size} >= {limit}")
        return False
    return True


def _get_cluster_id(extraction_path: str) -> str:
    id_file_path = os.path.join(extraction_path, "config", "id")
    if os.path.exists(id_file_path):
        try:
            with open(id_file_path) as f:
                cluster_id = f.read().strip()
                if cluster_id:
                    return cluster_id
        except Exception as e:
            raise RuntimeError(f"Failed to read config/id: {e}") from e
    raise RuntimeError("Could not find cluster ID. Missing config/id file in archive.")


def _get_component_graphs(target_components: list) -> dict:
    graph = {}
    tc = tuple(target_components or [])
    if tc:
        for c in dr.DELEGATES:
            if dr.get_name(c).startswith(tc):
                graph.update(dr.get_dependency_graph(c))
    return graph


def _process_with_insights_core(
    archive_path: str,
    config: _Config,
    Formatter,
    target_components,
    components_dict,
) -> tuple:
    logger.info(f"Processing archive: {archive_path}")
    os.makedirs(config.temp_upload_dir, exist_ok=True)
    with extract(
        archive_path,
        timeout=config.extract_timeout_seconds,
        extract_dir=config.temp_upload_dir,
    ) as extraction:
        tmp_dir = extraction.tmp_dir
        if not _validate_size(tmp_dir, config.unpacked_archive_size_limit):
            raise RuntimeError(
                f"Archive exceeds size limit: {config.unpacked_archive_size_limit}"
            )

        cluster_id = _get_cluster_id(tmp_dir)
        logger.info(f"Processing cluster: {cluster_id}")

        ctx, broker = initialize_broker(tmp_dir)
        ctx.all_files = []

        try:
            output = StringIO()
            with Formatter(broker, stream=output):
                dr.run_components(target_components, components_dict, broker=broker)
            output.seek(0)
            result = output.read()
            logger.info(f"Processing completed for cluster {cluster_id}")
            return cluster_id, result
        finally:
            if hasattr(broker, "cleanup"):
                broker.cleanup()
            del broker, ctx


# ---------------------------------------------------------------------------
# Memory / heap helpers
# ---------------------------------------------------------------------------

def _rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024  # kB -> MB
    except OSError:
        pass
    r = resource.getrusage(resource.RUSAGE_SELF)
    if sys.platform == "darwin":
        return r.ru_maxrss / (1024 * 1024)
    return r.ru_maxrss / 1024


def _trim_heap() -> None:
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _extract_rule_count(results_json: str) -> int:
    try:
        data = json.loads(results_json)
        return sum(1 for r in data.get("reports", []) if r.get("type") == "rule")
    except (json.JSONDecodeError, TypeError):
        return -1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    default_config = Path(__file__).resolve().parent.parent / "config.yml"

    parser = argparse.ArgumentParser(
        description="Process an insights archive directly (no DB, no HTTP server)"
    )
    parser.add_argument("archive", help="Path to .tar / .tar.gz / .tgz archive")
    parser.add_argument(
        "--config",
        default=str(default_config),
        help=f"Path to config.yml (default: {default_config})",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        metavar="N",
        help="Process the archive N times (default: 1; useful for memory leak testing)",
    )
    parser.add_argument(
        "--show-results",
        action="store_true",
        help="Print raw JSON results to stdout (or --output FILE). Default: suppressed.",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Write raw JSON results to FILE (implies --show-results). Only the last iteration is written.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Set log level to DEBUG",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not args.verbose:
        logging.getLogger("insights.core.dr").setLevel(logging.WARNING)

    if not os.path.exists(args.archive):
        print(f"ERROR: archive not found: {args.archive}", file=sys.stderr)
        sys.exit(1)

    config = _load_config(args.config)
    _load_components(config)

    Formatter = dr.get_component(config.format) or HumanReadableFormat
    if config.target_components:
        components_dict = _get_component_graphs(config.target_components)
    else:
        components_dict = dr.determine_components(dr.COMPONENTS[dr.GROUPS.single])
    target_components = dr.toposort_flatten(components_dict, sort=False)

    rss_start = _rss_mb()
    print(f"RSS at start: {rss_start:.1f} MB", file=sys.stderr)

    last_results_json = None

    for i in range(1, args.count + 1):
        rss_before = _rss_mb()
        fds_before = _fd_snapshot()

        cluster_id, results_json = _process_with_insights_core(
            args.archive, config, Formatter, target_components, components_dict
        )
        rule_count = _extract_rule_count(results_json)
        last_results_json = results_json

        rss_after = _rss_mb()
        fds_after = _fd_snapshot()

        _trim_heap()

        print(
            f"[{i}/{args.count}]"
            f"  cluster={cluster_id}"
            f"  rules={rule_count}"
            f"  rss_baseline={rss_start:.1f} MB"
            f"  rss_before={rss_before:.1f} MB"
            f"  rss_after={rss_after:.1f} MB"
            f"  delta={rss_after - rss_before:+.1f} MB"
            f"  total_growth={rss_after - rss_start:+.1f} MB"
            f"  fds={fds_after['total']} (delta={fds_after['total'] - fds_before['total']:+d})",
            file=sys.stderr,
        )

    if (args.show_results or args.output) and last_results_json is not None:
        if args.output:
            Path(args.output).write_text(last_results_json)
            print(f"Results written to {args.output}", file=sys.stderr)
        else:
            print(last_results_json)


if __name__ == "__main__":
    main()
