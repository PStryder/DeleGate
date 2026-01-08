"""
DeleGate - The Pure Planner

FastAPI application entry point.
"""
import asyncio
import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from delegate.api import router
from delegate.database import init_database, close_database
from delegate.registry import init_registry
from delegate.receipts import retry_worker, stop_retry_worker
from delegate.config import get_settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    settings = get_settings()

    # Initialize database
    init_database()
    logger.info("Database initialized")

    # Initialize worker registry
    await init_registry()
    logger.info("Worker registry initialized")

    # Start receipt retry worker
    retry_task = asyncio.create_task(retry_worker(interval_seconds=60))
    logger.info("Receipt retry worker started")

    logger.info(
        f"DeleGate started",
        extra={
            "instance_id": settings.instance_id,
            "port": settings.port,
        }
    )

    yield

    # Cleanup
    stop_retry_worker()
    retry_task.cancel()
    try:
        await retry_task
    except asyncio.CancelledError:
        pass

    await close_database()
    logger.info("DeleGate shutdown complete")


def create_app() -> FastAPI:
    """Create and configure FastAPI application"""
    settings = get_settings()

    app = FastAPI(
        title="DeleGate",
        description="The Pure Planner - LegiVellum intent-to-plan transformation",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_allowed_methods,
        allow_headers=settings.cors_allowed_headers,
    )

    # Include API routes
    app.include_router(router)

    return app


# Create app instance
app = create_app()


def main():
    """Main entry point for CLI"""
    import uvicorn

    settings = get_settings()

    uvicorn.run(
        "delegate.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
    )


if __name__ == "__main__":
    main()
