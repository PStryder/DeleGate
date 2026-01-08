"""
DeleGate Configuration

Environment-based configuration per SPEC-DG-0000.
"""
import os
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, ValidationError


class Settings(BaseSettings):
    """
    DeleGate configuration settings.
    All settings can be overridden via environment variables with DELEGATE_ prefix.
    """
    
    model_config = SettingsConfigDict(
        env_prefix="DELEGATE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Server configuration
    host: str = Field(default="0.0.0.0", description="Server bind address")
    port: int = Field(default=8000, description="Server port")
    debug: bool = Field(default=False, description="Enable debug logging")
    reload: bool = Field(default=False, description="Enable hot reload (dev only)")

    # Instance identification
    instance_id: str = Field(
        default="delegate-1",
        description="Instance identifier for this DeleGate"
    )

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/delegate",
        description="PostgreSQL connection URL"
    )
    sql_echo: bool = Field(default=False, description="Echo SQL queries")

    # Integration URLs
    memorygate_url: str = Field(
        default="http://localhost:8001",
        description="MemoryGate URL for context/templates/receipts"
    )
    memorygate_api_key: str = Field(
        default="",
        description="MemoryGate API key for receipt emission"
    )
    asyncgate_url: str = Field(
        default="http://localhost:8002",
        description="AsyncGate URL for async task execution"
    )

    # Planning limits
    max_plan_steps: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum steps per Plan"
    )
    planning_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Max time for Plan generation"
    )

    # Worker registry
    capability_cache_ttl_seconds: int = Field(
        default=600,
        ge=60,
        description="Worker capability cache TTL"
    )
    worker_health_check_interval_seconds: int = Field(
        default=60,
        ge=10,
        description="Interval for worker health checks"
    )

    # Trust configuration
    default_trust_tier: str = Field(
        default="verified",
        description="Minimum trust tier if not specified (untrusted, sandbox, verified, trusted)"
    )
    allow_untrusted_workers: bool = Field(
        default=False,
        description="Enable untrusted worker registration"
    )
    require_signatures_production: bool = Field(
        default=False,
        description="Require cryptographic signatures in production"
    )

    # Observability
    enable_metrics: bool = Field(
        default=False,
        description="Enable Prometheus metrics"
    )
    metrics_port: int = Field(
        default=9090,
        description="Prometheus metrics port"
    )

    # CORS configuration (explicit allowlist for security)
    cors_allowed_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:8080"],
        description="Allowed CORS origins (explicit allowlist for security)"
    )
    cors_allow_credentials: bool = Field(
        default=True,
        description="Allow credentials in CORS requests"
    )
    cors_allowed_methods: list[str] = Field(
        default=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        description="Allowed HTTP methods"
    )
    cors_allowed_headers: list[str] = Field(
        default=["Authorization", "Content-Type", "X-Tenant-ID"],
        description="Allowed request headers"
    )

    # Authentication
    api_key: str = Field(
        default="",
        description="API key for REST endpoint authentication"
    )
    allow_insecure_dev: bool = Field(
        default=False,
        description="Allow unauthenticated access (dev only)"
    )

    # Multi-tenancy
    default_tenant_id: str = Field(
        default="default",
        description="Default tenant ID for unauthenticated requests"
    )

    # Rate limiting
    rate_limit_enabled: bool = Field(default=True, description="Enable rate limiting")
    rate_limit_requests_per_minute: int = Field(default=200, description="Rate limit per minute")

    # Validators
    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        """Validate PostgreSQL database URL format."""
        if not v.startswith(("postgresql://", "postgresql+asyncpg://")):
            raise ValueError("database_url must be a PostgreSQL URL (postgresql:// or postgresql+asyncpg://)")
        return v

    @field_validator("port", "metrics_port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        """Validate port number range."""
        if not 1 <= v <= 65535:
            raise ValueError(f"Port must be between 1 and 65535, got {v}")
        return v

    @field_validator("memorygate_url", "asyncgate_url")
    @classmethod
    def validate_integration_url(cls, v: str) -> str:
        """Validate integration URLs are HTTP(S)."""
        if v and not v.startswith(("http://", "https://")):
            raise ValueError(f"URL must start with http:// or https://, got {v}")
        return v

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: str, info) -> str:
        """Validate API key is set when auth is required."""
        # Get allow_insecure_dev from the data being validated
        allow_insecure = info.data.get("allow_insecure_dev", False)
        if not v and not allow_insecure:
            raise ValueError("api_key is required when allow_insecure_dev=False")
        return v


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()


# Convenience accessors
def get_database_url() -> str:
    """Get database URL from settings"""
    return get_settings().database_url


def get_memorygate_url() -> str:
    """Get MemoryGate URL from settings"""
    return get_settings().memorygate_url


def get_memorygate_api_key() -> str:
    """Get MemoryGate API key from settings"""
    return get_settings().memorygate_api_key


def get_asyncgate_url() -> str:
    """Get AsyncGate URL from settings"""
    return get_settings().asyncgate_url


def get_instance_id() -> str:
    """Get DeleGate instance ID"""
    return get_settings().instance_id
