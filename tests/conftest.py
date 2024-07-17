# mypy: ignore-errors
import pytest
import pytest_asyncio
from uuid import uuid4
from pathlib import Path
from fastapi.testclient import TestClient

from yente import settings
from yente.app import create_app
from yente.search.indexer import update_index
from yente.provider import with_provider, close_provider


run_id = uuid4().hex
settings.TESTING = True
FIXTURES_PATH = Path(__file__).parent / "fixtures"
VERSIONS_PATH = FIXTURES_PATH / "versions.json"
MANIFEST_PATH = FIXTURES_PATH / "manifest.yml"
settings.MANIFEST = str(MANIFEST_PATH)
settings.UPDATE_TOKEN = "test"
settings.INDEX_NAME = f"yente-test-{run_id}"
settings.ENTITY_INDEX = f"{settings.INDEX_NAME}-entities"
settings.AUTO_REINDEX = False

app = create_app()
client = TestClient(app)


@pytest.fixture(scope="session", autouse=True)
def manifest():
    settings.MANIFEST = str(MANIFEST_PATH)


@pytest.fixture(scope="function", autouse=False)
def sanctions_catalog():
    manifest_tmp = settings.MANIFEST
    settings.MANIFEST = str(FIXTURES_PATH / "sanctions.yml")
    yield
    settings.MANIFEST = manifest_tmp


@pytest_asyncio.fixture(scope="function", autouse=False)
async def search_provider():
    async with with_provider() as provider:
        yield provider


@pytest_asyncio.fixture(scope="session", autouse=True)
async def load_data():
    await update_index(force=True)
    yield


@pytest_asyncio.fixture(autouse=True)
async def flush_cached_es():
    """Reference: https://github.com/pytest-dev/pytest-asyncio/issues/38#issuecomment-264418154"""
    try:
        yield
    finally:
        await close_provider()
