"""Định tuyến nhiều provider cho tầng sinh câu trả lời (LiteLLM boundary).

Engine `alicecore` có router riêng cho đường trích xuất; đây là bản cho đường chat/answer
của API. Luật phân loại lỗi giữ **giống nhau** để hành vi hai đường không lệch nhau:

| Loại            | Dấu hiệu                          | Hành động                          |
|-----------------|-----------------------------------|------------------------------------|
| `transient`     | timeout / kết nối / 5xx           | thử lại **cùng** provider (backoff)|
| `rate_limit`    | 429 / quota / resource exhausted  | đổi provider + nghỉ theo `Retry-After` |
| `auth`          | 401 / 403 / invalid api key       | tắt provider, đổi nhà              |
| `model_missing` | 404 / model not found             | tắt provider, đổi nhà              |
| `bad_request`   | 400                               | dừng luôn (đổi nhà cũng lỗi y vậy) |

429 không phải một loại lỗi mà là hai loại chung một mã: hạn mức theo phút (server nói
`Retry-After: 12`, chờ mười mấy giây là chạy tiếp) và hạn mức theo ngày (server nói một con số
rất lớn, hoặc không nói gì). Nên cooldown ở đây lấy **con số của server** khi có; và khi server
đã tự nói thì đó là `banned_until` — lệnh cấm thật, không phải gợi ý — vì gọi sớm ở phần lớn
gateway chỉ làm ban dài thêm. Luật này khớp `alicecore.core.ai.routing` để đường extraction và
đường generation không nghỉ khác nhau trên cùng một lần 429.

Mọi lần thử đều được ghi vào `ATTEMPT_LOG` để UI hiển thị — **không có thất bại im lặng**.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable, TypeVar

from sag_api.core.logging import get_logger
from sag_api.core.retry_after import retry_after_seconds

log = get_logger("llm_routing")

T = TypeVar("T")

_STATUS_RE = re.compile(r"(?:error code|status(?:\s+code)?|http)\D{0,3}(\d{3})", re.IGNORECASE)

#: Số bản ghi giữ lại cho UI. Đủ để soi một phiên làm việc; không cần bền vững qua restart.
_ATTEMPT_LOG_SIZE = 200


class FailureKind(str, Enum):
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    MODEL_MISSING = "model_missing"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


UNHEALTHY_KINDS = frozenset({FailureKind.AUTH, FailureKind.MODEL_MISSING})


def _status_of(error: BaseException) -> int | None:
    for attribute in ("status_code", "http_status", "code"):
        value = getattr(error, attribute, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    match = _STATUS_RE.search(str(error))
    return int(match.group(1)) if match else None


def classify_failure(error: BaseException) -> FailureKind:
    """Xếp loại lỗi provider. Ưu tiên status code, sau đó mới đến tên lớp và chữ trong message."""
    status = _status_of(error)
    if status == 429:
        return FailureKind.RATE_LIMIT
    if status in {401, 403}:
        return FailureKind.AUTH
    if status == 404:
        return FailureKind.MODEL_MISSING
    if status is not None and 500 <= status <= 599:
        return FailureKind.TRANSIENT

    name = type(error).__name__.casefold()
    if "ratelimit" in name:
        return FailureKind.RATE_LIMIT
    if "authentication" in name or "permissiondenied" in name:
        return FailureKind.AUTH
    if "notfound" in name:
        return FailureKind.MODEL_MISSING
    if "timeout" in name or "apiconnection" in name or "internalserver" in name:
        return FailureKind.TRANSIENT

    text = str(error).casefold()
    if any(word in text for word in ("rate limit", "too many requests", "quota", "resource exhausted", "overloaded")):
        return FailureKind.RATE_LIMIT
    if any(
        word in text
        for word in ("invalid api key", "incorrect api key", "unauthorized", "unauthenticated", "permission denied")
    ):
        return FailureKind.AUTH
    if any(word in text for word in ("model not found", "no such model", "unknown model", "does not exist")):
        return FailureKind.MODEL_MISSING
    if any(word in text for word in ("timeout", "timed out", "connection reset", "connection refused")):
        return FailureKind.TRANSIENT
    if status == 400:
        return FailureKind.BAD_REQUEST
    return FailureKind.UNKNOWN


@dataclass
class AttemptRecord:
    """Một lần gọi tới một provider — thành công cũng ghi, để biết cuối cùng ai trả lời."""

    provider_id: str
    label: str
    model: str
    stage: str  # generation | extraction
    attempt: int
    ok: bool
    action: str  # ok | retry | failover | abort
    latency_ms: int
    kind: str | None = None
    error: str | None = None
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ATTEMPT_LOG: deque[AttemptRecord] = deque(maxlen=_ATTEMPT_LOG_SIZE)


def record_attempt(record: AttemptRecord) -> None:
    ATTEMPT_LOG.append(record)
    if record.ok:
        log.debug("provider ok: %s (%dms)", record.label, record.latency_ms)
    else:
        log.warning(
            "provider %s [%s] → %s: %s",
            record.label,
            record.kind,
            record.action,
            (record.error or "")[:200],
        )


def recent_attempts(limit: int = 50) -> list[dict[str, Any]]:
    items = list(ATTEMPT_LOG)[-limit:]
    items.reverse()  # mới nhất lên đầu
    return [item.to_dict() for item in items]


def clear_attempts() -> None:
    ATTEMPT_LOG.clear()


@dataclass
class _State:
    cooldown_until: float = 0.0
    #: Khác `cooldown_until` ở chỗ đây là con số **server tự nói** qua `Retry-After`. Cooldown
    #: đoán ra thì có thể bỏ qua khi cả chuỗi bận; ban do server đặt thì không — gọi sớm chỉ làm
    #: ban dài thêm, và đó chính là cách một provider bị nhốt cả ngày vì một hạn mức theo phút.
    banned_until: float = 0.0
    unhealthy_reason: str | None = None
    consecutive_failures: int = 0


class ChainRunner:
    """Chạy một lời gọi qua chuỗi provider theo ưu tiên.

    Trạng thái (cooldown / tắt vì sai key) sống theo tiến trình và **dùng chung** cho mọi
    lời gọi, nhờ đó một provider vừa bị 429 sẽ không bị đâm lại ngay ở request kế tiếp.
    """

    def __init__(self, *, retry_delay: float = 2.0, backoff_factor: float = 2.0, max_delay: float = 30.0) -> None:
        self.retry_delay = retry_delay
        self.backoff_factor = backoff_factor
        self.max_delay = max_delay
        self._states: dict[str, _State] = {}

    def _state(self, provider_id: str) -> _State:
        return self._states.setdefault(provider_id, _State())

    def reset(self) -> None:
        """Xoá trạng thái — gọi khi cấu hình đổi (key mới thì provider đáng được thử lại)."""
        self._states.clear()

    def health_snapshot(self, chain: Iterable[dict]) -> list[dict[str, Any]]:
        now = time.monotonic()
        snapshot = []
        for entry in chain:
            state = self._state(str(entry.get("id")))
            snapshot.append(
                {
                    "provider_id": entry.get("id"),
                    "label": entry.get("label") or f"{entry.get('id')} / {entry.get('model')}",
                    "model": entry.get("model"),
                    "priority": entry.get("priority", 100),
                    "unhealthy_reason": state.unhealthy_reason,
                    "cooldown_remaining": max(0.0, round(state.cooldown_until - now, 1)),
                    "banned_remaining": max(0.0, round(state.banned_until - now, 1)),
                    "consecutive_failures": state.consecutive_failures,
                }
            )
        return snapshot

    def _usable(self, chain: list[dict]) -> list[dict]:
        now = time.monotonic()
        ready = [
            entry
            for entry in chain
            if self._state(str(entry.get("id"))).unhealthy_reason is None
            and self._state(str(entry.get("id"))).cooldown_until <= now
        ]
        if ready:
            return ready
        # Hết provider "sẵn sàng" thì vẫn thử những cái chưa bị tắt: cooldown là gợi ý,
        # không phải lệnh cấm — thà thử còn hơn trả lỗi khi có thể vẫn chạy được.
        #
        # Ngoại lệ: provider đang trong `banned_until` thì KHÔNG được thử. Đó là con số server
        # tự đặt qua `Retry-After`; gọi sớm không phải "cố thêm một lần", nó gia hạn ban.
        advisory = [
            entry
            for entry in chain
            if self._state(str(entry.get("id"))).unhealthy_reason is None
            and self._state(str(entry.get("id"))).banned_until <= now
        ]
        if advisory:
            return advisory
        return [entry for entry in chain if self._state(str(entry.get("id"))).banned_until <= now]

    def _delay(self, attempt: int) -> float:
        base = self.retry_delay * (self.backoff_factor**attempt)
        return min(base * (0.5 + random.random() * 0.5), self.max_delay)

    def _apply_rate_limit(self, label: str, entry: dict, state: _State, error: BaseException) -> None:
        """429: nghỉ đúng bằng `Retry-After` của server nếu có, không thì theo cooldown cấu hình.

        Con số của server là thứ duy nhất phân biệt được hạn mức theo phút với hạn mức theo ngày;
        cooldown cấu hình chỉ là phỏng đoán cho gateway không chịu nói.
        """
        asked = retry_after_seconds(error)
        seconds = asked if asked is not None else float(entry.get("cooldown_seconds", 60.0))
        state.cooldown_until = time.monotonic() + seconds
        if asked is not None:
            state.banned_until = state.cooldown_until
            log.warning("provider %s asked for Retry-After %.0fs - no call until then", label, asked)

    async def run(
        self,
        chain: list[dict],
        call: Callable[[dict], Awaitable[T]],
        *,
        stage: str = "generation",
        default_max_retries: int = 2,
    ) -> T:
        """Gọi `call(entry)` lần lượt theo chuỗi cho tới khi có kết quả.

        Raises:
            RuntimeError: khi mọi provider đều thất bại — message liệt kê lý do từng provider.
        """
        failures: list[str] = []
        last_error: BaseException | None = None

        for entry in self._usable(chain):
            provider_id = str(entry.get("id"))
            label = str(entry.get("label") or f"{provider_id} / {entry.get('model')}")
            state = self._state(provider_id)
            max_retries = int(entry.get("max_retries") or default_max_retries)

            for attempt in range(max_retries + 1):
                started = time.monotonic()
                try:
                    result = await call(entry)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - phân loại xong mới quyết định
                    latency_ms = int((time.monotonic() - started) * 1000)
                    kind = classify_failure(error)
                    last_error = error
                    state.consecutive_failures += 1

                    if kind is FailureKind.BAD_REQUEST:
                        action = "abort"
                    elif kind is FailureKind.TRANSIENT and attempt < max_retries:
                        action = "retry"
                    else:
                        action = "failover"

                    record_attempt(
                        AttemptRecord(
                            provider_id=provider_id,
                            label=label,
                            model=str(entry.get("model") or ""),
                            stage=stage,
                            attempt=attempt + 1,
                            ok=False,
                            action=action,
                            latency_ms=latency_ms,
                            kind=kind.value,
                            error=str(error)[:500],
                        )
                    )

                    if action == "abort":
                        raise
                    if action == "retry":
                        await asyncio.sleep(self._delay(attempt))
                        continue
                    if kind in UNHEALTHY_KINDS:
                        state.unhealthy_reason = f"{kind.value}: {str(error)[:200]}"
                    elif kind is FailureKind.RATE_LIMIT:
                        self._apply_rate_limit(label, entry, state, error)
                    failures.append(f"{label} [{kind.value}] {str(error)[:200]}")
                    break
                else:
                    state.consecutive_failures = 0
                    state.cooldown_until = 0.0
                    state.banned_until = 0.0
                    record_attempt(
                        AttemptRecord(
                            provider_id=provider_id,
                            label=label,
                            model=str(entry.get("model") or ""),
                            stage=stage,
                            attempt=attempt + 1,
                            ok=True,
                            action="ok",
                            latency_ms=int((time.monotonic() - started) * 1000),
                        )
                    )
                    return result

        detail = "; ".join(failures) if failures else self._unavailable_detail(chain)
        raise RuntimeError(detail) from last_error

    def _unavailable_detail(self, chain: list[dict]) -> str:
        """Vì sao chuỗi rỗng — "không có provider nào khả dụng" một mình là câu vô dụng."""
        now = time.monotonic()
        parts = []
        for entry in chain:
            label = str(entry.get("label") or f"{entry.get('id')} / {entry.get('model')}")
            state = self._state(str(entry.get("id")))
            if state.banned_until > now:
                parts.append(f"{label} [rate limit, còn {state.banned_until - now:.0f}s]")
            elif state.unhealthy_reason:
                parts.append(f"{label} [{state.unhealthy_reason}]")
        return "; ".join(parts) if parts else "không có provider nào khả dụng"
