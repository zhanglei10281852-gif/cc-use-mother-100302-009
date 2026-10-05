"""本地化设计闭环应用服务。

职责：
- 客户声音登记与来源校验（销售口头承诺必须有可核验凭证）
- 需求澄清、优先级确认、撤回与影响传播
- 冲突登记、解决与偏差接受（安全冲突不得作为偏差接受）
- 产品配置、设计决定、计算、样机验证、供应商替代、工程变更管理
- 设计基线发布门禁与不可变快照
- 已交付序列号的依据冻结
- 需求撤回/法规更新/零件停产/验证失败四类事件传播
- 有效需求、验证覆盖、开放风险与双向追踪查询

服务自身只使用内存存储与标准库，便于后续替换为持久化实现。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from .model import (
    Baseline,
    Calculation,
    ChangeStatus,
    Conflict,
    ConflictKind,
    ConflictStatus,
    CustomerVoice,
    DesignDecision,
    EngineeringChange,
    EventKind,
    EventStatus,
    ImpactEvent,
    PartUsage,
    Priority,
    ProductConfiguration,
    PrototypeValidation,
    ReleasedUnit,
    Requirement,
    RequirementStatus,
    SourceKind,
    SubstitutionStatus,
    SupplierSubstitution,
    VerificationStatus,
)


class ReleaseGateError(ValueError):
    """设计基线发布门禁未通过。"""


class LocalDesignService:
    def __init__(self) -> None:
        self.voices: dict[str, CustomerVoice] = {}
        self.requirements: dict[str, Requirement] = {}
        self._requirement_versions: dict[str, list[Requirement]] = {}
        self.conflicts: dict[str, Conflict] = {}
        self.configurations: dict[str, ProductConfiguration] = {}
        self.decisions: dict[str, DesignDecision] = {}
        self.calculations: dict[str, Calculation] = {}
        self.validations: dict[str, PrototypeValidation] = {}
        self.part_usages: list[PartUsage] = []
        self.substitutions: dict[str, SupplierSubstitution] = {}
        self.changes: dict[str, EngineeringChange] = {}
        self.baselines: dict[str, Baseline] = {}
        self.units: dict[str, ReleasedUnit] = {}
        self.events: dict[str, ImpactEvent] = {}
        # 基线发布后新传播到配置、尚未通过再发布消解的事件
        self._stale_since: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # 客户声音
    # ------------------------------------------------------------------
    def record_voice(
        self,
        voice_id: str,
        case_code: str,
        source_kind: SourceKind,
        source_ref: str,
        customer: str,
        recorded_at: date,
        raw_text: str,
        captured_by: str,
    ) -> CustomerVoice:
        """登记客户声音。销售口头承诺必须填写来源凭证（会议纪要/承诺记录）。"""
        if voice_id in self.voices:
            raise ValueError(f"客户声音 {voice_id} 已存在")
        if not source_ref.strip():
            raise ValueError("来源凭证不能为空：口头承诺也必须落到可核验记录")
        if not raw_text.strip() or not captured_by.strip():
            raise ValueError("客户声音内容与登记人不能为空")
        voice = CustomerVoice(
            voice_id=voice_id,
            case_code=case_code,
            source_kind=source_kind,
            source_ref=source_ref.strip(),
            customer=customer,
            recorded_at=recorded_at,
            raw_text=raw_text,
            captured_by=captured_by,
        )
        self.voices[voice_id] = voice
        return voice

    # ------------------------------------------------------------------
    # 需求：创建、澄清、优先级确认、撤回
    # ------------------------------------------------------------------
    def create_requirement(
        self,
        req_id: str,
        case_code: str,
        voice_ids: tuple[str, ...] | list[str],
        title: str,
        statement: str,
        categories: tuple[str, ...] | list[str],
        target_customers: tuple[str, ...] | list[str],
    ) -> Requirement:
        """把带来源的客户声音转成需求；没有来源不得建需求。"""
        if req_id in self.requirements:
            raise ValueError(f"需求 {req_id} 已存在")
        if not voice_ids:
            raise ValueError("需求必须至少关联一条客户声音来源")
        voices: list[CustomerVoice] = []
        for vid in voice_ids:
            voice = self.voices.get(vid)
            if voice is None:
                raise ValueError(f"客户声音 {vid} 不存在，需求来源不可追溯")
            if voice.case_code != case_code:
                raise ValueError(f"客户声音 {vid} 不属于闭环 {case_code}")
            voices.append(voice)
        if not statement.strip():
            raise ValueError("需求陈述不能为空")
        requirement = Requirement(
            req_id=req_id,
            case_code=case_code,
            voice_ids=tuple(voice_ids),
            title=title,
            statement=statement,
            categories=tuple(categories),
            target_customers=tuple(target_customers),
            priority=None,
            status=RequirementStatus.DRAFT,
        )
        self.requirements[req_id] = requirement
        self._requirement_versions[req_id] = [requirement]
        return requirement

    def clarify_requirement(self, req_id: str, statement: str | None = None) -> Requirement:
        """澄清需求，澄清后进入待确认优先级状态。"""
        current = self._requirement(req_id)
        if current.status is RequirementStatus.WITHDRAWN:
            raise ValueError(f"需求 {req_id} 已撤回，不能澄清")
        changes: dict[str, object] = {"status": RequirementStatus.CLARIFIED}
        if statement is not None:
            if not statement.strip():
                raise ValueError("澄清后的需求陈述不能为空")
            changes["statement"] = statement
            changes["revision"] = current.revision + 1
        return self._evolve_requirement(current, **changes)

    def confirm_requirement(self, req_id: str, priority: Priority) -> Requirement:
        """确认优先级，只有已澄清需求可以确认，确认后方可进入配置与基线。"""
        current = self._requirement(req_id)
        if current.status is RequirementStatus.WITHDRAWN:
            raise ValueError(f"需求 {req_id} 已撤回，不能确认")
        if current.status is RequirementStatus.DRAFT:
            raise ValueError(f"需求 {req_id} 尚未澄清，不能确认优先级")
        return self._evolve_requirement(
            current, status=RequirementStatus.CONFIRMED, priority=priority
        )

    def withdraw_requirement(self, req_id: str, reason: str, occurred_at: date) -> ImpactEvent:
        """撤回需求并传播到所有引用它的配置与已交付序列号。"""
        current = self._requirement(req_id)
        if not reason.strip():
            raise ValueError("撤回原因不能为空")
        evolved = self._evolve_requirement(
            current,
            status=RequirementStatus.WITHDRAWN,
            withdrawn_reason=reason,
            revision=current.revision + 1,
        )
        affected_configs = [
            cid for cid, cfg in self.configurations.items() if req_id in cfg.requirement_ids
        ]
        # 从受影响配置的现行需求清单中移除撤回项，已发布基线内的快照保持不变
        for cid in affected_configs:
            cfg = self.configurations[cid]
            self.configurations[cid] = replace(
                cfg,
                requirement_ids=tuple(r for r in cfg.requirement_ids if r != req_id),
            )
        return self._propagate(
            EventKind.REQUIREMENT_WITHDRAWN,
            detail=f"需求 {req_id} 撤回：{reason}",
            ref_id=req_id,
            case_code=current.case_code,
            occurred_at=occurred_at,
            affected_config_ids=tuple(affected_configs),
        )

    # ------------------------------------------------------------------
    # 产品配置
    # ------------------------------------------------------------------
    def create_configuration(
        self, config_id: str, case_code: str, name: str, customers: tuple[str, ...] | list[str]
    ) -> ProductConfiguration:
        if config_id in self.configurations:
            raise ValueError(f"配置 {config_id} 已存在")
        config = ProductConfiguration(
            config_id=config_id,
            case_code=case_code,
            name=name,
            customers=tuple(customers),
        )
        self.configurations[config_id] = config
        return config

    def assign_requirements(self, config_id: str, req_ids: tuple[str, ...] | list[str]) -> ProductConfiguration:
        """把已确认需求纳入配置；未确认需求不得进入设计基线范围。"""
        config = self._configuration(config_id)
        for req_id in req_ids:
            requirement = self.requirements.get(req_id)
            if requirement is None:
                raise ValueError(f"需求 {req_id} 不存在")
            if requirement.status is not RequirementStatus.CONFIRMED:
                raise ValueError(f"需求 {req_id} 状态为 {requirement.status.value}，未确认不能纳入配置")
            if requirement.case_code != config.case_code:
                raise ValueError(f"需求 {req_id} 不属于闭环 {config.case_code}")
        merged = tuple(dict.fromkeys((*config.requirement_ids, *req_ids)))
        config = replace(config, requirement_ids=merged)
        self.configurations[config_id] = config
        return config

    # ------------------------------------------------------------------
    # 冲突分析与解决
    # ------------------------------------------------------------------
    def register_conflict(
        self,
        conflict_id: str,
        kind: ConflictKind,
        is_safety: bool,
        description: str,
        requirement_ids: tuple[str, ...] | list[str] = (),
        config_ids: tuple[str, ...] | list[str] = (),
    ) -> Conflict:
        if conflict_id in self.conflicts:
            raise ValueError(f"冲突 {conflict_id} 已存在")
        for req_id in requirement_ids:
            self._requirement(req_id)
        for config_id in config_ids:
            self._configuration(config_id)
        case_code = self._conflict_case_code(tuple(requirement_ids), tuple(config_ids))
        conflict = Conflict(
            conflict_id=conflict_id,
            case_code=case_code,
            kind=kind,
            is_safety=is_safety,
            description=description,
            requirement_ids=tuple(requirement_ids),
            config_ids=tuple(config_ids),
        )
        self.conflicts[conflict_id] = conflict
        return conflict

    def record_decision(
        self,
        decision_id: str,
        config_id: str,
        title: str,
        chosen_option: str,
        rejected_options: tuple[str, ...] | list[str],
        linked_requirement_ids: tuple[str, ...] | list[str],
        linked_conflict_id: str | None = None,
        decided_at: date | None = None,
    ) -> DesignDecision:
        """记录设计决定，构成需求/冲突到设计选择的追踪环节。"""
        config = self._configuration(config_id)
        for req_id in linked_requirement_ids:
            self._requirement(req_id)
        if linked_conflict_id is not None:
            self._conflict(linked_conflict_id)
        decision = DesignDecision(
            decision_id=decision_id,
            case_code=config.case_code,
            config_id=config_id,
            title=title,
            chosen_option=chosen_option,
            rejected_options=tuple(rejected_options),
            linked_requirement_ids=tuple(linked_requirement_ids),
            linked_conflict_id=linked_conflict_id,
            decided_at=decided_at,
        )
        self.decisions[decision_id] = decision
        return decision

    def resolve_conflict(self, conflict_id: str, decision_id: str) -> Conflict:
        """以设计决定解决冲突。安全冲突只能被真正解决，不能转为偏差。"""
        conflict = self._conflict(conflict_id)
        if conflict.status is not ConflictStatus.OPEN:
            raise ValueError(f"冲突 {conflict_id} 已不再开放")
        decision = self._decision(decision_id)
        if decision.linked_conflict_id != conflict_id:
            raise ValueError(f"决定 {decision_id} 未关联冲突 {conflict_id}")
        conflict = replace(
            conflict,
            status=ConflictStatus.RESOLVED,
            resolution_decision_id=decision_id,
        )
        self.conflicts[conflict_id] = conflict
        return conflict

    def accept_deviation(
        self,
        conflict_id: str,
        justification: str,
        applicable_customers: tuple[str, ...] | list[str],
        expiry: date,
        today: date,
    ) -> Conflict:
        """对非安全冲突接受偏差，必须说明适用客户与期限。"""
        conflict = self._conflict(conflict_id)
        if conflict.is_safety:
            raise ValueError(f"冲突 {conflict_id} 属于安全冲突，不得作为偏差接受，必须解决后才能发布")
        if conflict.status is not ConflictStatus.OPEN:
            raise ValueError(f"冲突 {conflict_id} 已不再开放")
        if not applicable_customers:
            raise ValueError("接受偏差必须说明适用客户")
        if expiry <= today:
            raise ValueError("偏差期限必须晚于当前日期")
        if not justification.strip():
            raise ValueError("接受偏差必须给出理由")
        conflict = replace(
            conflict,
            status=ConflictStatus.ACCEPTED_DEVIATION,
            justification=justification,
            deviation_customers=tuple(applicable_customers),
            deviation_expiry=expiry,
        )
        self.conflicts[conflict_id] = conflict
        return conflict

    # ------------------------------------------------------------------
    # 计算、样机验证
    # ------------------------------------------------------------------
    def record_calculation(
        self,
        calc_id: str,
        config_id: str,
        name: str,
        standard: str,
        requirement_ids: tuple[str, ...] | list[str],
        status: VerificationStatus,
        result_summary: str,
    ) -> Calculation:
        self._configuration(config_id)
        for req_id in requirement_ids:
            self._requirement(req_id)
        calc = Calculation(
            calc_id=calc_id,
            config_id=config_id,
            name=name,
            standard=standard,
            requirement_ids=tuple(requirement_ids),
            status=status,
            result_summary=result_summary,
        )
        self.calculations[calc_id] = calc
        return calc

    def record_validation(
        self,
        validation_id: str,
        config_id: str,
        prototype_code: str,
        requirement_ids: tuple[str, ...] | list[str],
        status: VerificationStatus,
        result_summary: str,
        occurred_at: date,
    ) -> PrototypeValidation:
        """登记样机验证结果；验证失败自动登记冲突并向该配置传播。"""
        self._configuration(config_id)
        for req_id in requirement_ids:
            self._requirement(req_id)
        validation = PrototypeValidation(
            validation_id=validation_id,
            config_id=config_id,
            prototype_code=prototype_code,
            requirement_ids=tuple(requirement_ids),
            status=status,
            result_summary=result_summary,
        )
        self.validations[validation_id] = validation
        if status is VerificationStatus.FAILED:
            config = self._configuration(config_id)
            conflict_id = f"conflict-vf-{validation_id}"
            if conflict_id not in self.conflicts:
                self.register_conflict(
                    conflict_id=conflict_id,
                    kind=ConflictKind.VALIDATION_FAILURE,
                    is_safety=self._requirements_touch_safety(requirement_ids),
                    description=f"样机 {prototype_code} 验证 {validation_id} 未通过：{result_summary}",
                    requirement_ids=tuple(requirement_ids),
                    config_ids=(config_id,),
                )
            self._propagate(
                EventKind.VALIDATION_FAILURE,
                detail=f"验证 {validation_id} 失败：{result_summary}",
                ref_id=validation_id,
                case_code=config.case_code,
                occurred_at=occurred_at,
                affected_config_ids=(config_id,),
            )
        return validation

    # ------------------------------------------------------------------
    # 零件、供应商替代、工程变更
    # ------------------------------------------------------------------
    def add_part_usage(self, config_id: str, part_number: str, part_name: str, supplier: str) -> PartUsage:
        self._configuration(config_id)
        usage = PartUsage(
            config_id=config_id, part_number=part_number, part_name=part_name, supplier=supplier
        )
        self.part_usages.append(usage)
        return usage

    def propose_substitution(
        self,
        substitution_id: str,
        config_id: str,
        part_number: str,
        new_supplier: str,
        reason: str,
    ) -> SupplierSubstitution:
        config = self._configuration(config_id)
        usages = [u for u in self.part_usages if u.config_id == config_id and u.part_number == part_number]
        if not usages:
            raise ValueError(f"配置 {config_id} 未使用零件 {part_number}，不能发起替代")
        substitution = SupplierSubstitution(
            substitution_id=substitution_id,
            config_id=config_id,
            part_number=part_number,
            old_supplier=usages[0].supplier,
            new_supplier=new_supplier,
            reason=reason,
        )
        self.substitutions[substitution_id] = substitution
        return substitution

    def approve_substitution(self, substitution_id: str, change_id: str | None = None) -> SupplierSubstitution:
        substitution = self._substitution(substitution_id)
        if substitution.status is not SubstitutionStatus.PROPOSED:
            raise ValueError(f"替代 {substitution_id} 状态为 {substitution.status.value}，不能批准")
        return self._replace_substitution(
            substitution,
            status=SubstitutionStatus.APPROVED,
            linked_change_id=change_id,
        )

    def reject_substitution(self, substitution_id: str) -> SupplierSubstitution:
        substitution = self._substitution(substitution_id)
        return self._replace_substitution(substitution, status=SubstitutionStatus.REJECTED)

    def record_change(
        self,
        change_id: str,
        config_id: str,
        title: str,
        trigger_ref: str,
        linked_requirement_ids: tuple[str, ...] | list[str] = (),
        status: ChangeStatus = ChangeStatus.DRAFT,
    ) -> EngineeringChange:
        config = self._configuration(config_id)
        change = EngineeringChange(
            change_id=change_id,
            case_code=config.case_code,
            config_id=config_id,
            title=title,
            trigger_ref=trigger_ref,
            linked_requirement_ids=tuple(linked_requirement_ids),
            status=status,
        )
        self.changes[change_id] = change
        return change

    def implement_change(self, change_id: str) -> EngineeringChange:
        change = self._change(change_id)
        change = replace(change, status=ChangeStatus.IMPLEMENTED)
        self.changes[change_id] = change
        return change

    def report_part_obsolescence(
        self, part_number: str, detail: str, occurred_at: date, is_safety: bool = False
    ) -> ImpactEvent:
        """零件停产：传播到所有在用配置；无已批准替代时登记阻断性冲突。"""
        affected = tuple(dict.fromkeys(u.config_id for u in self.part_usages if u.part_number == part_number))
        if not affected:
            raise ValueError(f"没有配置使用零件 {part_number}")
        case_code = self._configuration(affected[0]).case_code
        event = self._propagate(
            EventKind.PART_OBSOLESCENCE,
            detail=f"零件 {part_number} 停产：{detail}",
            ref_id=part_number,
            case_code=case_code,
            occurred_at=occurred_at,
            affected_config_ids=affected,
        )
        for index, config_id in enumerate(affected):
            approved = any(
                s.config_id == config_id
                and s.part_number == part_number
                and s.status is SubstitutionStatus.APPROVED
                for s in self.substitutions.values()
            )
            if not approved:
                conflict_id = f"conflict-ob-{part_number}-{config_id}"
                if conflict_id not in self.conflicts:
                    self.register_conflict(
                        conflict_id=conflict_id,
                        kind=ConflictKind.OBSOLESCENCE,
                        is_safety=is_safety,
                        description=f"零件 {part_number} 停产，配置 {config_id} 尚无已批准替代供应商",
                        config_ids=(config_id,),
                    )
        return event

    def report_regulation_update(
        self,
        clause: str,
        detail: str,
        occurred_at: date,
        config_ids: tuple[str, ...] | list[str] = (),
        requirement_ids: tuple[str, ...] | list[str] = (),
        is_safety: bool = True,
    ) -> ImpactEvent:
        """法规更新：按显式配置或需求所在配置传播，并登记开放冲突等待设计决定。"""
        configs = set(config_ids)
        case_codes = {self._configuration(cid).case_code for cid in configs}
        for req_id in requirement_ids:
            requirement = self._requirement(req_id)
            case_codes.add(requirement.case_code)
            configs.update(
                cid for cid, cfg in self.configurations.items() if req_id in cfg.requirement_ids
            )
        if not configs:
            raise ValueError("法规更新必须指定受影响配置或需求")
        case_code = next(iter(case_codes))
        event = self._propagate(
            EventKind.REGULATION_UPDATE,
            detail=f"法规 {clause} 更新：{detail}",
            ref_id=clause,
            case_code=case_code,
            occurred_at=occurred_at,
            affected_config_ids=tuple(sorted(configs)),
        )
        conflict_id = f"conflict-reg-{clause}"
        if conflict_id not in self.conflicts:
            self.register_conflict(
                conflict_id=conflict_id,
                kind=ConflictKind.REGULATION,
                is_safety=is_safety,
                description=f"法规 {clause} 更新需重新评估：{detail}",
                requirement_ids=tuple(requirement_ids),
                config_ids=tuple(sorted(configs)),
            )
        return event

    # ------------------------------------------------------------------
    # 基线发布门禁
    # ------------------------------------------------------------------
    def release_baseline(
        self,
        config_id: str,
        baseline_id: str,
        released_at: date,
    ) -> Baseline:
        """发布设计基线。门禁条件：

        1. 纳入需求全部为已确认状态；
        2. 不存在未解决冲突（安全冲突必须解决，非安全冲突要么解决要么按偏差接受）；
        3. 已接受偏差未过期，且适用客户与期限随基线快照保存；
        4. 每条需求均有通过的计算与通过的样机验证覆盖；
        5. 不存在待批/被拒的供应商替代（必须先批准或撤回）。
        """
        config = self._configuration(config_id)
        if baseline_id in self.baselines:
            raise ValueError(f"基线 {baseline_id} 已存在")

        requirements = [self._requirement(rid) for rid in config.requirement_ids]
        unconfirmed = [r.req_id for r in requirements if r.status is not RequirementStatus.CONFIRMED]
        if unconfirmed:
            raise ReleaseGateError(f"存在未确认需求，不能发布基线：{unconfirmed}")

        blocking: list[str] = []
        for conflict in self._conflicts_for_config(config_id):
            if conflict.status is ConflictStatus.OPEN:
                blocking.append(
                    f"冲突 {conflict.conflict_id} 未解决"
                    + ("（安全冲突必须解决）" if conflict.is_safety else "")
                )
            elif conflict.status is ConflictStatus.ACCEPTED_DEVIATION and conflict.deviation_expiry is not None:
                if conflict.deviation_expiry < released_at:
                    blocking.append(f"偏差 {conflict.conflict_id} 已过适用期限，需重新评估")
        if blocking:
            raise ReleaseGateError("；".join(blocking))

        uncovered: list[str] = []
        for requirement in requirements:
            calc_ok = any(
                c.config_id == config_id
                and requirement.req_id in c.requirement_ids
                and c.status is VerificationStatus.PASSED
                for c in self.calculations.values()
            )
            validation_ok = any(
                v.config_id == config_id
                and requirement.req_id in v.requirement_ids
                and v.status is VerificationStatus.PASSED
                for v in self.validations.values()
            )
            if not calc_ok or not validation_ok:
                uncovered.append(requirement.req_id)
        if uncovered:
            raise ReleaseGateError(f"以下需求缺少通过的计算或样机验证覆盖：{uncovered}")

        pending_subs = [
            s.substitution_id
            for s in self.substitutions.values()
            if s.config_id == config_id and s.status is not SubstitutionStatus.APPROVED
        ]
        if pending_subs:
            raise ReleaseGateError(f"存在未批准的供应商替代：{pending_subs}")

        previous = config.current_baseline_id
        revision = 1 if previous is None else self.baselines[previous].revision + 1
        decision_ids = tuple(
            sorted(d.decision_id for d in self.decisions.values() if d.config_id == config_id)
        )
        calc_ids = tuple(sorted(c.calc_id for c in self.calculations.values() if c.config_id == config_id))
        validation_ids = tuple(
            sorted(v.validation_id for v in self.validations.values() if v.config_id == config_id)
        )
        substitution_ids = tuple(
            sorted(s.substitution_id for s in self.substitutions.values() if s.config_id == config_id)
        )
        deviation_ids = tuple(
            sorted(
                c.conflict_id
                for c in self._conflicts_for_config(config_id)
                if c.status is ConflictStatus.ACCEPTED_DEVIATION
            )
        )

        snapshot = Baseline(
            baseline_id=baseline_id,
            case_code=config.case_code,
            config_id=config_id,
            revision=revision,
            released_at=released_at,
            requirements=tuple(requirements),
            decision_ids=decision_ids,
            calc_ids=calc_ids,
            validation_ids=validation_ids,
            substitution_ids=substitution_ids,
            accepted_deviation_ids=deviation_ids,
        )
        snapshot = replace(snapshot, fingerprint=_baseline_fingerprint(snapshot))
        self.baselines[baseline_id] = snapshot
        self.configurations[config_id] = replace(config, current_baseline_id=baseline_id)
        self._stale_since.pop(config_id, None)
        return snapshot

    def deliver_unit(
        self, serial_number: str, config_id: str, delivered_at: date
    ) -> ReleasedUnit:
        """按当前基线交付序列号，并冻结需求内容指纹作为交付依据。"""
        if serial_number in self.units:
            raise ValueError(f"序列号 {serial_number} 已存在")
        config = self._configuration(config_id)
        if config.current_baseline_id is None:
            raise ValueError(f"配置 {config_id} 尚未发布基线，不能交付")
        if self._stale_since.get(config_id):
            raise ReleaseGateError(
                f"配置 {config_id} 基线受事件影响待重发布，不能按旧基线继续交付"
            )
        baseline = self.baselines[config.current_baseline_id]
        unit = ReleasedUnit(
            serial_number=serial_number,
            config_id=config_id,
            baseline_id=baseline.baseline_id,
            baseline_fingerprint=baseline.fingerprint,
            delivered_at=delivered_at,
            requirement_fingerprints=baseline.requirement_fingerprints(),
        )
        self.units[serial_number] = unit
        return unit

    # ------------------------------------------------------------------
    # 查询：有效需求、验证覆盖、开放风险、双向追踪
    # ------------------------------------------------------------------
    def effective_requirements(self, config_id: str) -> list[Requirement]:
        """配置当前有效需求（已确认且未撤回）。"""
        config = self._configuration(config_id)
        return [
            self.requirements[rid]
            for rid in config.requirement_ids
            if self.requirements[rid].status is RequirementStatus.CONFIRMED
        ]

    def verification_coverage(self, config_id: str) -> dict[str, object]:
        """按需求给出计算与样机验证覆盖情况。"""
        self._configuration(config_id)
        rows: dict[str, dict[str, object]] = {}
        for requirement in self.effective_requirements(config_id):
            calcs = [
                c.calc_id
                for c in self.calculations.values()
                if c.config_id == config_id and requirement.req_id in c.requirement_ids
            ]
            validations = [
                v.validation_id
                for v in self.validations.values()
                if v.config_id == config_id and requirement.req_id in v.requirement_ids
            ]
            calc_passed = any(
                self.calculations[cid].status is VerificationStatus.PASSED for cid in calcs
            )
            validation_passed = any(
                self.validations[vid].status is VerificationStatus.PASSED for vid in validations
            )
            rows[requirement.req_id] = {
                "title": requirement.title,
                "priority": requirement.priority.value if requirement.priority else None,
                "calc_ids": calcs,
                "validation_ids": validations,
                "calc_passed": calc_passed,
                "validation_passed": validation_passed,
                "fully_covered": calc_passed and validation_passed,
            }
        return {
            "config_id": config_id,
            "requirements": rows,
            "all_fully_covered": all(
                bool(row["fully_covered"]) for row in rows.values()
            ),
        }

    def open_risks(self, *, config_id: str | None = None, case_code: str | None = None) -> list[dict[str, object]]:
        """开放风险：开放冲突（安全优先）、失败验证、待批替代、过期偏差、待重发布基线。"""
        config_ids = self._scope_config_ids(config_id, case_code)
        risks: list[dict[str, object]] = []
        for cid in config_ids:
            for conflict in self._conflicts_for_config(cid):
                if conflict.status is ConflictStatus.OPEN:
                    risks.append({
                        "type": "safety_conflict" if conflict.is_safety else "open_conflict",
                        "config_id": cid,
                        "ref": conflict.conflict_id,
                        "detail": conflict.description,
                    })
                elif (
                    conflict.status is ConflictStatus.ACCEPTED_DEVIATION
                    and conflict.deviation_expiry is not None
                ):
                    risks.append({
                        "type": "accepted_deviation",
                        "config_id": cid,
                        "ref": conflict.conflict_id,
                        "expiry": conflict.deviation_expiry.isoformat(),
                        "customers": list(conflict.deviation_customers),
                        "detail": conflict.description,
                    })
            for validation in self.validations.values():
                if validation.config_id == cid and validation.status is VerificationStatus.FAILED:
                    risks.append({
                        "type": "failed_validation",
                        "config_id": cid,
                        "ref": validation.validation_id,
                        "detail": validation.result_summary,
                    })
            for substitution in self.substitutions.values():
                if substitution.config_id == cid and substitution.status is SubstitutionStatus.PROPOSED:
                    risks.append({
                        "type": "pending_substitution",
                        "config_id": cid,
                        "ref": substitution.substitution_id,
                        "detail": f"{substitution.part_number}: {substitution.old_supplier} -> {substitution.new_supplier}",
                    })
            for event_id in self._stale_since.get(cid, set()):
                risks.append({
                    "type": "baseline_stale",
                    "config_id": cid,
                    "ref": event_id,
                    "detail": "基线发布后发生影响事件，需重新评估并发布基线",
                })
        risks.sort(key=lambda row: 0 if row["type"] == "safety_conflict" else 1)
        return risks

    def trace_config(self, config_id: str) -> dict[str, object]:
        """配置维度双向追踪：声音->需求->决定/冲突->验证->基线->序列号。"""
        config = self._configuration(config_id)
        req_ids = list(config.requirement_ids)
        voice_ids = tuple(dict.fromkeys(
            vid for rid in req_ids for vid in self.requirements[rid].voice_ids
        ))
        baseline_ids = [
            b.baseline_id
            for b in self.baselines.values()
            if b.config_id == config_id
        ]
        return {
            "config_id": config_id,
            "name": config.name,
            "voices": [
                {
                    "voice_id": vid,
                    "source_kind": self.voices[vid].source_kind.value,
                    "source_ref": self.voices[vid].source_ref,
                    "customer": self.voices[vid].customer,
                }
                for vid in voice_ids
            ],
            "requirements": [self._requirement_row(rid) for rid in req_ids],
            "decisions": [
                {
                    "decision_id": d.decision_id,
                    "title": d.title,
                    "chosen_option": d.chosen_option,
                    "linked_requirement_ids": list(d.linked_requirement_ids),
                    "linked_conflict_id": d.linked_conflict_id,
                }
                for d in self.decisions.values()
                if d.config_id == config_id
            ],
            "conflicts": [
                self._conflict_row(c.conflict_id)
                for c in self._conflicts_for_config(config_id)
            ],
            "calculations": [
                {"calc_id": c.calc_id, "status": c.status.value, "requirement_ids": list(c.requirement_ids)}
                for c in self.calculations.values()
                if c.config_id == config_id
            ],
            "validations": [
                {"validation_id": v.validation_id, "status": v.status.value,
                 "requirement_ids": list(v.requirement_ids)}
                for v in self.validations.values()
                if v.config_id == config_id
            ],
            "changes": [
                {"change_id": c.change_id, "status": c.status.value, "trigger_ref": c.trigger_ref}
                for c in self.changes.values()
                if c.config_id == config_id
            ],
            "baselines": baseline_ids,
            "current_baseline_id": config.current_baseline_id,
            "delivered_serials": [
                s.serial_number for s in self.units.values() if s.config_id == config_id
            ],
        }

    def trace_requirement(self, req_id: str) -> dict[str, object]:
        """需求维度双向追踪：上游声音来源，下游配置/决定/验证/基线/序列号。"""
        requirement = self._requirement(req_id)
        config_ids = [
            cid for cid, cfg in self.configurations.items() if req_id in cfg.requirement_ids
        ]
        baseline_hits: list[dict[str, str]] = []
        unit_hits: list[str] = []
        for baseline in self.baselines.values():
            if req_id in baseline.requirement_fingerprints():
                baseline_hits.append({
                    "baseline_id": baseline.baseline_id,
                    "config_id": baseline.config_id,
                })
                unit_hits.extend(
                    s.serial_number
                    for s in self.units.values()
                    if s.baseline_id == baseline.baseline_id
                )
        return {
            "requirement": self._requirement_row(req_id),
            "voices": [
                {"voice_id": vid, "source_ref": self.voices[vid].source_ref,
                 "source_kind": self.voices[vid].source_kind.value,
                 "customer": self.voices[vid].customer}
                for vid in requirement.voice_ids
            ],
            "config_ids": config_ids,
            "conflicts": [
                self._conflict_row(cid)
                for cid, conflict in self.conflicts.items()
                if req_id in conflict.requirement_ids
            ],
            "decisions": [
                d.decision_id
                for d in self.decisions.values()
                if req_id in d.linked_requirement_ids
            ],
            "calculations": [
                c.calc_id for c in self.calculations.values() if req_id in c.requirement_ids
            ],
            "validations": [
                v.validation_id for v in self.validations.values() if req_id in v.requirement_ids
            ],
            "baselines": baseline_hits,
            "delivered_serials": unit_hits,
            "status": requirement.status.value,
        }

    def trace_voice(self, voice_id: str) -> dict[str, object]:
        """从客户声音向下追踪到需求、基线与序列号。"""
        voice = self.voices.get(voice_id)
        if voice is None:
            raise ValueError(f"客户声音 {voice_id} 不存在")
        req_ids = [
            rid for rid, req in self.requirements.items() if voice_id in req.voice_ids
        ]
        result: dict[str, object] = {
            "voice_id": voice_id,
            "source_ref": voice.source_ref,
            "customer": voice.customer,
            "raw_text": voice.raw_text,
            "requirements": [],
        }
        req_rows: list[object] = []
        for rid in req_ids:
            trace = self.trace_requirement(rid)
            req_rows.append({
                "req_id": rid,
                "status": trace["status"],
                "baselines": trace["baselines"],
                "delivered_serials": trace["delivered_serials"],
            })
        result["requirements"] = req_rows
        return result

    def unit_delivery_basis(self, serial_number: str) -> dict[str, object]:
        """序列号交付依据：冻结的基线与需求指纹，并标注与现行需求的漂移。"""
        unit = self.units.get(serial_number)
        if unit is None:
            raise ValueError(f"序列号 {serial_number} 不存在")
        baseline = self.baselines[unit.baseline_id]
        drift: list[dict[str, object]] = []
        for req_id, basis_fingerprint in unit.requirement_fingerprints.items():
            current = self.requirements.get(req_id)
            if current is None:
                continue
            current_fingerprint = current.fingerprint()
            if current_fingerprint != basis_fingerprint:
                drift.append({
                    "req_id": req_id,
                    "current_status": current.status.value,
                    "note": "交付后需求发生变化，交付依据仍以交付时基线为准",
                })
        affecting_events = [
            e.event_id
            for e in self.events.values()
            if serial_number in e.affected_unit_serials
        ]
        return {
            "serial_number": serial_number,
            "config_id": unit.config_id,
            "baseline_id": unit.baseline_id,
            "baseline_revision": baseline.revision,
            "baseline_fingerprint": unit.baseline_fingerprint,
            "delivered_at": unit.delivered_at.isoformat(),
            "basis_requirement_fingerprints": unit.requirement_fingerprints,
            "post_delivery_drift": drift,
            "notified_events": affecting_events,
            "basis_immutable": True,
        }

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _propagate(
        self,
        kind: EventKind,
        detail: str,
        ref_id: str,
        case_code: str,
        occurred_at: date,
        affected_config_ids: tuple[str, ...],
    ) -> ImpactEvent:
        event_id = f"evt-{kind.value}-{ref_id}-{occurred_at.isoformat()}"
        affected_units = tuple(
            s.serial_number
            for s in self.units.values()
            if s.config_id in affected_config_ids
        )
        event = ImpactEvent(
            event_id=event_id,
            case_code=case_code,
            kind=kind,
            detail=detail,
            ref_id=ref_id,
            occurred_at=occurred_at,
            affected_config_ids=affected_config_ids,
            affected_unit_serials=affected_units,
            status=EventStatus.PROPAGATED,
        )
        self.events[event_id] = event
        # 已发布基线的配置标记待重发布；未发布配置直接受现行清单/冲突约束
        for cid in affected_config_ids:
            if self.configurations[cid].current_baseline_id is not None:
                self._stale_since.setdefault(cid, set()).add(event_id)
        return event

    def _evolve_requirement(self, current: Requirement, **changes: object) -> Requirement:
        evolved = replace(current, **changes)
        self.requirements[current.req_id] = evolved
        self._requirement_versions[current.req_id].append(evolved)
        return evolved

    def _replace_substitution(
        self, substitution: SupplierSubstitution, **changes: object
    ) -> SupplierSubstitution:
        evolved = replace(substitution, **changes)
        self.substitutions[substitution.substitution_id] = evolved
        return evolved

    def _requirements_touch_safety(self, req_ids: tuple[str, ...] | list[str]) -> bool:
        return any(
            req_id in self.requirements and "safety" in self.requirements[req_id].categories
            for req_id in req_ids
        )

    def _conflicts_for_config(self, config_id: str) -> list[Conflict]:
        """配置适用的冲突：显式关联配置，或其关联需求被该配置纳入。"""
        case_code = self.configurations[config_id].case_code
        result: list[Conflict] = []
        for conflict in self.conflicts.values():
            if config_id in conflict.config_ids:
                result.append(conflict)
                continue
            if not conflict.requirement_ids:
                continue
            requirement_config_ids = {
                cid
                for rid in conflict.requirement_ids
                for cid, cfg in self.configurations.items()
                if rid in cfg.requirement_ids
                and self.requirements[rid].case_code == case_code
            }
            if config_id in requirement_config_ids:
                result.append(conflict)
        return result

    def _requirement_row(self, req_id: str) -> dict[str, object]:
        req = self.requirements[req_id]
        return {
            "req_id": req.req_id,
            "title": req.title,
            "status": req.status.value,
            "priority": req.priority.value if req.priority else None,
            "revision": req.revision,
            "voice_ids": list(req.voice_ids),
            "categories": list(req.categories),
            "target_customers": list(req.target_customers),
        }

    def _conflict_row(self, conflict_id: str) -> dict[str, object]:
        conflict = self.conflicts[conflict_id]
        return {
            "conflict_id": conflict.conflict_id,
            "kind": conflict.kind.value,
            "is_safety": conflict.is_safety,
            "status": conflict.status.value,
            "description": conflict.description,
            "requirement_ids": list(conflict.requirement_ids),
            "resolution_decision_id": conflict.resolution_decision_id,
            "deviation_customers": list(conflict.deviation_customers),
            "deviation_expiry": conflict.deviation_expiry.isoformat() if conflict.deviation_expiry else None,
        }

    def _scope_config_ids(self, config_id: str | None, case_code: str | None) -> list[str]:
        if config_id is not None:
            self._configuration(config_id)
            return [config_id]
        if case_code is not None:
            return [cid for cid, cfg in self.configurations.items() if cfg.case_code == case_code]
        return list(self.configurations)

    def _conflict_case_code(
        self, requirement_ids: tuple[str, ...], config_ids: tuple[str, ...]
    ) -> str:
        if requirement_ids:
            return self.requirements[requirement_ids[0]].case_code
        if config_ids:
            return self.configurations[config_ids[0]].case_code
        raise ValueError("冲突必须关联需求或配置")

    def _requirement(self, req_id: str) -> Requirement:
        requirement = self.requirements.get(req_id)
        if requirement is None:
            raise ValueError(f"需求 {req_id} 不存在")
        return requirement

    def _configuration(self, config_id: str) -> ProductConfiguration:
        config = self.configurations.get(config_id)
        if config is None:
            raise ValueError(f"配置 {config_id} 不存在")
        return config

    def _conflict(self, conflict_id: str) -> Conflict:
        conflict = self.conflicts.get(conflict_id)
        if conflict is None:
            raise ValueError(f"冲突 {conflict_id} 不存在")
        return conflict

    def _decision(self, decision_id: str) -> DesignDecision:
        decision = self.decisions.get(decision_id)
        if decision is None:
            raise ValueError(f"设计决定 {decision_id} 不存在")
        return decision

    def _substitution(self, substitution_id: str) -> SupplierSubstitution:
        substitution = self.substitutions.get(substitution_id)
        if substitution is None:
            raise ValueError(f"供应商替代 {substitution_id} 不存在")
        return substitution

    def _change(self, change_id: str) -> EngineeringChange:
        change = self.changes.get(change_id)
        if change is None:
            raise ValueError(f"工程变更 {change_id} 不存在")
        return change


def _baseline_fingerprint(baseline: Baseline) -> str:
    from .contracts import canonical_fingerprint

    payload = {
        "baseline_id": baseline.baseline_id,
        "config_id": baseline.config_id,
        "revision": baseline.revision,
        "released_at": baseline.released_at,
        "requirements": {
            req.req_id: req.fingerprint() for req in baseline.requirements
        },
        "decision_ids": list(baseline.decision_ids),
        "calc_ids": list(baseline.calc_ids),
        "validation_ids": list(baseline.validation_ids),
        "substitution_ids": list(baseline.substitution_ids),
        "accepted_deviation_ids": list(baseline.accepted_deviation_ids),
    }
    return canonical_fingerprint(payload)
