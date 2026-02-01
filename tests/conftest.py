"""
Pytest configuration and fixtures for DeleGate tests.
"""
import asyncio
import os
import pytest
from typing import AsyncGenerator

# Ensure auth is disabled for test runs.
os.environ.setdefault("DELEGATE_ALLOW_INSECURE_DEV", "true")

# Configure pytest-asyncio
pytest_plugins = ('pytest_asyncio',)


@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def anyio_backend():
    return 'asyncio'
