"""传动设备本地化设计闭环的基础领域契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
from typing import Iterable


def require_text(value: object, field_name: str) -> str:
    """校验必填文本字段，返回去首尾空白后的值。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空")
    return value.strip()


def require_iso_date(value: object, field_name: str) -> str:
    """校验 YYYY-MM-DD 形式的日期字段。"""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是 YYYY-MM-DD 日期")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是 YYYY-MM-DD 日期") from exc
    return value


def stable_fingerprint(value: object) -> str:
    """为数据类对象生成稳定摘要，供幂等、基线固化和审计使用。"""
    payload = asdict(value) if hasattr(value, "__dataclass_fields__") else value
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LocalizationCase:
    """保存最小且可校验的业务对象。"""

    case_code: str
    product_line: str
    customer: str
    revision: int

    def __post_init__(self) -> None:
        require_text(self.case_code, "case_code")
        require_text(self.product_line, "product_line")
        require_text(self.customer, "customer")
        if self.revision < 1:
            raise ValueError("revision 必须大于零")

    def evolve(self, **changes: object) -> "LocalizationCase":
        """返回新版本，避免就地改写历史对象。"""
        return replace(self, **changes)

    def fingerprint(self) -> str:
        """生成稳定摘要，供幂等和审计使用。"""
        return stable_fingerprint(self)


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
