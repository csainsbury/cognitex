"""Pytest configuration and fixtures."""

import pytest


@pytest.fixture
def test_settings():
    """Override settings for testing."""
    from cognitex.config import Settings

    return Settings(
        environment="testing",
        database_url="postgresql://test:test@localhost:5432/test",
        neo4j_uri="bolt://localhost:7687",
        redis_url="redis://localhost:6379/1",
    )
