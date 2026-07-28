from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AgentTaskLogRequest(BaseModel):
    """Một lần orchestrator khai báo đã giao việc cho sub-agent (brain không tự thấy được)."""

    agent: str = Field(min_length=1, max_length=120)
    task: str = Field(min_length=1, max_length=2000)
    status: Literal["started", "done", "failed"] = "done"
    model: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=2000)
    #: Nhãn của chính orchestrator (claude-code, codex…), khác với `agent` là bên nhận việc.
    actor: str = Field(default="api", max_length=120)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
