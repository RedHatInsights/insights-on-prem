"""Dependency injection providers for FastAPI."""
from functools import lru_cache
from typing import Dict

from fastapi import Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.services.content_service import ContentService
from app.services.report_service import ReportService
from app.services.processor_service import ProcessorService
from app.services.upload_service import UploadService
from app.services.config_loader import load_insights_config, load_insights_components


@lru_cache()
def get_content_service() -> ContentService:
    """
    Get or create the content service instance (cached singleton).

    :return: ContentService singleton instance
    """
    return ContentService()


@lru_cache()
def get_report_service(
    content_service: ContentService = Depends(get_content_service),
) -> ReportService:
    """
    Get or create the ReportService instance (cached singleton).

    :param content_service: Content service instance
    :return: ReportService singleton instance
    """
    return ReportService(content_service)


@lru_cache()
def get_processor_config(config_path: str = "config.yml") -> Dict:
    """
    Get or create the processor configuration (cached singleton).

    Loads insights-core configuration and components once at startup.
    The @lru_cache decorator ensures this only runs once.

    :param config_path: Path to config file
    :return: Configuration dictionary
    """
    config = load_insights_config(config_path)
    load_insights_components(config)
    return config


@lru_cache()
def get_processor_service(
    config: Dict = Depends(get_processor_config),
) -> ProcessorService:
    """
    Get or create the ProcessorService instance (cached singleton).

    :param config: Processor configuration
    :return: ProcessorService singleton instance
    """
    return ProcessorService(config)


@lru_cache()
def get_upload_service(
    processor_service: ProcessorService = Depends(get_processor_service),
) -> UploadService:
    """
    Get or create the UploadService instance (cached singleton).

    :param processor_service: Processor service instance
    :return: UploadService singleton instance
    """
    settings = get_settings()
    return UploadService(processor_service, settings)
