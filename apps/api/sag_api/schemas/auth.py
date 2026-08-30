from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    name: str = ""

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("The email format is invalid")
        return v


class LoginRequest(BaseModel):
    """Đăng nhập cục bộ. Mọi field đều tuỳ chọn: body rỗng nghĩa là "mở phiên trên máy này".

    Brain chạy một người dùng cho mỗi project trên máy của chính người đó, nên bắt gõ tên trước
    khi vào là một cửa quay không bảo vệ được gì. Tên vẫn nhận được để giữ tương thích với bản
    cũ và cho ai muốn đặt tên identity qua API.
    """

    name: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=255)
    password: str | None = Field(default=None, max_length=128)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return v.strip()

    @field_validator("email")
    @classmethod
    def _optional_email(cls, v: str) -> str:
        v = v.strip().lower()
        if v and ("@" not in v or "." not in v.split("@")[-1]):
            raise ValueError("The email format is invalid")
        return v


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    name: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
