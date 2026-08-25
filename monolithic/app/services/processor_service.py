"""Insights-core archive processing service."""

import json
import logging
import os
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

# Insights-core imports
from insights import dr
from insights.core.archives import extract
from insights.core.hydration import initialize_broker
from insights.formats.text import HumanReadableFormat
from sqlalchemy.orm import Session

from app.config import AppConfig
from app.exceptions import ProcessingError
from app.models import Report, RequestReport, RuleHit
from app.utils.content import normalize_rule_fqdn

logger = logging.getLogger(__name__)

_fd_log = logging.getLogger(__name__ + ".fds")


def _fd_snapshot() -> dict:
    """Read /proc/self/fd and return counts by type + paths of /tmp handles."""
    fd_dir = f"/proc/{os.getpid()}/fd"
    counts = {"total": 0, "socket": 0, "pipe": 0, "tmp": 0, "file": 0, "other": 0}
    tmp_paths: list[str] = []
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


_fd_baseline: int | None = None


class ProcessorService:
    """
    Service for processing Red Hat Insights archives.
    Refactored from ArchiveProcessor to use dependency injection.
    """

    def __init__(self, config: AppConfig):
        """
        Initialize the processor service.

        :param config: Application configuration
        """
        self.config = config

        # Setup formatter
        self.Formatter = dr.get_component(config.format) or HumanReadableFormat

        # Setup target components
        if config.target_components:
            self.components_dict = self._get_component_graphs(config.target_components)
        else:
            # Use all single-node components if none specified
            self.components_dict = dr.determine_components(
                dr.COMPONENTS[dr.GROUPS.single]
            )

        self.target_components = dr.toposort_flatten(self.components_dict, sort=False)

        # Extraction settings
        self.extract_timeout_seconds = config.extract_timeout_seconds
        self.extract_tmp_dir = config.temp_upload_dir
        self.unpacked_archive_size_limit = config.unpacked_archive_size_limit

        logger.debug(
            f"Processor initialized with {len(self.target_components)} components"
        )

    def _get_component_graphs(self, target_components: list[str]) -> dict:
        """
        Get dependency graphs for target components.

        :param target_components: List of component name prefixes
        :return: Dictionary of component dependency graphs
        """
        graph = {}
        tc = tuple(target_components or [])

        if tc:
            for c in dr.DELEGATES:
                if dr.get_name(c).startswith(tc):
                    graph.update(dr.get_dependency_graph(c))

        return graph

    def _validate_size(self, extraction_path: str) -> bool:
        """
        Validate unpacked archive size.

        :param extraction_path: Path to extracted archive
        :return: True if size is acceptable, False otherwise
        """
        if self.unpacked_archive_size_limit < 0:
            logger.debug("No size limitation for unpacked archive")
            return True

        total_size = sum(p.stat().st_size for p in Path(extraction_path).rglob("*"))

        if total_size >= self.unpacked_archive_size_limit:
            logger.warning(
                f"Unpacked archive exceeds limit: {total_size} >= "
                f"{self.unpacked_archive_size_limit}"
            )
            return False

        return True

    def get_cluster_id(self, extraction_path: str) -> str:
        """
        Extract cluster ID from archive.

        :param extraction_path: Path to extracted archive directory
        :return: Cluster identifier
        :raises ProcessingError: If cluster ID cannot be determined
        """
        import os

        # Get cluster ID from config/id file
        id_file_path = os.path.join(extraction_path, "config", "id")
        if os.path.exists(id_file_path):
            try:
                with open(id_file_path) as f:
                    cluster_id = f.read().strip()
                    if cluster_id:
                        logger.info(f"Found cluster_id in config/id: {cluster_id}")
                        return cluster_id
            except Exception as e:
                logger.error(f"Failed to read config/id: {e}")
                raise ProcessingError(f"Failed to read config/id: {str(e)}") from e

        raise ProcessingError(
            "Could not find cluster ID. Missing config/id file in archive."
        )

    def process_with_insights_core(self, archive_path: str) -> tuple[str, str]:
        """
        Process archive with insights-core.

        :param archive_path: Path to archive file
        :return: Tuple of (cluster_id, results_json)
        :raises ProcessingError: If processing fails
        """
        try:
            logger.info(f"Processing archive: {archive_path}")

            # Use insights.core.archives.extract()
            with extract(
                archive_path,
                timeout=self.extract_timeout_seconds,
                extract_dir=self.extract_tmp_dir,
            ) as extraction:
                # Validate size
                if not self._validate_size(extraction.tmp_dir):
                    raise ProcessingError(
                        f"Archive exceeds size limit: {self.unpacked_archive_size_limit}"
                    )

                # Get cluster ID
                cluster_id = self.get_cluster_id(extraction.tmp_dir)
                logger.info(f"Processing cluster: {cluster_id}")

                # Initialize broker
                ctx, broker = initialize_broker(extraction.tmp_dir)

                try:
                    # Run components with formatter
                    output = StringIO()
                    with self.Formatter(broker, stream=output):
                        dr.run_components(
                            self.target_components, self.components_dict, broker=broker
                        )

                    output.seek(0)
                    result = output.read()

                    logger.info(f"Processing completed for cluster {cluster_id}")
                    logger.debug(f"Result length: {len(result)} chars")

                    return cluster_id, result
                finally:
                    # Break circular references between broker exceptions
                    # and call frames to prevent memory leaks (CCXDEV-16176).
                    # The hasattr guard keeps compatibility with un-patched
                    # insights-core that lacks Broker.cleanup().
                    if hasattr(broker, 'cleanup'):
                        broker.cleanup()

        except Exception as e:
            logger.error(f"insights-core processing failed: {e}", exc_info=True)
            raise ProcessingError(f"Analysis failed: {str(e)}") from e

    def extract_rule_hits(self, results_json: str) -> list[dict]:
        """
        Extract rule hits from insights-core results.

        :param results_json: JSON string from insights-core
        :return: List of rule hit dictionaries
        """
        rule_hits = []

        try:
            if not results_json or results_json == "{}":
                logger.info("No results to parse")
                return rule_hits

            results = json.loads(results_json)
            reports = results.get("reports", [])

            for report in reports:
                if report.get("type") == "rule":
                    component = report.get("component", "")
                    rule_fqdn = component
                    error_key = report.get("key", "UNKNOWN_ERROR")
                    details = report.get("details", {})

                    if rule_fqdn:
                        rule_hits.append(
                            {
                                "rule_fqdn": rule_fqdn,
                                "error_key": error_key,
                                "details": details,
                            }
                        )

            logger.info(f"Extracted {len(rule_hits)} rule hits")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse results JSON: {e}")
        except Exception as e:
            logger.error(f"Error extracting rule hits: {e}", exc_info=True)

        return rule_hits

    def save_results(
        self,
        db: Session,
        cluster_id: str,
        results_json: str,
        request_id: str,
    ) -> int:
        """
        Save processing results to database.

        :param db: Database session
        :param cluster_id: Cluster identifier
        :param results_json: JSON results from insights-core
        :param request_id: Request ID for on-demand gathering tracking
        :return: Number of rule hits saved
        """
        # Extract rule hits from results
        rule_hits = self.extract_rule_hits(results_json)

        try:
            # Save main report
            report_data = {
                "cluster_id": cluster_id,
                "rule_count": len(rule_hits),
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "results": results_json,
            }

            Report.upsert(
                db,
                cluster=cluster_id,
                report=json.dumps(report_data),
                gathered_at=datetime.now(timezone.utc),
            )

            # Replace all rule hits for this cluster atomically
            RuleHit.delete_for_cluster(db, cluster_id)
            for hit in rule_hits:
                RuleHit.upsert(
                    db,
                    cluster_id=cluster_id,
                    rule_fqdn=hit["rule_fqdn"],
                    error_key=hit["error_key"],
                )

            # Save simplified report for on-demand request tracking
            simplified_report = json.dumps(
                [
                    {**hit, "rule_fqdn": normalize_rule_fqdn(hit["rule_fqdn"])}
                    for hit in rule_hits
                ]
            )
            RequestReport.create(
                db,
                request_id=request_id,
                cluster_id=cluster_id,
                report=simplified_report,
            )

            # Commit the transaction
            db.commit()

            logger.info(f"Saved {len(rule_hits)} rule hits for cluster {cluster_id}")
            return len(rule_hits)

        except Exception as e:
            # Rollback on any error
            db.rollback()
            logger.error(
                f"Failed to save results for cluster {cluster_id}: {e}", exc_info=True
            )
            raise ProcessingError(f"Database save failed: {str(e)}") from e

    def process_archive(
        self,
        db: Session,
        archive_path: str,
        request_id: str,
    ) -> tuple[str, int]:
        """
        Main processing function - extract, analyze, and save archive.

        :param db: Database session
        :param archive_path: Path to uploaded archive file
        :param request_id: Request ID for on-demand gathering
        :return: Tuple of (cluster_id, number of rules found)
        :raises ProcessingError: If processing fails at any stage
        """
        logger.info(f"Starting archive processing: {archive_path}")

        global _fd_baseline
        fds_before = _fd_snapshot()
        if _fd_baseline is None:
            _fd_baseline = fds_before["total"]
        drift = fds_before["total"] - _fd_baseline
        _fd_log.debug(
            "FDs before processing: total=%d (drift=%+d) sockets=%d pipes=%d tmp=%d files=%d tmp_paths=%s",
            fds_before["total"], drift,
            fds_before["socket"], fds_before["pipe"],
            fds_before["tmp"], fds_before["file"],
            fds_before["tmp_paths"] if fds_before["tmp_paths"] else "[]",
        )
        if drift > 20:
            _fd_log.warning("FD drift +%d — possible handle accumulation (total=%d)", drift, fds_before["total"])

        # Process with insights-core
        cluster_id, results_json = self.process_with_insights_core(archive_path)

        # Save to database
        rules_count = self.save_results(db, cluster_id, results_json, request_id)

        fds_after = _fd_snapshot()
        leaked = fds_after["total"] - fds_before["total"]
        if leaked != 0:
            _fd_log.warning(
                "FD delta after archive: %+d (before=%d after=%d) — new tmp=%s",
                leaked, fds_before["total"], fds_after["total"],
                [p for p in fds_after["tmp_paths"] if p not in fds_before["tmp_paths"]],
            )
        else:
            _fd_log.debug("FD delta: 0 (total=%d) — no handles left open", fds_after["total"])

        logger.info(f"Completed processing for cluster {cluster_id}")
        return cluster_id, rules_count
