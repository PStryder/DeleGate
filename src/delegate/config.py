"""
DeleGate Configuration

Environment-based configuration per SPEC-DG-0000.
"""
import os
from functools import lru_cache
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """
    DeleGate configuration settings.
    All settings can be overridden via environment variables with DELEGATE_ prefix.
    """

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

    # CORS
    cors_origins: str = Field(
        default="*",
        description="Comma-separated list of allowed CORS origins"
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

    class Config:
        env_prefix = "DELEGATE_"
        env_file = ".env"
        env_file_encoding = "utf-8"


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


def get_asyncgate_url() -> str:
    """Get AsyncGate URL from settings"""
    return get_settings().asyncgate_url


def get_instance_id() -> str:
    """Get DeleGate instance ID"""
    return get_settings().instance_id
