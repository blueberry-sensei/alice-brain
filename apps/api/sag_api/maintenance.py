"""Maintenance commands run inside the API container.

    python -m sag_api.maintenance orphans --source-id <id>                        # dry run: counts + owner ids
    python -m sag_api.maintenance orphans --source-id <id> --apply --expect-owners N

`orphans` removes engine rows (articles, chunks, events and their vectors) that no current
document of the source owns. They are left behind when a processing attempt died before the
document recorded its engine id; `grep` and lexical search would otherwise keep returning them
without any document to cite. `--apply` refuses unless the owner count still equals the dry run's.
A vector store failure aborts that owner with its rows intact (exit code 1); rerun once it recovers.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

from sqlalchemy import func, select

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, dispose_db
from sag_api.core.logging import get_logger
from sag_api.db.models import Document, Source
from sag_api.enums import DocumentStatus
from sag_api.sag import EngineManager

log = get_logger("maintenance")

#: Documents that may be between "engine rows written" and "engine id recorded".
_BUSY = (DocumentStatus.PENDING, DocumentStatus.LOADING, DocumentStatus.EXTRACTING)


@dataclass
class OrphanRepair:
    source_id: str
    owners: tuple[str, ...]
    articles: int
    chunks: int
    events: int
    applied: bool = False
    skipped: list[str] = field(default_factory=list)


async def _documents(source_id: str) -> list[Document]:
    async with SessionLocal() as session:
        return list((await session.scalars(select(Document).where(Document.source_id == source_id))).all())


async def _still_orphan(source_id: str, owner: str) -> bool:
    """Read the documents again right before a delete: this process holds no lock the API respects."""
    async with SessionLocal() as session:
        busy = await session.scalar(
            select(func.count()).select_from(Document).where(Document.source_id == source_id, Document.status.in_(_BUSY))
        )
        if busy:
            raise SystemExit(f"{busy} document(s) started processing during the repair; stopped before {owner}.")
        owned = await session.scalar(
            select(func.count()).select_from(Document).where(Document.source_id == source_id, Document.sag_source_id == owner)
        )
    return not owned


async def repair_orphans(
    source_id: str, *, apply: bool, engine_manager: EngineManager, expect_owners: int | None = None
) -> OrphanRepair:
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
    if source is None:
        raise SystemExit(f"Source does not exist: {source_id}")
    documents = await _documents(source.id)
    busy = [document.id for document in documents if document.status in _BUSY]
    if apply and busy:
        raise SystemExit(
            f"{len(busy)} document(s) are still processing; their engine rows may not be recorded yet. "
            "Run again when the source is idle."
        )
    live = {document.sag_source_id for document in documents if document.sag_source_id}
    found = await engine_manager.find_orphans(source.sag_source_config_id, live, source=source)
    repair = OrphanRepair(source.id, found.source_ids, found.articles, found.chunks, found.events)
    if not apply:
        return repair
    if expect_owners is None or expect_owners != len(found.source_ids):
        raise SystemExit(
            f"Refusing to apply: {len(found.source_ids)} owner(s) now, expected {expect_owners}. "
            "Run the dry run again and pass its count with --expect-owners."
        )
    repair.applied = True
    for owner in found.source_ids:
        if not await _still_orphan(source.id, owner):
            repair.skipped.append(owner)
            log.warning("Orphan repair skipped source=%s owner=%s: a document owns it now", source.id, owner)
            continue
        await engine_manager.delete_document_data(source.sag_source_config_id, owner, source=source)
        log.warning("Orphan repair removed source=%s owner=%s", source.id, owner)
    log.warning(
        "Orphan repair applied source=%s owners=%d skipped=%d articles=%d chunks=%d events=%d",
        source.id,
        len(found.source_ids),
        len(repair.skipped),
        repair.articles,
        repair.chunks,
        repair.events,
    )
    return repair


def report(repair: OrphanRepair) -> str:
    lines = [f"owner {owner}" for owner in repair.owners] + [f"skipped {owner}" for owner in repair.skipped]
    lines.append(
        f"source_id={repair.source_id} applied={repair.applied} owners={len(repair.owners)} "
        f"articles={repair.articles} chunks={repair.chunks} events={repair.events} "
        f"skipped={len(repair.skipped)}"
    )
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> OrphanRepair:
    engine_manager = EngineManager(settings)
    try:
        return await repair_orphans(
            args.source_id, apply=args.apply, engine_manager=engine_manager, expect_owners=args.expect_owners
        )
    finally:
        await engine_manager.aclose_all()
        await dispose_db()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sag_api.maintenance")
    commands = parser.add_subparsers(dest="command", required=True)
    orphans = commands.add_parser("orphans", help="count or delete engine rows no document owns")
    orphans.add_argument("--source-id", required=True)
    orphans.add_argument("--apply", action="store_true", help="delete them (default: dry run)")
    orphans.add_argument("--expect-owners", type=int, help="owner count printed by the dry run")
    repair = asyncio.run(_run(parser.parse_args(argv)))
    print(report(repair))
    return 0


if __name__ == "__main__":
    sys.exit(main())
