"""本地化设计闭环领域模型。

覆盖从客户声音到设计基线的完整链路：

客户声音 -> 需求（澄清/优先级/撤回）-> 冲突与偏差 -> 产品配置
-> 设计决定 -> 计算/样机验证/供应商替代/工程变更 -> 设计基线 -> 已交付序列号

所有领域对象均为不可变值对象，状态演进通过替换新版本完成，
基线与交付记录保存完整内容快照，保证历史依据不被后续事件改写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from .contracts import canonical_fingerprint


class SourceKind(str, Enum):
    """客户声音来源，销售口头承诺必须落到可溯源记录后才能驱动需求。"""

    SITE_ISSUE = "site_issue"              # 客户现场问题
    SALES_VERBAL = "sales_verbal"          # 销售口头承诺（需补来源凭证）
    FIELD_REPORT = "field_report"          # 现场服务报告
    CUSTOMER_DOCUMENT = "customer_document"  # 客户来函/技术协议


class RequirementStatus(str, Enum):
    DRAFT = "draft"            # 待澄清
    CLARIFIED = "clarified"    # 已澄清，待确认优先级
    CONFIRMED = "confirmed"    # 已确认，可进入配置与基线
    WITHDRAWN = "withdrawn"    # 已撤回（触发影响传播）


class Priority(str, Enum):
    P1_SAFETY = "P1"   # 安全/强制
    P2_HIGH = "P2"
    P3_MEDIUM = "P3"
    P4_LOW = "P4"

    @property
    def rank(self) -> int:
        return {"P1": 1, "P2": 2, "P3": 3, "P4": 4}[self.value]


class ConflictKind(str, Enum):
    REQUIREMENT = "requirement"            # 需求之间冲突（含安全冲突）
    REGULATION = "regulation"              # 法规更新导致的冲突
    OBSOLESCENCE = "obsolescence"          # 零件停产导致的冲突
    VALIDATION_FAILURE = "validation_failure"  # 样机验证失败


class ConflictStatus(str, Enum):
    OPEN = "open"                          # 未解决
    RESOLVED = "resolved"                  # 已由设计决定解决
    ACCEPTED_DEVIATION = "accepted_deviation"  # 接受偏差（限非安全冲突）


class VerificationStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class SubstitutionStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class ChangeStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    IMPLEMENTED = "implemented"


class EventKind(str, Enum):
    REQUIREMENT_WITHDRAWN = "requirement_withdrawn"
    REGULATION_UPDATE = "regulation_update"
    PART_OBSOLESCENCE = "part_obsolescence"
    VALIDATION_FAILURE = "validation_failure"


class EventStatus(str, Enum):
    NEW = "new"
    PROPAGATED = "propagated"


@dataclass(frozen=True, slots=True)
class CustomerVoice:
    """客户声音：现场问题或销售口头承诺，必须带有可核验来源。"""

    voice_id: str
    case_code: str
    source_kind: SourceKind
    source_ref: str          # 会议纪要/现场记录/服务单号，销售口头承诺不得为空
    customer: str
    recorded_at: date
    raw_text: str
    captured_by: str

    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


@dataclass(frozen=True, slots=True)
class Requirement:
    """带来源的需求，经澄清、优先级确认后方可进入设计基线。"""

    req_id: str
    case_code: str
    voice_ids: tuple[str, ...]
    title: str
    statement: str
    categories: tuple[str, ...]          # 如 environment / load / maintenance / safety
    target_customers: tuple[str, ...]
    priority: Priority | None
    status: RequirementStatus
    revision: int = 1
    withdrawn_reason: str | None = None

    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


@dataclass(frozen=True, slots=True)
class Conflict:
    """需求冲突或外部事件引发的冲突，安全冲突未解决不得发布。"""

    conflict_id: str
    case_code: str
    kind: ConflictKind
    is_safety: bool
    description: str
    requirement_ids: tuple[str, ...]     # 关联需求（停产/验证冲突可留空）
    config_ids: tuple[str, ...]          # 停产/验证冲突直接关联配置
    status: ConflictStatus = ConflictStatus.OPEN
    resolution_decision_id: str | None = None
    justification: str | None = None
    deviation_customers: tuple[str, ...] = ()   # 接受偏差时必填：适用客户
    deviation_expiry: date | None = None        # 接受偏差时必填：期限

    def is_open(self) -> bool:
        return self.status is ConflictStatus.OPEN

    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


@dataclass(frozen=True, slots=True)
class ProductConfiguration:
    """产品配置：同一减速机面向矿山/风电/机器人产线的不同配置。"""

    config_id: str
    case_code: str
    name: str
    customers: tuple[str, ...]
    requirement_ids: tuple[str, ...] = ()
    current_baseline_id: str | None = None

    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


@dataclass(frozen=True, slots=True)
class DesignDecision:
    """设计决定：关联需求与冲突，构成客户声音到设计决定的追踪环节。"""

    decision_id: str
    case_code: str
    config_id: str
    title: str
    chosen_option: str
    rejected_options: tuple[str, ...]
    linked_requirement_ids: tuple[str, ...]
    linked_conflict_id: str | None = None
    decided_at: date | None = None

    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


@dataclass(frozen=True, slots=True)
class Calculation:
    """计算校核记录，覆盖若干需求。"""

    calc_id: str
    config_id: str
    name: str
    standard: str
    requirement_ids: tuple[str, ...]
    status: VerificationStatus = VerificationStatus.PENDING
    result_summary: str | None = None


@dataclass(frozen=True, slots=True)
class PrototypeValidation:
    """样机验证记录，覆盖若干需求。"""

    validation_id: str
    config_id: str
    prototype_code: str
    requirement_ids: tuple[str, ...]
    status: VerificationStatus = VerificationStatus.PENDING
    result_summary: str | None = None


@dataclass(frozen=True, slots=True)
class PartUsage:
    """配置在用零件清单，是停产传播判定的依据。"""

    config_id: str
    part_number: str
    part_name: str
    supplier: str


@dataclass(frozen=True, slots=True)
class SupplierSubstitution:
    """供应商替代：停产、国产化或降本引发，批准前阻断发布。"""

    substitution_id: str
    config_id: str
    part_number: str
    old_supplier: str
    new_supplier: str
    reason: str
    status: SubstitutionStatus = SubstitutionStatus.PROPOSED
    linked_change_id: str | None = None


@dataclass(frozen=True, slots=True)
class EngineeringChange:
    """工程变更，串联冲突、替代与验证返工。"""

    change_id: str
    case_code: str
    config_id: str
    title: str
    trigger_ref: str                    # 冲突/事件/替代编号
    linked_requirement_ids: tuple[str, ...]
    status: ChangeStatus = ChangeStatus.DRAFT


@dataclass(frozen=True, slots=True)
class Baseline:
    """设计基线：发布时刻的不可变快照，作为交付与追溯依据。"""

    baseline_id: str
    case_code: str
    config_id: str
    revision: int
    released_at: date
    requirements: tuple[Requirement, ...]
    decision_ids: tuple[str, ...]
    calc_ids: tuple[str, ...]
    validation_ids: tuple[str, ...]
    substitution_ids: tuple[str, ...]
    accepted_deviation_ids: tuple[str, ...]
    fingerprint: str = field(default="")

    def requirement_fingerprints(self) -> dict[str, str]:
        return {req.req_id: req.fingerprint() for req in self.requirements}


@dataclass(frozen=True, slots=True)
class ReleasedUnit:
    """已交付序列号：冻结交付时基线依据，后续事件不改写其依据。"""

    serial_number: str
    config_id: str
    baseline_id: str
    baseline_fingerprint: str
    delivered_at: date
    requirement_fingerprints: dict[str, str]


@dataclass(frozen=True, slots=True)
class ImpactEvent:
    """需求撤回/法规更新/零件停产/验证失败事件及其传播结果。"""

    event_id: str
    case_code: str
    kind: EventKind
    detail: str
    ref_id: str                          # 撤回需求/法规条款/零件/验证编号
    occurred_at: date
    affected_config_ids: tuple[str, ...] = ()
    affected_unit_serials: tuple[str, ...] = ()
    status: EventStatus = EventStatus.NEW
