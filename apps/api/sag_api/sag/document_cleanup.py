"""Delete one document's derived alicecore records across relational and vector stores."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, delete, exists, func, or_, select, text

from sag_api.core.logging import get_logger

log = get_logger("sag.cleanup")


@dataclass(frozen=True)
class DeletedDocumentRecords:
    chunk_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    relation_ids: tuple[str, ...]
    entity_ids: tuple[str, ...]


def _batches(values: tuple[str, ...], size: int = 500) -> Iterator[tuple[str, ...]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _lance_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def _delete_vector_ids(client: Any, index: str, ids: tuple[str, ...]) -> None:
    if not ids:
        return

    # alicecore 0.7.1 has no bulk-delete facade. Keep provider details contained here
    # so deleting thousands of derived rows does not become thousands of transactions.
    open_table = getattr(client, "_open_table", None)
    if callable(open_table):
        table = await open_table(index)
        if table is None:
            return
        for batch in _batches(ids):
            values = ", ".join(_lance_literal(value) for value in batch)
            await table.delete(f"id IN ({values})")
        return

    raw_client = getattr(client, "client", client)
    delete_by_query = getattr(raw_client, "delete_by_query", None)
    if callable(delete_by_query):
        for batch in _batches(ids):
            await delete_by_query(
                index=index,
                query={"ids": {"values": list(batch)}},
                conflicts="proceed",
                refresh=True,
            )
        return

    engine_factory = getattr(client, "_engine", None)
    if callable(engine_factory):
        quote = "`" if client.__class__.__name__ == "OceanBaseVectorStore" else '"'
        statement = text(
            f"DELETE FROM {quote}{index}{quote} WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        for batch in _batches(ids):
            async with engine_factory().begin() as connection:
                await connection.execute(statement, {"ids": list(batch)})
        return

    for record_id in ids:
        await client.delete(index=index, id=record_id)


async def _delete_vectors(records: DeletedDocumentRecords) -> None:
    """Delete the vector rows of ``records``; any failure raises."""
    from alicecore.core.storage.client import get_vector_client

    client = get_vector_client()
    groups = (
        ("source_chunks", records.chunk_ids),
        ("event_vectors", records.event_ids),
        ("event_entity_vectors", records.relation_ids),
        ("entity_vectors", records.entity_ids),
    )
    for index, ids in groups:
        await _delete_vector_ids(client, index, ids)


async def delete_document_records(
    source_config_id: str,
    document_source_id: str,
) -> DeletedDocumentRecords:
    """Delete chunks, events, relations and now-orphaned entities for one document."""
    from alicecore.db import get_session_factory
    from alicecore.db.models import (
        Article,
        ArticleSection,
        Entity,
        EventEntity,
        EventEntityEmbedding,
        SourceChunk,
        SourceEvent,
    )

    session_factory = get_session_factory()
    async with session_factory() as session:
        article_ids = tuple(
            (
                await session.scalars(
                    # The key is the article id only. `Article.source_id` is an optional external
                    # binding the brain never sets; matching it could remove an article some other
                    # live document owns.
                    select(Article.id).where(
                        Article.source_config_id == source_config_id,
                        Article.id == document_source_id,
                    )
                )
            ).all()
        )
        source_ids = tuple({document_source_id, *article_ids})
        chunk_conditions = [SourceChunk.source_id.in_(source_ids)]
        if article_ids:
            chunk_conditions.append(SourceChunk.article_id.in_(article_ids))
        chunk_ids = tuple(
            (
                await session.scalars(
                    select(SourceChunk.id).where(
                        SourceChunk.source_config_id == source_config_id,
                        or_(*chunk_conditions),
                    )
                )
            ).all()
        )

        event_conditions = [SourceEvent.source_id.in_(source_ids)]
        if article_ids:
            event_conditions.append(SourceEvent.article_id.in_(article_ids))
        if chunk_ids:
            event_conditions.append(SourceEvent.chunk_id.in_(chunk_ids))
        event_ids = tuple(
            (
                await session.scalars(
                    select(SourceEvent.id).where(
                        SourceEvent.source_config_id == source_config_id,
                        or_(*event_conditions),
                    )
                )
            ).all()
        )

        relation_rows = (
            (
                await session.execute(
                    select(EventEntity.id, EventEntity.entity_id).where(
                        EventEntity.event_id.in_(event_ids)
                    )
                )
            ).all()
            if event_ids
            else []
        )
        relation_ids = tuple(row.id for row in relation_rows)
        related_entity_ids = tuple({row.entity_id for row in relation_rows})

        if relation_ids:
            await session.execute(
                delete(EventEntityEmbedding).where(EventEntityEmbedding.id.in_(relation_ids))
            )
            await session.execute(delete(EventEntity).where(EventEntity.id.in_(relation_ids)))
        if event_ids:
            await session.execute(delete(SourceEvent).where(SourceEvent.id.in_(event_ids)))

        orphan_entity_ids: tuple[str, ...] = ()
        if related_entity_ids:
            orphan_entity_ids = tuple(
                (
                    await session.scalars(
                        select(Entity.id).where(
                            Entity.id.in_(related_entity_ids),
                            ~exists(
                                select(EventEntity.id).where(EventEntity.entity_id == Entity.id)
                            ),
                        )
                    )
                ).all()
            )
            if orphan_entity_ids:
                await session.execute(delete(Entity).where(Entity.id.in_(orphan_entity_ids)))

        if chunk_ids:
            await session.execute(delete(SourceChunk).where(SourceChunk.id.in_(chunk_ids)))
        if article_ids:
            await session.execute(
                delete(ArticleSection).where(ArticleSection.article_id.in_(article_ids))
            )
            await session.execute(delete(Article).where(Article.id.in_(article_ids)))

        records = DeletedDocumentRecords(
            chunk_ids=chunk_ids,
            event_ids=event_ids,
            relation_ids=relation_ids,
            entity_ids=orphan_entity_ids,
        )
        # Vectors are deleted before the commit: if the vector store fails, the relational deletes
        # roll back and the rows still name every id, so a rerun finds and retries them.
        await _delete_vectors(records)
        await session.commit()
    return records


@dataclass(frozen=True)
class OrphanRecords:
    """Engine rows of one source that no current document owns, grouped by owner id."""

    source_ids: tuple[str, ...]
    articles: int
    chunks: int
    events: int


async def find_orphan_records(source_config_id: str, live_source_ids: set[str]) -> OrphanRecords:
    """Count articles, chunks and events whose owner is not a live document's ``sag_source_id``.

    An event's owner is its article id, or its source id when it has no article; these are the
    same keys `delete_document_records` deletes by.
    """
    from alicecore.db import get_session_factory
    from alicecore.db.models import Article, SourceChunk, SourceEvent

    event_owner = func.coalesce(SourceEvent.article_id, SourceEvent.source_id)
    grouped = {
        "articles": select(Article.id, func.count()).where(Article.source_config_id == source_config_id).group_by(Article.id),
        "chunks": select(SourceChunk.source_id, func.count())
        .where(SourceChunk.source_config_id == source_config_id)
        .group_by(SourceChunk.source_id),
        "events": select(event_owner, func.count())
        .where(SourceEvent.source_config_id == source_config_id)
        .group_by(event_owner),
    }
    totals = dict.fromkeys(grouped, 0)
    owners: set[str] = set()
    async with get_session_factory()() as session:
        for table, statement in grouped.items():
            for owner, count in (await session.execute(statement)).all():
                if owner not in live_source_ids:
                    owners.add(owner)
                    totals[table] += count
    return OrphanRecords(source_ids=tuple(sorted(owners)), **totals)
