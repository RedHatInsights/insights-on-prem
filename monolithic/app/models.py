"""Database models for Insights On Premise."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, PrimaryKeyConstraint
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
    def delete_older_than(cls, db: Session, cutoff: datetime) -> int:
        """Delete all reports not checked since the given cutoff."""
        return db.query(cls).filter(cls.last_checked_at < cutoff).delete()

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
        now = datetime.now(timezone.utc)

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
    impacted_since = Column(DateTime, nullable=True)

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
        now = datetime.now(timezone.utc)

        # Prepare insert statement with ON CONFLICT DO UPDATE
        stmt = insert(cls).values(
            cluster_id=cluster_id,
            rule_fqdn=rule_fqdn,
            error_key=error_key,
            updated_at=now,
            impacted_since=now,
        )

        # On conflict, just update updated_at timestamp
        stmt = stmt.on_conflict_do_update(
            constraint="rule_hit_pkey",
            set_={
                "updated_at": stmt.excluded.updated_at,
            },
        )

        # Execute the statement
        db.execute(stmt)

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
    def delete_older_than(cls, db: Session, cutoff: datetime) -> int:
        """Delete all rule hits not updated since the given cutoff."""
        return db.query(cls).filter(cls.updated_at < cutoff).delete()

    @classmethod
    def delete_for_cluster(cls, db: Session, cluster_id: str) -> int:
        """
        Delete all rule hits for a cluster.

        :param db: Database session
        :param cluster_id: Cluster identifier
        :return: Number of rows deleted
        """
        count = db.query(cls).filter_by(cluster_id=cluster_id).delete()
        return count


class RequestReport(Base):
    """
    Table storing simplified reports for on-demand data gathering requests.

    Each row represents a processed request identified by request_id.
    Row presence indicates the request has been processed.
    """

    __tablename__ = "request_report"

    request_id = Column(VARCHAR, nullable=False, primary_key=True)
    cluster_id = Column(VARCHAR, nullable=False)
    report = Column(VARCHAR, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("idx_request_report_created_at", "created_at"),)

    @classmethod
    def create(
        cls,
        db: Session,
        request_id: str,
        cluster_id: str,
        report: str,
    ) -> "RequestReport":
        """
        Create a request report record after successful processing.

        :param db: Database session
        :param request_id: Request identifier from upload
        :param cluster_id: Cluster identifier from archive
        :param report: Simplified report JSON string
        :return: The created RequestReport instance
        """
        record = cls(
            request_id=request_id,
            cluster_id=cluster_id,
            report=report,
            created_at=datetime.now(timezone.utc),
        )
        db.add(record)
        return record

    @classmethod
    def get_by_cluster_and_request(
        cls,
        db: Session,
        cluster_id: str,
        request_id: str,
    ) -> "RequestReport":
        """
        Get a request report by cluster ID and request ID.

        :param db: Database session
        :param cluster_id: Cluster identifier
        :param request_id: Request identifier
        :return: RequestReport instance or None
        """
        return (
            db.query(cls)
            .filter_by(cluster_id=cluster_id, request_id=request_id)
            .first()
        )

    @classmethod
    def delete_older_than(cls, db: Session, cutoff: datetime) -> int:
        """
        Delete all request reports older than the given cutoff.

        :param db: Database session
        :param cutoff: Delete records created before this time
        :return: Number of rows deleted
        """
        return db.query(cls).filter(cls.created_at < cutoff).delete()
