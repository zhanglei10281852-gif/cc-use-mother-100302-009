"""传动设备本地化设计闭环的基础领域契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace, is_dataclass
from datetime import date, datetime
from enum import Enum
from hashlib import sha256
import json
from typing import Iterable


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"无法序列化 {type(value)!r}")


def canonical_fingerprint(data: object) -> str:
    """生成稳定内容摘要，供幂等、快照与审计使用。"""
    payload = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LocalizationCase:
    """保存最小且可校验的业务对象。"""

    case_code: str
    product_line: str
    customer: str
    revision: int

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{key} 不能为空")
            if isinstance(value, int) and value < 1:
                raise ValueError(f"{key} 必须大于零")

    def evolve(self, **changes: object) -> "LocalizationCase":
        """返回新版本，避免就地改写历史对象。"""
        return replace(self, **changes)

    def fingerprint(self) -> str:
        """生成稳定摘要，供幂等和审计使用。"""
        return canonical_fingerprint(asdict(self))


def unique_by_identity(items: Iterable[LocalizationCase]) -> list[LocalizationCase]:
    """按业务标识去重，并拒绝同标识不同内容。"""
    found: dict[str, LocalizationCase] = {}
    for item in items:
        key = str(getattr(item, "case_code"))
        previous = found.get(key)
        if previous is not None and previous.fingerprint() != item.fingerprint():
            raise ValueError(f"业务标识 {key} 对应的内容发生冲突")
        found[key] = item
    return [found[key] for key in sorted(found)]
