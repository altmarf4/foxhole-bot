import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def pytest_collection_modifyitems(items):
    for item in items:
        if "asyncio" not in item.keywords and item.get_closest_marker("asyncio") is None:
            pass


@pytest.fixture
def anyio_backend():
    return "asyncio"
