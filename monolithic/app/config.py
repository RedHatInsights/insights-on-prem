"""Application configuration."""

import os
from dataclasses import dataclass, field


@dataclass
class AppConfig:
    """Application configuration.

    Values are loaded from config.yml, with environment variables
    overriding the app section settings.
    """

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "insights"
    postgres_user: str = "insights"
    postgres_password: str = "insights"
    max_file_size: int = 104857600
    temp_upload_dir: str = "/tmp/insights-uploads"
    extract_timeout_seconds: int = 300
    format: str = "insights.formats._json.JsonFormat"
    target_components: list[str] = field(default_factory=list)
    unpacked_archive_size_limit: int = -1

    request_report_retention_hours: int = 24
    request_report_cleanup_interval_minutes: int = 60

    thanos_url: str = "https://rbac-query-proxy.open-cluster-management-observability.svc.cluster.local:8443"
    thanos_token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    thanos_sa_cert_path: str = (
        "/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt"
    )
    thanos_query_timeout_seconds: int = 10
    thanos_query_lookback_minutes: int = 60

    plugin_packages: list[str] = field(default_factory=list)
    plugin_configs: list[dict] = field(default_factory=list)

    mtls_enabled: bool = False

    @property
    def database_url(self) -> str:
        """Construct PostgreSQL connection URL from components."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


# Environment variable names mapped to (field_name, type)
_ENV_OVERRIDES = {
    "POSTGRES_HOST": ("postgres_host", str),
    "POSTGRES_PORT": ("postgres_port", int),
    "POSTGRES_DB": ("postgres_db", str),
    "POSTGRES_USER": ("postgres_user", str),
    "POSTGRES_PASSWORD": ("postgres_password", str),
    "MAX_FILE_SIZE": ("max_file_size", int),
    "TEMP_UPLOAD_DIR": ("temp_upload_dir", str),
    "THANOS_URL": ("thanos_url", str),
    "THANOS_TOKEN_PATH": ("thanos_token_path", str),
    "THANOS_SA_CERT_PATH": ("thanos_sa_cert_path", str),
    "THANOS_QUERY_TIMEOUT_SECONDS": ("thanos_query_timeout_seconds", int),
    "THANOS_QUERY_LOOKBACK_MINUTES": ("thanos_query_lookback_minutes", int),
    "REQUEST_REPORT_RETENTION_HOURS": ("request_report_retention_hours", int),
    "REQUEST_REPORT_CLEANUP_INTERVAL_MINUTES": (
        "request_report_cleanup_interval_minutes",
        int,
    ),
    "MTLS_ENABLED": ("mtls_enabled", lambda v: v.lower() in ("true", "1", "yes")),
}


def apply_env_overrides(config: AppConfig) -> None:
    """Override AppConfig fields with values from environment variables."""
    for env_var, (attr, type_fn) in _ENV_OVERRIDES.items():
        val = os.environ.get(env_var)
        if val is not None:
            setattr(config, attr, type_fn(val))
