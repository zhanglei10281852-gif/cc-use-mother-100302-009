"""本地化设计闭环领域模型：客户声音、需求、冲突、配置基线、验证、变更与交付。

设计原则：
- 所有领域对象不可变，状态推进通过 evolve 产出新版本，保留历史依据。
- 每条客户声音和需求都带来源，形成"客户声音 -> 需求 -> 设计决定"的可追踪链路。
- 已交付序列号固化在发布时的基线快照上，后续变更只影响受影响配置，不回改已交付依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Final

from .contracts import require_iso_date, require_text, stable_fingerprint


class RequirementStatus(str, Enum):
    """需求生命周期状态。"""

    DRAFT = "draft"                # 已录入，等待澄清
    CLARIFIED = "clarified"        # 已澄清，等待冲突分析/优先级确认
    PRIORITIZED = "prioritized"    # 优先级已确认，可进入基线
    BASELINED = "baselined"        # 已进入某条配置基线
    WITHDRAWN = "withdrawn"        # 客户/法规撤回，需传播
    SUPERSEDED = "superseded"      # 被新版本需求替代


class ConflictType(str, Enum):
    """冲突类型。"""

    REQUIREMENT = "requirement"    # 需求互相矛盾（如 IP68 与维护窗口）
    REGULATION = "regulation"      # 与法规要求冲突
    SAFETY = "safety"              # 安全相关冲突，未解决不得发布


class ConflictStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"          # 已解决（给出设计决定）
    DEVIATION = "deviation"        # 接受偏差：必须有适用客户与期限
    ESCALATED = "escalated"        # 升级中，仍视为未解决


class Priority(str, Enum):
    P1 = "P1"   # 安全/合规/核心性能
    P2 = "P2"   # 重要客户特定要求
    P3 = "P3"   # 一般改进


class ConfigStatus(str, Enum):
    DRAFT = "draft"
    RELEASED = "released"          # 发布：安全冲突全部关闭、需求均已确认
    FROZEN = "frozen"              # 冻结（例如交付后），变更须走工程变更单


class VerificationKind(str, Enum):
    CALCULATION = "calculation"    # 计算校核（如热容量、接触强度）
    PROTOTYPE = "prototype"        # 样机试验
    FIELD = "field"                # 现场验证


class VerificationResult(str, Enum):
    PLANNED = "planned"
    PASSED = "passed"
    FAILED = "failed"              # 验证失败：必须传播到受影响配置
    WAIVED = "waived"              # 经验证委员会批准的豁免


class ChangeKind(str, Enum):
    SUPPLIER_SUBSTITUTION = "supplier_substitution"  # 供应商替代
    PART_OBSOLESCENCE = "part_obsolescence"          # 零件停产
    REGULATION_UPDATE = "regulation_update"          # 法规更新
    VERIFICATION_FAILURE = "verification_failure"    # 验证失败返工
    REQUIREMENT_WITHDRAWAL = "requirement_withdrawal"
    DESIGN_REVISION = "design_revision"


# 验证失败、撤回、停产、法规更新等事件需要传播的变更类型
PROPAGATING_CHANGE_KINDS: Final[frozenset[ChangeKind]] = frozenset(
    {
        ChangeKind.SUPPLIER_SUBSTITUTION,
        ChangeKind.PART_OBSOLESCENCE,
        ChangeKind.REGULATION_UPDATE,
        ChangeKind.VERIFICATION_FAILURE,
        ChangeKind.REQUIREMENT_WITHDRAWAL,
    }
)


@dataclass(frozen=True, slots=True)
class Source:
    """客户声音的来源凭证，保证需求可回溯到现场。"""

    source_type: str               # site_report / sales_minutes / regulation / customer_email ...
    reference: str                 # 报告编号、会议纪要号、法规条款号
    customer: str                  # 提出方（客户名称或监管机构）
    recorded_at: str               # YYYY-MM-DD
    contact: str | None = None

    def __post_init__(self) -> None:
        require_text(self.source_type, "source_type")
        require_text(self.reference, "reference")
        require_text(self.customer, "customer")
        require_iso_date(self.recorded_at, "recorded_at")


@dataclass(frozen=True, slots=True)
class VoiceOfCustomer:
    """客户现场问题/口头承诺，必须带来源，否则不得转需求。"""

    voc_code: str
    product_line: str
    statement: str
    source: Source
    translated_requirement_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.voc_code, "voc_code")
        require_text(self.product_line, "product_line")
        require_text(self.statement, "statement")
        if not isinstance(self.source, Source):
            raise ValueError("source 必须是 Source 对象")

    def fingerprint(self) -> str:
        return stable_fingerprint(self)


@dataclass(frozen=True, slots=True)
class Requirement:
    """带来源的本地化需求。

    - applicable_customers: 适用客户范围；空元组表示全产品线通用。
    - safety_related: 安全相关需求，其冲突按安全冲突处理。
    """

    req_code: str
    product_line: str
    title: str
    statement: str
    source_voc_codes: tuple[str, ...]
    priority: Priority | None = None          # 优先级确认前为 None
    status: RequirementStatus = RequirementStatus.DRAFT
    applicable_customers: tuple[str, ...] = ()
    safety_related: bool = False
    regulation_ref: str | None = None         # 法规条款（法规类需求）
    supersedes: str | None = None             # 被本需求替代的旧需求编号
    clarification_notes: str | None = None

    def __post_init__(self) -> None:
        require_text(self.req_code, "req_code")
        require_text(self.product_line, "product_line")
        require_text(self.title, "title")
        require_text(self.statement, "statement")
        if not self.source_voc_codes:
            raise ValueError("需求至少需要一个来源（source_voc_codes 不能为空）")
        for code in self.source_voc_codes:
            require_text(code, "source_voc_codes 元素")

    def evolve(self, **changes: object) -> "Requirement":
        return replace(self, **changes)

    def fingerprint(self) -> str:
        return stable_fingerprint(self)


@dataclass(frozen=True, slots=True)
class Conflict:
    """需求间或需求与法规间的冲突。

    安全冲突（conflict_type=SAFETY）在未解决（open/escalated）时阻止配置发布。
    接受偏差（DEVIATION）必须填写适用客户与偏差到期日。
    """

    conflict_code: str
    conflict_type: ConflictType
    requirement_codes: tuple[str, ...]
    description: str
    status: ConflictStatus = ConflictStatus.OPEN
    resolution_note: str | None = None
    decision_ref: str | None = None           # 关联的设计决定/计算报告
    deviation_applicable_customers: tuple[str, ...] = ()
    deviation_expires_at: str | None = None

    def __post_init__(self) -> None:
        require_text(self.conflict_code, "conflict_code")
        require_text(self.description, "description")
        if not self.requirement_codes:
            raise ValueError("冲突至少涉及一个需求")
        if self.conflict_type is ConflictType.REQUIREMENT and len(self.requirement_codes) < 2:
            raise ValueError("需求互斥冲突至少涉及两个需求")
        for code in self.requirement_codes:
            require_text(code, "requirement_codes 元素")
        if self.status is ConflictStatus.DEVIATION:
            if not self.deviation_applicable_customers:
                raise ValueError("接受偏差必须说明适用客户")
            require_iso_date(
                self.deviation_expires_at or "", "deviation_expires_at"
            )
            if not self.resolution_note or not self.resolution_note.strip():
                raise ValueError("接受偏差必须说明处置理由")

    @property
    def is_safety(self) -> bool:
        return self.conflict_type is ConflictType.SAFETY

    @property
    def is_unresolved(self) -> bool:
        return self.status in (ConflictStatus.OPEN, ConflictStatus.ESCALATED)

    def evolve(self, **changes: object) -> "Conflict":
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class DesignDecision:
    """设计决定：把需求落实为具体参数/方案，构成双向追踪的设计侧节点。"""

    decision_code: str
    summary: str
    requirement_codes: tuple[str, ...]
    calculation_refs: tuple[str, ...] = ()    # 关联计算书编号
    conflict_codes: tuple[str, ...] = ()      # 本决定解决/处置的冲突

    def __post_init__(self) -> None:
        require_text(self.decision_code, "decision_code")
        require_text(self.summary, "summary")
        if not self.requirement_codes:
            raise ValueError("设计决定必须至少关联一条需求")

    def fingerprint(self) -> str:
        return stable_fingerprint(self)


@dataclass(frozen=True, slots=True)
class VerificationRecord:
    """计算校核/样机试验记录，挂接到具体需求与设计决定。"""

    verification_code: str
    kind: VerificationKind
    requirement_codes: tuple[str, ...]
    decision_code: str
    result: VerificationResult = VerificationResult.PLANNED
    report_ref: str | None = None
    executed_at: str | None = None
    failure_note: str | None = None

    def __post_init__(self) -> None:
        require_text(self.verification_code, "verification_code")
        if not self.requirement_codes:
            raise ValueError("验证记录必须至少覆盖一条需求")
        require_text(self.decision_code, "decision_code")
        if self.result in (VerificationResult.PASSED, VerificationResult.FAILED):
            require_iso_date(self.executed_at or "", "executed_at")
            require_text(self.report_ref or "", "report_ref")
        if self.result is VerificationResult.FAILED and not (
            self.failure_note and self.failure_note.strip()
        ):
            raise ValueError("验证失败必须填写失败说明")

    @property
    def covers_requirements(self) -> frozenset[str]:
        return frozenset(self.requirement_codes)

    def evolve(self, **changes: object) -> "VerificationRecord":
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class ProductConfiguration:
    """产品配置与设计基线。

    released_at 非空即视为已发布；发布时保存需求/决定/验证快照与指纹，
    序列号交付后以该快照为"原依据"，不随后续变更改写。
    """

    config_code: str
    product_line: str
    name: str
    requirement_codes: tuple[str, ...]
    decision_codes: tuple[str, ...] = ()
    status: ConfigStatus = ConfigStatus.DRAFT
    revision: int = 1
    released_at: str | None = None
    baseline_fingerprint: str | None = None
    # 发布时固化的依据快照（req_code -> 需求当时指纹）
    requirement_basis: dict[str, str] = field(default_factory=dict)
    open_change_codes: tuple[str, ...] = ()   # 已传播到本配置、待消化的变更

    def __post_init__(self) -> None:
        require_text(self.config_code, "config_code")
        require_text(self.product_line, "product_line")
        require_text(self.name, "name")
        if not self.requirement_codes:
            raise ValueError("配置必须至少包含一条有效需求")
        if self.revision < 1:
            raise ValueError("revision 必须大于零")
        if self.released_at is not None:
            require_iso_date(self.released_at, "released_at")
            if not self.baseline_fingerprint:
                raise ValueError("已发布配置必须有基线指纹")

    @property
    def is_released(self) -> bool:
        return self.status in (ConfigStatus.RELEASED, ConfigStatus.FROZEN)

    def evolve(self, **changes: object) -> "ProductConfiguration":
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class DeliveredUnit:
    """已交付序列号，固化交付时的配置版本、需求清单与基线依据。"""

    serial_number: str
    config_code: str
    config_revision: int
    baseline_fingerprint: str
    customer: str
    delivered_at: str
    requirement_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.serial_number, "serial_number")
        require_text(self.config_code, "config_code")
        require_text(self.baseline_fingerprint, "baseline_fingerprint")
        require_text(self.customer, "customer")
        require_iso_date(self.delivered_at, "delivered_at")
        if self.config_revision < 1:
            raise ValueError("config_revision 必须大于零")
        if not self.requirement_codes:
            raise ValueError("序列号必须固化交付时的需求依据清单")


@dataclass(frozen=True, slots=True)
class EngineeringChange:
    """工程变更单：供应商替代、零件停产、法规更新、验证失败、需求撤回。

    传播规则由 impacted_config_codes 落地；已交付序列号不回改，只产生通知/服务公告。
    supersedes_change_codes 支持变更的版本接替。
    """

    change_code: str
    kind: ChangeKind
    summary: str
    requirement_codes: tuple[str, ...]       # 受影响需求
    decision_codes: tuple[str, ...] = ()
    impacted_config_codes: tuple[str, ...] = ()
    notified_serials: tuple[str, ...] = ()   # 已通知的已交付序列号
    effective_at: str | None = None
    supersedes_change_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.change_code, "change_code")
        require_text(self.summary, "summary")
        if not self.requirement_codes:
            raise ValueError("工程变更必须至少关联一条需求")
        if self.effective_at is not None:
            require_iso_date(self.effective_at, "effective_at")

    def fingerprint(self) -> str:
        return stable_fingerprint(self)
