"""可注入时钟。测试用 Clock.fixed 固定时间，生产取系统 UTC 时间。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import TracebackType


class Clock:
    def __init__(self, fixed: datetime | None = None) -> None:
        self._fixed = fixed

    @classmethod
    def fixed(cls, value: datetime | str) -> "Clock":
        if isinstance(value, str):
            value = parse_ts(value)
        return cls(value)

    def now(self) -> datetime:
        if self._fixed is not None:
            return self._fixed
        return datetime.now(timezone.utc)

    def now_ts(self) -> str:
        return format_ts(self.now())

    def set(self, value: datetime | str) -> None:
        self._fixed = parse_ts(value) if isinstance(value, str) else value

    def advance(self, **kwargs: object) -> None:
        from datetime import timedelta

        if self._fixed is None:
            self._fixed = self.now()
        self._fixed += timedelta(**kwargs)  # type: ignore[arg-type]

    def __enter__(self) -> "Clock":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._fixed = None


def parse_ts(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # 毫秒精度 + Z，示例：2026-09-22T08:30:00.000Z
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_ts(value: str | None) -> str | None:
    """把任意带偏移量的 ISO 8601 输入规范成可按字符串比较的 UTC 'Z' 形式。"""

    if value is None:
        return None
    return format_ts(parse_ts(value))


clock = Clock()
