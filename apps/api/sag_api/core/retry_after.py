"""Đọc con số `Retry-After` mà provider tự đưa ra khi trả 429.

Vì sao cần riêng một module: 429 **không** phải một loại lỗi. Nó là hai loại nằm chung một mã.

* Hạn mức theo phút (RPM/TPM). Server thường nói `Retry-After: 12`. Chờ 12 giây là chạy tiếp.
* Hạn mức theo ngày (hết quota). Server nói một con số rất lớn, hoặc không nói gì.

Đối xử với cả hai bằng một hằng số cứng là sai theo cả hai hướng: nghỉ 60 giây cho một hạn mức
ngày thì cứ mỗi phút lại đâm vào tường một lần (và nhiều gateway **gia hạn** ban mỗi lần bị đâm),
còn nghỉ 60 giây cho một hạn mức phút 5 giây thì bỏ phí provider tốt nhất trong chuỗi.

Bản này cố ý **chép lại** `alicecore.core.ai.rate_limit.retry_after_seconds` thay vì import:
`sag_api` chỉ được phép import `alicecore` trong `sag_api/sag/` (xem AGENTS.md). Hai bản phải
đọc cùng một tập header và cùng một tập câu chữ, nếu không đường extraction và đường generation
sẽ nghỉ khác nhau trên cùng một lần 429.
"""

from __future__ import annotations

import datetime as _dt
import re
from email.utils import parsedate_to_datetime
from typing import Any

#: Trần khi server nói một con số vô lý (hoặc HTTP-date lệch đồng hồ). Một tiếng là đủ cho mọi
#: hạn mức theo phút/giờ; hạn mức theo ngày thì chờ tiếp cũng không còn ý nghĩa với một request.
MAX_RETRY_AFTER_SECONDS = 3600.0

#: Các header mà gateway thực tế dùng để nói "chờ bấy nhiêu giây".
_HEADER_NAMES = ("retry-after", "Retry-After", "x-ratelimit-reset-after", "ratelimit-reset")

#: "try again in 690 seconds", "retry after 11 minutes", "please wait 30s"
_TEXT_PATTERNS = (
    re.compile(
        r"retry[- ]after[\"'\s:=]+(\d+(?:\.\d+)?)\s*(ms|s|sec|secs|second|seconds|m|min|minute|minutes)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:try|retry)\s+again\s+in\s+(\d+(?:\.\d+)?)\s*(ms|s|sec|secs|second|seconds|m|min|minute|minutes)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:please\s+)?wait\s+(\d+(?:\.\d+)?)\s*(ms|s|sec|secs|second|seconds|m|min|minute|minutes)",
        re.IGNORECASE,
    ),
)

_UNIT_SCALE = {
    "ms": 0.001,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
}


def _clamp(seconds: float) -> float | None:
    if seconds <= 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _from_header_value(raw: Any) -> float | None:
    """`Retry-After` cho phép hai dạng: delta-seconds và HTTP-date (RFC 9110)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return _clamp(float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    now = _dt.datetime.now(tz=when.tzinfo) if when.tzinfo else _dt.datetime.now()
    return _clamp((when - now).total_seconds())


def _from_headers(headers: Any) -> float | None:
    if headers is None:
        return None
    for name in _HEADER_NAMES:
        try:
            raw = headers.get(name)
        except Exception:  # noqa: BLE001 - header container lạ thì bỏ qua, không được nổ ở đây
            raw = None
        value = _from_header_value(raw)
        if value is not None:
            return value
    return None


def _from_text(text: str) -> float | None:
    for pattern in _TEXT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            amount = float(match.group(1))
        except (TypeError, ValueError):
            continue
        unit = (match.group(2) or "s").lower()
        value = _clamp(amount * _UNIT_SCALE.get(unit, 1.0))
        if value is not None:
            return value
    return None


def retry_after_seconds(source: Any) -> float | None:
    """Server yêu cầu chờ bao lâu, hoặc None nếu nó không nói.

    Nhận cả `httpx.Response`, exception của SDK (openai/litellm bọc response vào `.response`),
    và chuỗi thuần. Ưu tiên header thật; chỉ khi không có header mới đọc text, vì text là
    phỏng đoán và có thể trúng một con số không liên quan.
    """
    if source is None:
        return None

    for holder in (source, getattr(source, "response", None)):
        value = _from_headers(getattr(holder, "headers", None))
        if value is not None:
            return value

    body = getattr(source, "text", None)
    text = str(source) if isinstance(source, str) else str(source)
    if isinstance(body, str) and body:
        # Response của httpx: `str(response)` chỉ ra "<Response [429]>", nội dung thật nằm ở .text
        text = f"{text} {body}"
    return _from_text(text)


def describe_wait(seconds: float | None) -> str:
    """Con số cho người đọc: giây thì nói giây, dài thì nói phút — không in 4200.0."""
    if seconds is None:
        return ""
    if seconds < 90:
        return f"{seconds:.0f} giây"
    return f"{seconds / 60:.0f} phút"
