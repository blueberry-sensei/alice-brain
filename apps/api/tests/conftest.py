"""Test fixtures: point the configuration at a temporary directory before sag_api is imported (settings is a process-level singleton)."""

import os
import sys
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="sag-test-")
os.environ.setdefault("SAG_DATABASE_URL", f"sqlite+aiosqlite:///{_TMP}/sag.db")
os.environ.setdefault("SAG_DATA_DIR", f"{_TMP}/sag")
os.environ.setdefault("SAG_UPLOAD_DIR", f"{_TMP}/uploads")
os.environ.setdefault("SAG_DEBUG", "false")
os.environ.setdefault("SAG_SAG_LANGUAGE", "en")
# Force offline: even when a .env with a real key exists, the tests stay deterministic (no LLM call is made)
os.environ["SAG_LLM_API_KEY"] = ""
os.environ["SAG_LLM_BASE_URL"] = ""
os.environ["SAG_EMBEDDING_API_KEY"] = ""


@pytest.fixture(autouse=True)
async def _isolate_persisted_jobs():
    """A test must not recover queued jobs created by an earlier app lifespan."""
    yield
    if "sag_api.core.db" not in sys.modules:
        return

    from sqlalchemy import delete, inspect

    from sag_api.core.db import SessionLocal, engine
    from sag_api.db.models import Job

    async with engine.connect() as connection:
        exists = await connection.run_sync(lambda sync: inspect(sync).has_table(Job.__tablename__))
    if not exists:
        return

    async with SessionLocal() as session:
        await session.execute(delete(Job))
        await session.commit()
