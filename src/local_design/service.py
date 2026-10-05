"""本地化设计闭环应用服务。

在不可变领域模型之上提供状态推进与规则校验：

- 客户声音（带来源）转需求；销售口头承诺无来源不得入基线。
- 澄清 -> 冲突分析 -> 优先级确认，之后需求才能进入配置基线。
- 发布门禁：未解决的安全冲突阻止发布；偏差必须有适用客户与未到期期限。
- 发布时固化基线指纹与需求依据；序列号交付后保持原依据。
- 需求撤回、法规更新、零件停产、供应商替代、验证失败通过工程变更单传播到受影响配置。
- 提供任一配置的有效需求、验证覆盖、开放风险，以及客户声音到设计决定的双向追踪。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .contracts import stable_fingerprint
from .models import (
    ChangeKind,
    ConfigStatus,
    Conflict,
    ConflictStatus,
    ConflictType,
    DeliveredUnit,
    DesignDecision,
    EngineeringChange,
    Priority,
    ProductConfiguration,
    Requirement,
    RequirementStatus,
    Source,
    VerificationKind,
    VerificationRecord,
    VerificationResult,
    VoiceOfCustomer,
)


class LoopError(ValueError):
    """违反闭环业务规则时抛出。"""


def _today() -> str:
    return date.today().isoformat()


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


@dataclass
class TraceNode:
    """双向追踪结果中的一个节点。"""

    kind: str
    code: str
    summary: str


@dataclass
class ConfigurationView:
    """配置全景视图：有效需求、验证覆盖、开放风险、追踪链路。"""

    configuration: ProductConfiguration
    effective_requirements: list[Requirement]
    verification_coverage: dict[str, list[VerificationRecord]]
    uncovered_requirements: list[str]
    open_risks: list[str]
    open_changes: list[EngineeringChange]
    expired_deviations: list[Conflict]
    delivered_serials: list[str]


class LocalDesignLoop:
    """内存态闭环仓储与规则服务（后续可替换为持久化实现）。"""

    def __init__(self) -> None:
        self._voc: dict[str, VoiceOfCustomer] = {}
        self._requirements: dict[str, Requirement] = {}
        self._conflicts: dict[str, Conflict] = {}
        self._decisions: dict[str, DesignDecision] = {}
        self._verifications: dict[str, VerificationRecord] = {}
        self._configurations: dict[str, list[ProductConfiguration]] = {}
        self._units: dict[str, DeliveredUnit] = {}
        self._changes: dict[str, EngineeringChange] = {}
        self._audit_log: list[str] = []

    # ------------------------------------------------------------------ 审计

    @property
    def audit_log(self) -> tuple[str, ...]:
        return tuple(self._audit_log)

    def _audit(self, message: str) -> None:
        self._audit_log.append(f"{_today()} {message}")

    # ------------------------------------------------------ 客户声音与需求

    def register_voc(
        self,
        voc_code: str,
        product_line: str,
        statement: str,
        source: Source,
    ) -> VoiceOfCustomer:
        """登记带来源凭证的客户声音；无来源的口头承诺不予登记转需求。"""
        if voc_code in self._voc:
            raise LoopError(f"客户声音 {voc_code} 已存在")
        voc = VoiceOfCustomer(
            voc_code=voc_code,
            product_line=product_line,
            statement=statement,
            source=source,
        )
        self._voc[voc_code] = voc
        self._audit(f"VOC登记 {voc_code} 来源={source.source_type}:{source.reference}")
        return voc

    def create_requirement(
        self,
        req_code: str,
        product_line: str,
        title: str,
        statement: str,
        source_voc_codes: tuple[str, ...],
        *,
        applicable_customers: tuple[str, ...] = (),
        safety_related: bool = False,
        regulation_ref: str | None = None,
    ) -> Requirement:
        """把客户声音转成带来源的需求；来源缺失或不存在一律拒绝。"""
        if req_code in self._requirements:
            raise LoopError(f"需求 {req_code} 已存在")
        if not source_voc_codes:
            raise LoopError("需求必须带来源，销售口头承诺无来源不得进入设计基线")
        for voc_code in source_voc_codes:
            voc = self._voc.get(voc_code)
            if voc is None:
                raise LoopError(f"来源 {voc_code} 不存在，不能作为需求依据")
            if voc.product_line != product_line:
                raise LoopError(f"来源 {voc_code} 不属于产品线 {product_line}")
        requirement = Requirement(
            req_code=req_code,
            product_line=product_line,
            title=title,
            statement=statement,
            source_voc_codes=tuple(source_voc_codes),
            applicable_customers=tuple(applicable_customers),
            safety_related=safety_related,
            regulation_ref=regulation_ref,
        )
        self._requirements[req_code] = requirement
        self._audit(f"需求创建 {req_code} 来源={','.join(source_voc_codes)}")
        return requirement

    def clarify_requirement(self, req_code: str, notes: str) -> Requirement:
        """澄清需求（环境、载荷、维护条件等），澄清后进入冲突分析与优先级确认。"""
        requirement = self._get_requirement(req_code)
        if requirement.status is RequirementStatus.WITHDRAWN:
            raise LoopError(f"需求 {req_code} 已撤回，不能澄清")
        clarified = requirement.evolve(
            status=RequirementStatus.CLARIFIED,
            clarification_notes=notes,
        )
        self._requirements[req_code] = clarified
        self._audit(f"需求澄清 {req_code}")
        return clarified

    def prioritize_requirement(self, req_code: str, priority: Priority) -> Requirement:
        """确认优先级；只有澄清过的需求才能确认优先级。"""
        requirement = self._get_requirement(req_code)
        if requirement.status is RequirementStatus.DRAFT:
            raise LoopError(f"需求 {req_code} 尚未澄清，不能确认优先级")
        if requirement.status in (
            RequirementStatus.WITHDRAWN,
            RequirementStatus.SUPERSEDED,
        ):
            raise LoopError(f"需求 {req_code} 状态为 {requirement.status.value}，不能确认优先级")
        prioritized = requirement.evolve(
            status=RequirementStatus.PRIORITIZED,
            priority=priority,
        )
        self._requirements[req_code] = prioritized
        self._audit(f"需求优先级确认 {req_code} -> {priority.value}")
        return prioritized

    # ------------------------------------------------------------ 冲突管理

    def register_conflict(
        self,
        conflict_code: str,
        conflict_type: ConflictType,
        requirement_codes: tuple[str, ...],
        description: str,
    ) -> Conflict:
        """登记冲突分析结果，所涉需求必须存在。"""
        if conflict_code in self._conflicts:
            raise LoopError(f"冲突 {conflict_code} 已存在")
        for req_code in requirement_codes:
            self._get_requirement(req_code)
        conflict = Conflict(
            conflict_code=conflict_code,
            conflict_type=conflict_type,
            requirement_codes=tuple(requirement_codes),
            description=description,
        )
        self._conflicts[conflict_code] = conflict
        self._audit(
            f"冲突登记 {conflict_code} 类型={conflict_type.value} 需求={','.join(requirement_codes)}"
        )
        return conflict

    def resolve_conflict(
        self,
        conflict_code: str,
        resolution_note: str,
        decision_ref: str,
    ) -> Conflict:
        """以设计决定解决冲突。"""
        conflict = self._get_conflict(conflict_code)
        if not resolution_note.strip():
            raise LoopError("解决冲突必须给出处置说明")
        self._get_decision(decision_ref)
        resolved = conflict.evolve(
            status=ConflictStatus.RESOLVED,
            resolution_note=resolution_note,
            decision_ref=decision_ref,
        )
        self._conflicts[conflict_code] = resolved
        self._audit(f"冲突解决 {conflict_code} 决定={decision_ref}")
        return resolved

    def accept_deviation(
        self,
        conflict_code: str,
        resolution_note: str,
        applicable_customers: tuple[str, ...],
        expires_at: str,
    ) -> Conflict:
        """接受偏差：必须说明适用客户与期限；仅非安全冲突可接受偏差。"""
        conflict = self._get_conflict(conflict_code)
        if conflict.is_safety:
            raise LoopError(f"安全冲突 {conflict_code} 不允许以偏差放行，必须解决")
        deviation = conflict.evolve(
            status=ConflictStatus.DEVIATION,
            resolution_note=resolution_note,
            deviation_applicable_customers=tuple(applicable_customers),
            deviation_expires_at=expires_at,
        )
        self._conflicts[conflict_code] = deviation
        self._audit(
            f"偏差接受 {conflict_code} 适用客户={','.join(applicable_customers)} 到期={expires_at}"
        )
        return deviation

    # ------------------------------------------------- 设计决定与样机验证

    def add_decision(
        self,
        decision_code: str,
        summary: str,
        requirement_codes: tuple[str, ...],
        *,
        calculation_refs: tuple[str, ...] = (),
        conflict_codes: tuple[str, ...] = (),
    ) -> DesignDecision:
        """登记设计决定（含关联计算书），所涉需求必须存在。"""
        if decision_code in self._decisions:
            raise LoopError(f"设计决定 {decision_code} 已存在")
        for req_code in requirement_codes:
            self._get_requirement(req_code)
        for conflict_code in conflict_codes:
            self._get_conflict(conflict_code)
        decision = DesignDecision(
            decision_code=decision_code,
            summary=summary,
            requirement_codes=tuple(requirement_codes),
            calculation_refs=tuple(calculation_refs),
            conflict_codes=tuple(conflict_codes),
        )
        self._decisions[decision_code] = decision
        self._audit(f"设计决定登记 {decision_code} 需求={','.join(requirement_codes)}")
        return decision

    def add_verification(
        self,
        verification_code: str,
        kind: VerificationKind,
        requirement_codes: tuple[str, ...],
        decision_code: str,
        *,
        result: VerificationResult = VerificationResult.PLANNED,
        report_ref: str | None = None,
        executed_at: str | None = None,
        failure_note: str | None = None,
    ) -> VerificationRecord:
        """登记计算/样机/现场验证记录，失败时自动提出并传播返工变更。"""
        if verification_code in self._verifications:
            raise LoopError(f"验证记录 {verification_code} 已存在")
        self._get_decision(decision_code)
        for req_code in requirement_codes:
            self._get_requirement(req_code)
        record = VerificationRecord(
            verification_code=verification_code,
            kind=kind,
            requirement_codes=tuple(requirement_codes),
            decision_code=decision_code,
            result=result,
            report_ref=report_ref,
            executed_at=executed_at,
            failure_note=failure_note,
        )
        self._verifications[verification_code] = record
        self._audit(
            f"验证登记 {verification_code} 类型={kind.value} 结果={result.value}"
        )
        if result is VerificationResult.FAILED:
            self.propagate_event(
                change_code=f"CHG-VF-{verification_code}",
                kind=ChangeKind.VERIFICATION_FAILURE,
                summary=f"验证 {verification_code} 失败：{failure_note or '见报告'}",
                requirement_codes=record.requirement_codes,
                decision_codes=(decision_code,),
            )
        return record

    def update_verification_result(
        self,
        verification_code: str,
        result: VerificationResult,
        *,
        report_ref: str,
        executed_at: str,
        failure_note: str | None = None,
    ) -> VerificationRecord:
        """更新验证结论；由计划转为失败同样触发传播。"""
        record = self._verifications[verification_code]
        updated = record.evolve(
            result=result,
            report_ref=report_ref,
            executed_at=executed_at,
            failure_note=failure_note,
        )
        self._verifications[verification_code] = updated
        self._audit(f"验证结论更新 {verification_code} -> {result.value}")
        if result is VerificationResult.FAILED:
            self.propagate_event(
                change_code=f"CHG-VF-{verification_code}",
                kind=ChangeKind.VERIFICATION_FAILURE,
                summary=f"验证 {verification_code} 失败：{failure_note or '见报告'}",
                requirement_codes=updated.requirement_codes,
                decision_codes=(updated.decision_code,),
            )
        return updated

    # ------------------------------------------------------- 配置与发布门禁

    def draft_configuration(
        self,
        config_code: str,
        product_line: str,
        name: str,
        requirement_codes: tuple[str, ...],
        decision_codes: tuple[str, ...] = (),
    ) -> ProductConfiguration:
        """创建产品配置草案；需求须已完成澄清与优先级确认。"""
        if config_code in self._configurations:
            raise LoopError(f"配置 {config_code} 已存在，请使用新版本")
        codes = tuple(dict.fromkeys(requirement_codes))
        self._assert_requirements_ready_for_baseline(codes)
        for decision_code in decision_codes:
            self._get_decision(decision_code)
        config = ProductConfiguration(
            config_code=config_code,
            product_line=product_line,
            name=name,
            requirement_codes=codes,
            decision_codes=tuple(decision_codes),
        )
        self._configurations[config_code] = [config]
        self._audit(f"配置草案创建 {config_code} 需求={','.join(codes)}")
        return config

    def _assert_requirements_ready_for_baseline(self, codes: tuple[str, ...]) -> None:
        for req_code in codes:
            requirement = self._get_requirement(req_code)
            if requirement.status in (
                RequirementStatus.WITHDRAWN,
                RequirementStatus.SUPERSEDED,
            ):
                raise LoopError(f"需求 {req_code} 已失效（{requirement.status.value}），不能进入基线")
            if requirement.status is RequirementStatus.DRAFT:
                raise LoopError(f"需求 {req_code} 尚未澄清，不能进入基线")
            if requirement.priority is None:
                raise LoopError(f"需求 {req_code} 尚未确认优先级，不能进入基线")

    def release_configuration(
        self,
        config_code: str,
        released_at: str | None = None,
    ) -> ProductConfiguration:
        """发布门禁检查并固化设计基线。

        - 未解决的安全冲突不得发布；
        - 偏差必须限定适用客户且未到期；
        - 发布时保存需求依据指纹，供交付序列号保持原依据。
        """
        config = self.get_latest_configuration(config_code)
        if config.is_released:
            raise LoopError(f"配置 {config_code} 已发布，变更须创建新版本")
        self._assert_requirements_ready_for_baseline(config.requirement_codes)

        blockers: list[str] = []
        release_day = _parse_day(released_at or _today())
        for conflict in self._conflicts_for_requirements(config.requirement_codes):
            if conflict.is_safety and conflict.is_unresolved:
                blockers.append(
                    f"安全冲突 {conflict.conflict_code} 未解决：{conflict.description}"
                )
            if conflict.status is ConflictStatus.DEVIATION:
                if conflict.deviation_expires_at and _parse_day(
                    conflict.deviation_expires_at
                ) < release_day:
                    blockers.append(
                        f"冲突 {conflict.conflict_code} 的偏差已于 {conflict.deviation_expires_at} 到期"
                    )
                if not _deviation_covers_config(conflict, config, self._requirements):
                    blockers.append(
                        f"冲突 {conflict.conflict_code} 的偏差适用客户未覆盖本配置全部需求范围"
                    )
        if blockers:
            joined = "；".join(blockers)
            raise LoopError(f"配置 {config_code} 未通过发布门禁：{joined}")

        basis = {
            code: self._get_requirement(code).fingerprint()
            for code in config.requirement_codes
        }
        released = config.evolve(
            status=ConfigStatus.RELEASED,
            released_at=released_at or _today(),
            requirement_basis=basis,
            baseline_fingerprint=stable_fingerprint(
                {
                    "config": config.config_code,
                    "revision": config.revision,
                    "requirements": basis,
                    "decisions": config.decision_codes,
                }
            ),
        )
        self._configurations[config_code][-1] = released
        for req_code in config.requirement_codes:
            requirement = self._get_requirement(req_code)
            if requirement.status is RequirementStatus.PRIORITIZED:
                self._requirements[req_code] = requirement.evolve(
                    status=RequirementStatus.BASELINED
                )
        self._audit(
            f"配置发布 {config_code} rev={config.revision} 指纹={released.baseline_fingerprint[:12]}"
        )
        return released

    def revise_configuration(
        self,
        config_code: str,
        requirement_codes: tuple[str, ...] | None = None,
        decision_codes: tuple[str, ...] | None = None,
    ) -> ProductConfiguration:
        """基于已发布配置创建新版本以消化变更；新版本须重新通过发布门禁。"""
        current = self.get_latest_configuration(config_code)
        if not current.is_released:
            raise LoopError(f"配置 {config_code} 尚未发布，无需创建新版本")
        codes = tuple(dict.fromkeys(requirement_codes or current.requirement_codes))
        self._assert_requirements_ready_for_baseline(codes)
        new_decisions = tuple(decision_codes or current.decision_codes)
        for decision_code in new_decisions:
            self._get_decision(decision_code)
        revision = current.evolve(
            requirement_codes=codes,
            decision_codes=new_decisions,
            status=ConfigStatus.DRAFT,
            revision=current.revision + 1,
            released_at=None,
            baseline_fingerprint=None,
            requirement_basis={},
            open_change_codes=(),
        )
        self._configurations[config_code].append(revision)
        self._audit(f"配置新版本 {config_code} rev={revision.revision}")
        return revision

    def deliver_unit(
        self,
        serial_number: str,
        config_code: str,
        customer: str,
        delivered_at: str,
    ) -> DeliveredUnit:
        """按已发布基线交付序列号，并把该配置冻结；序列号固化交付时依据。"""
        if serial_number in self._units:
            raise LoopError(f"序列号 {serial_number} 已交付")
        config = self.get_latest_configuration(config_code)
        if not config.is_released:
            raise LoopError(f"配置 {config_code} 未发布，不能交付")
        if not config.baseline_fingerprint:
            raise LoopError(f"配置 {config_code} 缺少基线指纹")
        unit = DeliveredUnit(
            serial_number=serial_number,
            config_code=config_code,
            config_revision=config.revision,
            baseline_fingerprint=config.baseline_fingerprint,
            customer=customer,
            delivered_at=delivered_at,
            requirement_codes=config.requirement_codes,
        )
        self._units[serial_number] = unit
        if config.status is ConfigStatus.RELEASED:
            frozen = config.evolve(status=ConfigStatus.FROZEN)
            self._configurations[config_code][-1] = frozen
        self._audit(
            f"序列号交付 {serial_number} 配置={config_code} rev={config.revision}"
        )
        return unit

    # --------------------------------------------------------- 变更与传播

    def withdraw_requirement(
        self,
        req_code: str,
        summary: str,
        *,
        effective_at: str | None = None,
    ) -> EngineeringChange:
        """需求撤回：标记撤回并传播到所有含该需求的配置。"""
        requirement = self._get_requirement(req_code)
        if requirement.status in (
            RequirementStatus.WITHDRAWN,
            RequirementStatus.SUPERSEDED,
        ):
            raise LoopError(f"需求 {req_code} 已失效")
        impacted = [
            code
            for code, revisions in self._configurations.items()
            if req_code in revisions[-1].requirement_codes
        ]
        change = self.propagate_event(
            change_code=f"CHG-RW-{req_code}",
            kind=ChangeKind.REQUIREMENT_WITHDRAWAL,
            summary=summary,
            requirement_codes=(req_code,),
            impacted_config_codes=tuple(impacted),
            effective_at=effective_at,
        )
        self._requirements[req_code] = requirement.evolve(
            status=RequirementStatus.WITHDRAWN
        )
        self._audit(f"需求撤回 {req_code}")
        return change

    def update_regulation(
        self,
        old_req_code: str,
        new_req_code: str,
        title: str,
        statement: str,
        summary: str,
        *,
        regulation_ref: str,
        priority: Priority,
        effective_at: str,
    ) -> EngineeringChange:
        """法规更新：以新需求接替旧需求，旧需求标记 SUPERSEDED 并传播受影响配置。"""
        old = self._get_requirement(old_req_code)
        impacted = [
            code
            for code, revisions in self._configurations.items()
            if old_req_code in revisions[-1].requirement_codes
        ]
        new = self.create_requirement(
            req_code=new_req_code,
            product_line=old.product_line,
            title=title,
            statement=statement,
            source_voc_codes=old.source_voc_codes,
            applicable_customers=old.applicable_customers,
            safety_related=old.safety_related,
            regulation_ref=regulation_ref,
        )
        new = self.clarify_requirement(
            new.req_code, f"法规更新接替 {old_req_code}（{regulation_ref}）"
        )
        new = self.prioritize_requirement(new.req_code, priority)
        self._requirements[new_req_code] = new.evolve(supersedes=old_req_code)
        change = self.propagate_event(
            change_code=f"CHG-RU-{new_req_code}",
            kind=ChangeKind.REGULATION_UPDATE,
            summary=summary,
            requirement_codes=(old_req_code, new_req_code),
            impacted_config_codes=tuple(impacted),
            effective_at=effective_at,
        )
        self._requirements[old_req_code] = old.evolve(
            status=RequirementStatus.SUPERSEDED,
            supersedes=old.supersedes,
        )
        self._audit(f"法规更新 {old_req_code} -> {new_req_code}")
        return change

    def propagate_event(
        self,
        change_code: str,
        kind: ChangeKind,
        summary: str,
        requirement_codes: tuple[str, ...],
        *,
        decision_codes: tuple[str, ...] = (),
        impacted_config_codes: tuple[str, ...] | None = None,
        effective_at: str | None = None,
    ) -> EngineeringChange:
        """登记工程变更单并传播到受影响配置（已交付序列号保持原依据，只记录通知）。"""
        if change_code in self._changes:
            raise LoopError(f"工程变更 {change_code} 已存在")
        for req_code in requirement_codes:
            self._get_requirement(req_code)
        if impacted_config_codes is None:
            impacted_config_codes = tuple(
                code
                for code, revisions in self._configurations.items()
                if set(requirement_codes) & set(revisions[-1].requirement_codes)
            )
        for config_code in impacted_config_codes:
            if config_code not in self._configurations:
                raise LoopError(f"受影响配置 {config_code} 不存在")

        notified_serials = tuple(
            serial
            for serial, unit in self._units.items()
            if unit.config_code in impacted_config_codes
        )
        change = EngineeringChange(
            change_code=change_code,
            kind=kind,
            summary=summary,
            requirement_codes=tuple(requirement_codes),
            decision_codes=tuple(decision_codes),
            impacted_config_codes=tuple(impacted_config_codes),
            notified_serials=notified_serials,
            effective_at=effective_at,
        )
        self._changes[change_code] = change

        for config_code in impacted_config_codes:
            revisions = self._configurations[config_code]
            current = revisions[-1]
            if change_code not in current.open_change_codes:
                revisions[-1] = current.evolve(
                    open_change_codes=current.open_change_codes + (change_code,)
                )
        self._audit(
            f"变更传播 {change_code} 类型={kind.value} 配置={','.join(impacted_config_codes) or '无'} "
            f"通知序列号={','.join(notified_serials) or '无'}"
        )
        return change

    # ------------------------------------------------------------- 查询视图

    def get_voc(self, voc_code: str) -> VoiceOfCustomer:
        return self._voc[voc_code]

    def get_requirement(self, req_code: str) -> Requirement:
        return self._get_requirement(req_code)

    def get_conflict(self, conflict_code: str) -> Conflict:
        return self._get_conflict(conflict_code)

    def get_change(self, change_code: str) -> EngineeringChange:
        return self._changes[change_code]

    def get_latest_configuration(self, config_code: str) -> ProductConfiguration:
        revisions = self._configurations.get(config_code)
        if not revisions:
            raise LoopError(f"配置 {config_code} 不存在")
        return revisions[-1]

    def get_unit(self, serial_number: str) -> DeliveredUnit:
        unit = self._units.get(serial_number)
        if unit is None:
            raise LoopError(f"序列号 {serial_number} 不存在")
        return unit

    def configuration_view(
        self, config_code: str, *, on_date: str | None = None
    ) -> ConfigurationView:
        """汇总配置的有效需求、验证覆盖、开放风险与已交付序列号。"""
        config = self.get_latest_configuration(config_code)
        effective: list[Requirement] = []
        for req_code in config.requirement_codes:
            requirement = self._requirements[req_code]
            if requirement.status in (
                RequirementStatus.WITHDRAWN,
                RequirementStatus.SUPERSEDED,
            ):
                continue
            effective.append(requirement)

        coverage: dict[str, list[VerificationRecord]] = {
            req.req_code: [] for req in effective
        }
        for record in self._verifications.values():
            for req_code in record.requirement_codes:
                if req_code in coverage:
                    coverage[req_code].append(record)
        uncovered = sorted(
            req_code
            for req_code, records in coverage.items()
            if not any(r.result is VerificationResult.PASSED for r in records)
        )

        risks: list[str] = []
        expired: list[Conflict] = []
        today_obj = _parse_day(on_date or _today())
        for conflict in self._conflicts_for_requirements(config.requirement_codes):
            if conflict.is_safety and conflict.is_unresolved:
                risks.append(f"安全冲突未解决：{conflict.conflict_code} {conflict.description}")
            if conflict.status is ConflictStatus.DEVIATION:
                if conflict.deviation_expires_at and _parse_day(
                    conflict.deviation_expires_at
                ) < today_obj:
                    expired.append(conflict)
                    risks.append(
                        f"偏差已到期：{conflict.conflict_code}（到期日 {conflict.deviation_expires_at}）"
                    )
                else:
                    risks.append(
                        f"在用偏差：{conflict.conflict_code}（到期日 {conflict.deviation_expires_at}，"
                        f"适用客户 {','.join(conflict.deviation_applicable_customers)}）"
                    )
        for req_code in uncovered:
            requirement = self._requirements[req_code]
            if requirement.safety_related:
                risks.append(f"安全相关需求 {req_code} 尚无通过结论的验证覆盖")

        open_changes = [
            self._changes[code] for code in config.open_change_codes if code in self._changes
        ]
        for change in open_changes:
            failed = [
                v
                for v in self._verifications.values()
                if change.change_code == f"CHG-VF-{v.verification_code}"
                and v.result is VerificationResult.FAILED
            ]
            if failed:
                risks.append(
                    f"验证失败待返工：{failed[0].verification_code}（{change.change_code}）"
                )
            else:
                risks.append(f"待消化变更：{change.change_code} {change.kind.value} {change.summary}")

        serials = sorted(
            serial
            for serial, unit in self._units.items()
            if unit.config_code == config_code
        )
        return ConfigurationView(
            configuration=config,
            effective_requirements=effective,
            verification_coverage=coverage,
            uncovered_requirements=uncovered,
            open_risks=risks,
            open_changes=open_changes,
            expired_deviations=expired,
            delivered_serials=serials,
        )

    def effective_requirements(self, config_code: str) -> list[Requirement]:
        """配置当前有效需求（排除已撤回/被替代）。"""
        return self.configuration_view(config_code).effective_requirements

    def verification_coverage(
        self, config_code: str
    ) -> dict[str, list[VerificationRecord]]:
        return self.configuration_view(config_code).verification_coverage

    def open_risks(self, config_code: str) -> list[str]:
        return self.configuration_view(config_code).open_risks

    # ------------------------------------------------------- 双向追踪

    def trace_from_voc(self, voc_code: str) -> list[TraceNode]:
        """正向追踪：客户声音 -> 需求 -> 设计决定/计算 -> 验证 -> 配置/序列号。"""
        if voc_code not in self._voc:
            raise LoopError(f"客户声音 {voc_code} 不存在")
        nodes: list[TraceNode] = [
            TraceNode("voc", voc_code, self._voc[voc_code].statement)
        ]
        req_codes = [
            code
            for code, req in self._requirements.items()
            if voc_code in req.source_voc_codes
        ]
        for req_code in sorted(req_codes):
            req = self._requirements[req_code]
            nodes.append(TraceNode("requirement", req_code, f"[{req.status.value}] {req.title}"))
        decision_codes = sorted(
            {
                d.decision_code
                for d in self._decisions.values()
                if set(d.requirement_codes) & set(req_codes)
            }
        )
        for decision_code in decision_codes:
            decision = self._decisions[decision_code]
            nodes.append(
                TraceNode(
                    "decision",
                    decision_code,
                    f"{decision.summary} 计算={','.join(decision.calculation_refs) or '无'}",
                )
            )
        for record in sorted(self._verifications.values(), key=lambda r: r.verification_code):
            if set(record.requirement_codes) & set(req_codes):
                nodes.append(
                    TraceNode(
                        "verification",
                        record.verification_code,
                        f"{record.kind.value}:{record.result.value}",
                    )
                )
        for config_code, revisions in sorted(self._configurations.items()):
            latest = revisions[-1]
            if set(latest.requirement_codes) & set(req_codes):
                nodes.append(
                    TraceNode(
                        "configuration",
                        f"{config_code}@rev{latest.revision}",
                        f"基线={latest.baseline_fingerprint[:12] if latest.baseline_fingerprint else '未发布'}",
                    )
                )
        for serial, unit in sorted(self._units.items()):
            if set(unit.requirement_codes) & set(req_codes):
                nodes.append(TraceNode("serial", serial, f"交付客户={unit.customer}"))
        return nodes

    def trace_to_origin(self, req_or_decision_code: str) -> list[TraceNode]:
        """反向追踪：需求或设计决定 -> 来源客户声音（含来源凭证）。"""
        req_codes: list[str] = []
        if req_or_decision_code in self._requirements:
            req_codes.append(req_or_decision_code)
        decision = self._decisions.get(req_or_decision_code)
        if decision is not None:
            nodes = [TraceNode("decision", decision.decision_code, decision.summary)]
            req_codes.extend(decision.requirement_codes)
        elif not req_codes:
            raise LoopError(f"{req_or_decision_code} 既不是需求也不是设计决定")
        else:
            nodes = []
        for req_code in dict.fromkeys(req_codes):
            req = self._requirements[req_code]
            nodes.append(TraceNode("requirement", req_code, req.title))
            for voc_code in req.source_voc_codes:
                voc = self._voc[voc_code]
                source = voc.source
                nodes.append(
                    TraceNode(
                        "voc",
                        voc_code,
                        f"{voc.statement} | 来源 {source.source_type}:{source.reference} "
                        f"客户={source.customer} 日期={source.recorded_at}",
                    )
                )
        return nodes

    # ------------------------------------------------------------- 内部辅助

    def _get_requirement(self, req_code: str) -> Requirement:
        requirement = self._requirements.get(req_code)
        if requirement is None:
            raise LoopError(f"需求 {req_code} 不存在")
        return requirement

    def _get_conflict(self, conflict_code: str) -> Conflict:
        conflict = self._conflicts.get(conflict_code)
        if conflict is None:
            raise LoopError(f"冲突 {conflict_code} 不存在")
        return conflict

    def _get_decision(self, decision_code: str) -> DesignDecision:
        decision = self._decisions.get(decision_code)
        if decision is None:
            raise LoopError(f"设计决定 {decision_code} 不存在")
        return decision

    def _conflicts_for_requirements(
        self, req_codes: tuple[str, ...]
    ) -> list[Conflict]:
        wanted = set(req_codes)
        return [
            conflict
            for conflict in self._conflicts.values()
            if set(conflict.requirement_codes) & wanted
        ]


def _deviation_covers_config(
    conflict: Conflict,
    config: ProductConfiguration,
    requirements: dict[str, Requirement],
) -> bool:
    """偏差适用客户必须覆盖配置中受冲突影响需求的全部适用客户。

    通用需求（applicable_customers 为空，表示全客户适用）不允许通过客户限定的偏差放行。
    """
    allowed = set(conflict.deviation_applicable_customers)
    for req_code in conflict.requirement_codes:
        if req_code not in config.requirement_codes:
            continue
        requirement = requirements.get(req_code)
        if requirement is None:
            return False
        if not requirement.applicable_customers:
            return False
        if not set(requirement.applicable_customers) <= allowed:
            return False
    return True
