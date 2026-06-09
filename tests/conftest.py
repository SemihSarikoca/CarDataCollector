"""Shared pytest configuration for the test suite."""
import asyncio

import pytest

# Run all `async def test_*` without per-test decorators.
pytest_plugins = ()


def pytest_collection_modifyitems(items):
    for item in items:
        if asyncio.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker(pytest.mark.asyncio)
