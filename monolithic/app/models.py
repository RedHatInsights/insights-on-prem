"""Database models for Insights On Premise."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, PrimaryKeyConstraint
from sqlalchemy.dialects.postgresql import VARCHAR, insert
from sqlalchemy.orm import Session

from app.database import Base


class Report(Base):
    """
    Main report table storing cluster insights data.

    Stores one report per cluster.
    """

    __tablename__ = "report"

    cluster = Column(VARCHAR, nullable=False, primary_key=True)
    report = Column(VARCHAR, nullable=False)
    reported_at = Column(DateTime, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)
    gathered_at = Column(DateTime, nullable=True)

    @classmethod
    def upsert(
        cls,
        db: Session,
        cluster: str,
        report: str,
        gathered_at: datetime = None,
    ) -> "Report":
        """
        Insert or update a report atomically using PostgreSQL's ON CONFLICT.

        :param db: Database session
        :param cluster: Cluster identifier
        :param report: Report JSON data
        :param gathered_at: When the report was gathered
        :return: The created or updated Report instance
        """
        now = datetime.utcnow()

        # Prepare insert statement with ON CONFLICT DO UPDATE
        stmt = insert(cls).values(
            cluster=cluster,
            report=report,
            reported_at=now,
            last_checked_at=now,
            gathered_at=gathered_at or now,
        )

        # On conflict, update the report and timestamps
        # Keep reported_at from original insert, update gathered_at if provided
        update_dict = {
            "report": stmt.excluded.report,
            "last_checked_at": stmt.excluded.last_checked_at,
        }
        if gathered_at:
            update_dict["gathered_at"] = stmt.excluded.gathered_at

        stmt = stmt.on_conflict_do_update(
            constraint="report_pkey",
            set_=update_dict,
        )

        # Execute the statement
        db.execute(stmt)
        db.commit()

        # Fetch and return the record
        result = db.query(cls).filter_by(cluster=cluster).one()
        return result


class RuleHit(Base):
    """
    Table storing individual rule violations found in reports.

    Each row represents one rule that was triggered for a cluster.
    """

    __tablename__ = "rule_hit"

    cluster_id = Column(VARCHAR, nullable=False)
    rule_fqdn = Column(VARCHAR, nullable=False)
    error_key = Column(VARCHAR, nullable=False)
    updated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint(
            "cluster_id", "rule_fqdn", "error_key", name="rule_hit_pkey"
        ),
    )

    @classmethod
    def upsert(
        cls,
        db: Session,
        cluster_id: str,
        rule_fqdn: str,
        error_key: str,
    ) -> "RuleHit":
        """
        Insert or update a rule hit atomically using PostgreSQL's ON CONFLICT.

        :param db: Database session
        :param cluster_id: Cluster identifier
        :param rule_fqdn: Fully qualified rule name
        :param error_key: Error key for the rule
        :return: The created or updated RuleHit instance
        """
        now = datetime.utcnow()

        # Prepare insert statement with ON CONFLICT DO UPDATE
        stmt = insert(cls).values(
            cluster_id=cluster_id,
            rule_fqdn=rule_fqdn,
            error_key=error_key,
            updated_at=now,
        )

        # On conflict, just update timestamp
        stmt = stmt.on_conflict_do_update(
            constraint="rule_hit_pkey",
            set_={
                "updated_at": stmt.excluded.updated_at,
            },
        )

        # Execute the statement
        db.execute(stmt)
        db.commit()

        # Fetch and return the record
        result = (
            db.query(cls)
            .filter_by(
                cluster_id=cluster_id,
                rule_fqdn=rule_fqdn,
                error_key=error_key,
            )
            .one()
        )
        return result

    @classmethod
    def delete_for_cluster(cls, db: Session, cluster_id: str) -> int:
        """
        Delete all rule hits for a cluster.

        :param db: Database session
        :param cluster_id: Cluster identifier
        :return: Number of rows deleted
        """
        count = (
            db.query(cls).filter_by(cluster_id=cluster_id).delete()
        )
        db.commit()
        return count


class ReportInfo(Base):
    """
    Table storing metadata about reports.

    Stores version information and other metadata for each cluster report.
    """

    __tablename__ = "report_info"

    cluster_id = Column(VARCHAR, nullable=False, primary_key=True)
    version_info = Column(VARCHAR, nullable=False)

    @classmethod
    def upsert(
        cls, db: Session, cluster_id: str, version_info: str
    ) -> "ReportInfo":
        """
        Insert or update report info atomically using PostgreSQL's ON CONFLICT.

        :param db: Database session
        :param cluster_id: Cluster identifier
        :param version_info: Version information JSON
        :return: The created or updated ReportInfo instance
        """
        # Prepare insert statement with ON CONFLICT DO UPDATE
        stmt = insert(cls).values(
            cluster_id=cluster_id,
            version_info=version_info,
        )

        # On conflict, update version_info
        stmt = stmt.on_conflict_do_update(
            constraint="report_info_pkey",
            set_={"version_info": stmt.excluded.version_info},
        )

        # Execute the statement
        db.execute(stmt)
        db.commit()

        # Fetch and return the record
        result = db.query(cls).filter_by(cluster_id=cluster_id).one()
        return result
