"""Dependency injection providers for FastAPI."""
from typing import Dict, Optional

from fastapi import Depends

from app.config import get_settings
from app.services.content_service import ContentService
from app.services.report_service import ReportService
from app.services.processor_service import ProcessorService
from app.services.upload_service import UploadService
from app.services.config_loader import load_insights_config, load_insights_components


class ServiceRegistry:
    """Registry for singleton service instances."""

    def __init__(self):
        self._content_service: Optional[ContentService] = None
        self._processor_config: Optional[Dict] = None
        self._report_service: Optional[ReportService] = None
        self._processor_service: Optional[ProcessorService] = None
        self._upload_service: Optional[UploadService] = None

    def get_content_service(self) -> ContentService:
        """
        Get or create ContentService singleton.

        :return: ContentService instance
        """
        if self._content_service is None:
            self._content_service = ContentService()
        return self._content_service

    def get_processor_config(self, config_path: str = "config.yml") -> Dict:
        """
        Get or create processor configuration singleton.

        :param config_path: Path to config file
        :return: Configuration dictionary
        """
        if self._processor_config is None:
            self._processor_config = load_insights_config(config_path)
            load_insights_components(self._processor_config)
        return self._processor_config

    def get_report_service(self) -> ReportService:
        """
        Get or create ReportService singleton.

        :return: ReportService instance
        """
        if self._report_service is None:
            content_service = self.get_content_service()
            self._report_service = ReportService(content_service)
        return self._report_service

    def get_processor_service(self) -> ProcessorService:
        """
        Get or create ProcessorService singleton.

        :return: ProcessorService instance
        """
        if self._processor_service is None:
            config = self.get_processor_config()
            self._processor_service = ProcessorService(config)
        return self._processor_service

    def get_upload_service(self) -> UploadService:
        """
        Get or create UploadService singleton.

        :return: UploadService instance
        """
        if self._upload_service is None:
            processor_service = self.get_processor_service()
            settings = get_settings()
            self._upload_service = UploadService(processor_service, settings)
        return self._upload_service


# Global registry instance
registry = ServiceRegistry()
