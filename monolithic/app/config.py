"""Application configuration management."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    Database credentials are loaded from POSTGRES_* environment variables
    which can be populated from Kubernetes secrets.
    """

    # Database settings (loaded from POSTGRES_* env vars)
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "insights"
    postgres_user: str = "insights"
    postgres_password: str = "insights"

    # Application settings
    max_file_size: int = 104857600  # 100MB in bytes
    temp_upload_dir: str = "/tmp/insights-uploads"
    log_level: str = "INFO"

    # API settings
    api_prefix: str = "/api/ingress/v1"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False
    )

    @property
    def database_url(self) -> str:
        """
        Construct database URL from components.

        :return: PostgreSQL connection URL
        """
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
