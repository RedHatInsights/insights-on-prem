"""Insights-core archive processing service."""
import json
import logging
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Dict, List, Tuple

from sqlalchemy.orm import Session

# Insights-core imports
from insights import dr
from insights.core.archives import extract
from insights.core.hydration import initialize_broker
from insights.formats.text import HumanReadableFormat

from app.config import AppConfig
from app.models import Report, RuleHit
from app.exceptions import ProcessingError

logger = logging.getLogger(__name__)


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

    def _get_component_graphs(self, target_components: List[str]) -> Dict:
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
                f"Unpacked archive exceeds limit: {total_size} >= {self.unpacked_archive_size_limit}"
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
                with open(id_file_path, "r") as f:
                    cluster_id = f.read().strip()
                    if cluster_id:
                        logger.info(f"Found cluster_id in config/id: {cluster_id}")
                        return cluster_id
            except Exception as e:
                logger.error(f"Failed to read config/id: {e}")
                raise ProcessingError(f"Failed to read config/id: {str(e)}")

        raise ProcessingError("Could not find cluster ID. Missing config/id file in archive.")

    def process_with_insights_core(self, archive_path: str) -> Tuple[str, str]:
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

        except Exception as e:
            logger.error(f"insights-core processing failed: {e}", exc_info=True)
            raise ProcessingError(f"Analysis failed: {str(e)}")

    def extract_rule_hits(self, results_json: str) -> List[Dict]:
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
                        rule_hits.append({
                            "rule_fqdn": rule_fqdn,
                            "error_key": error_key,
                            "details": details,
                        })

            logger.info(f"Extracted {len(rule_hits)} rule hits")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse results JSON: {e}")
        except Exception as e:
            logger.error(f"Error extracting rule hits: {e}", exc_info=True)

        return rule_hits

    def save_results(self, db: Session, cluster_id: str, results_json: str) -> int:
        """
        Save processing results to database.

        :param db: Database session
        :param cluster_id: Cluster identifier
        :param results_json: JSON results from insights-core
        :return: Number of rule hits saved
        """
        # Extract rule hits from results
        rule_hits = self.extract_rule_hits(results_json)

        try:
            # Save main report
            report_data = {
                "cluster_id": cluster_id,
                "rule_count": len(rule_hits),
                "processed_at": datetime.utcnow().isoformat(),
                "results": results_json,
            }

            Report.upsert(
                db,
                cluster=cluster_id,
                report=json.dumps(report_data),
                gathered_at=datetime.utcnow(),
            )

            # Upsert new rule hits (preserves impacted_since for existing ones)
            new_keys = set()
            for hit in rule_hits:
                RuleHit.upsert(
                    db,
                    cluster_id=cluster_id,
                    rule_fqdn=hit["rule_fqdn"],
                    error_key=hit["error_key"],
                )
                new_keys.add((hit["rule_fqdn"], hit["error_key"]))

            # Remove rule hits that are no longer firing
            existing_hits = db.query(RuleHit).filter_by(cluster_id=cluster_id).all()
            for existing in existing_hits:
                if (existing.rule_fqdn, existing.error_key) not in new_keys:
                    db.delete(existing)

            # Commit the transaction
            db.commit()

            logger.info(f"Saved {len(rule_hits)} rule hits for cluster {cluster_id}")
            return len(rule_hits)

        except Exception as e:
            # Rollback on any error
            db.rollback()
            logger.error(f"Failed to save results for cluster {cluster_id}: {e}", exc_info=True)
            raise ProcessingError(f"Database save failed: {str(e)}")

    def process_archive(self, db: Session, archive_path: str) -> Tuple[str, int]:
        """
        Main processing function - extract, analyze, and save archive.

        :param db: Database session
        :param archive_path: Path to uploaded archive file
        :return: Tuple of (cluster_id, number of rules found)
        :raises ProcessingError: If processing fails at any stage
        """
        logger.info(f"Starting archive processing: {archive_path}")

        # Process with insights-core
        cluster_id, results_json = self.process_with_insights_core(archive_path)

        # Save to database
        rules_count = self.save_results(db, cluster_id, results_json)

        logger.info(f"Completed processing for cluster {cluster_id}")
        return cluster_id, rules_count
