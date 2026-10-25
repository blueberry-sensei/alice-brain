from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sag_api.enums import DocumentStatus


class MessageItem(BaseModel):
    text: str
    author: str | None = None
    role: str | None = None
    ts: str | None = None
    thread: str | None = None


class IngestRequest(BaseModel):
    """Unified write: either a text or a batch of messages, one of the two."""

    text: str | None = None
    title: str | None = None
    messages: list[MessageItem] | None = Field(default=None)


class ExtractedEntity(BaseModel):
    type: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=2, max_length=500)

    @field_validator("type", "name", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ExtractedEvent(BaseModel):
    """One self-contained statement the client extracted, with its line inside `text` (1-based)."""

    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    line: int = Field(ge=1)
    entities: list[ExtractedEntity] = Field(default_factory=list)


class ExtractedIngestRequest(BaseModel):
    """Write a document whose events are already extracted: the server chunks and embeds only, no LLM."""

    title: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1)
    origin_path: str = Field(min_length=1, max_length=512)
    origin_line: int = Field(ge=1)
    events: list[ExtractedEvent] = Field(min_length=1)

    @model_validator(mode="after")
    def lines_inside_text(self) -> ExtractedIngestRequest:
        total = len(self.text.splitlines())
        outside = [event.line for event in self.events if event.line > total]
        if outside:
            raise ValueError(f"event line(s) {outside[:5]} are past the end of the text ({total} lines)")
        return self


class OriginUpdate(BaseModel):
    document_id: str
    origin_line: int = Field(ge=1)


class OriginUpdateRequest(BaseModel):
    """Move documents to new start lines after unrelated edits shifted their file."""

    items: list[OriginUpdate] = Field(min_length=1)


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_id: str
    filename: str
    content_type: str
    size_bytes: int
    status: DocumentStatus
    chunk_count: int
    event_count: int
    progress: int
    token_usage: int
    error: str | None
    origin_path: str | None = None
    origin_line: int | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("error", mode="before")
    @classmethod
    def redact_storage_error(cls, value: object) -> object:
        """Keep SQL and local storage details in server logs, not in the UI."""
        if not isinstance(value, str):
            return value
        normalized = value.lower()
        sql_markers = (
            "sqlite3.",
            "sqlalchemy.exc",
            "[sql:",
            "[parameters:",
            "sqlalche.me/e/",
        )
        if not any(marker in normalized for marker in sql_markers):
            return value
        if "foreign key constraint failed" in normalized:
            return "The source has not finished initialising, so the document is not stored yet; please retry."
        return "Storing the document failed; please retry, and check the service log if it keeps failing."
