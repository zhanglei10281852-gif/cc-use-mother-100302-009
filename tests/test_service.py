"""本地化设计闭环服务测试：覆盖客户声音到设计基线的完整业务规则。"""

import unittest
from datetime import date

from local_design import (
    ConflictKind,
    ConflictStatus,
    EventKind,
    LocalDesignService,
    Priority,
    ReleaseGateError,
    RequirementStatus,
    SourceKind,
    SubstitutionStatus,
    VerificationStatus,
)


TODAY = date(2026, 10, 5)


def build_happy_case() -> tuple[LocalDesignService, dict[str, str]]:
    """构造一个可发布基线的矿山工况闭环，返回关键标识。"""
    svc = LocalDesignService()
    svc.record_voice(
        "v-mining-dust", "case-rv-01", SourceKind.FIELD_REPORT, "FR-2026-031",
        "某矿业集团", date(2026, 3, 10),
        "井下减速机粉尘进入导致密封失效，要求 IP66 以上防护", "现场工程师王工",
    )
    svc.record_voice(
        "v-sales-noise", "case-rv-01", SourceKind.SALES_VERBAL, "MM-2026-044-会议纪要",
        "某矿业集团", date(2026, 4, 2),
        "销售在投标沟通中口头承诺低噪音（≤75dB）", "销售李工",
    )
    svc.create_requirement(
        "req-dust", "case-rv-01", ("v-mining-dust",),
        "高粉尘防护", "矿山井下工况外壳防护不低于 IP66",
        ("environment",), ("某矿业集团",),
    )
    svc.create_requirement(
        "req-noise", "case-rv-01", ("v-sales-noise",),
        "低噪音", "1 米处声压级不超过 75dB",
        ("environment",), ("某矿业集团",),
    )
    for req_id in ("req-dust", "req-noise"):
        svc.clarify_requirement(req_id)
    svc.confirm_requirement("req-dust", Priority.P1_SAFETY)
    svc.confirm_requirement("req-noise", Priority.P3_MEDIUM)
    svc.create_configuration("cfg-mining", "case-rv-01", "RV 矿山型", ("某矿业集团",))
    svc.assign_requirements("cfg-mining", ("req-dust", "req-noise"))
    svc.record_calculation(
        "calc-dust", "cfg-mining", "密封防护校核", "GB/T 4208",
        ("req-dust",), VerificationStatus.PASSED, "密封结构满足 IP66")
    svc.record_calculation(
        "calc-noise", "cfg-mining", "噪音校核", "GB/T 6404",
        ("req-noise",), VerificationStatus.PASSED, "预估 73dB")
    svc.record_validation(
        "val-dust", "cfg-mining", "PT-M01", ("req-dust",),
        VerificationStatus.PASSED, "粉尘试验通过", TODAY)
    svc.record_validation(
        "val-noise", "cfg-mining", "PT-M01", ("req-noise",),
        VerificationStatus.PASSED, "实测 72dB", TODAY)
    ids = {"case": "case-rv-01", "config": "cfg-mining"}
    return svc, ids


class VoiceSourceTests(unittest.TestCase):
    def test_verbal_promise_requires_source_ref(self) -> None:
        svc = LocalDesignService()
        with self.assertRaises(ValueError):
            svc.record_voice(
                "v1", "case-1", SourceKind.SALES_VERBAL, "  ",
                "客户甲", TODAY, "销售口头说免维护", "销售",
            )

    def test_requirement_without_voice_rejected(self) -> None:
        svc = LocalDesignService()
        with self.assertRaises(ValueError):
            svc.create_requirement(
                "r1", "case-1", (), "标题", "陈述", ("load",), ("客户甲",),
            )

    def test_requirement_rejects_unknown_voice(self) -> None:
        svc = LocalDesignService()
        with self.assertRaises(ValueError):
            svc.create_requirement(
                "r1", "case-1", ("nope",), "标题", "陈述", ("load",), ("客户甲",),
            )


class RequirementLifecycleTests(unittest.TestCase):
    def test_unclarified_requirement_cannot_enter_config(self) -> None:
        svc, ids = build_happy_case()
        svc.create_requirement(
            "req-load", ids["case"], ("v-mining-dust",),
            "重载", "峰值载荷 2.5 倍额定", ("load",), ("某矿业集团",),
        )
        with self.assertRaises(ValueError):
            svc.assign_requirements(ids["config"], ("req-load",))

    def test_clarify_then_confirm_flow(self) -> None:
        svc, _ = build_happy_case()
        req = svc.requirements["req-dust"]
        self.assertIs(req.status, RequirementStatus.CONFIRMED)
        self.assertIs(req.priority, Priority.P1_SAFETY)

    def test_withdraw_propagates_and_prunes_config(self) -> None:
        svc, ids = build_happy_case()
        baseline = svc.release_baseline(ids["config"], "bl-1", TODAY)
        svc.deliver_unit("SN-001", ids["config"], TODAY)
        event = svc.withdraw_requirement("req-noise", "客户取消低噪音要求", TODAY)
        self.assertIs(event.kind, EventKind.REQUIREMENT_WITHDRAWN)
        self.assertEqual(event.affected_config_ids, (ids["config"],))
        self.assertIn("SN-001", event.affected_unit_serials)
        # 现行配置不再包含撤回需求
        self.assertNotIn("req-noise", svc.configurations[ids["config"]].requirement_ids)
        # 已发布基线快照仍保留
        self.assertIn("req-noise", baseline.requirement_fingerprints())
        # 基线被标记待重发布，不能继续按旧基线交付
        with self.assertRaises(ReleaseGateError):
            svc.deliver_unit("SN-002", ids["config"], TODAY)
        # 撤回需求不能再澄清
        with self.assertRaises(ValueError):
            svc.clarify_requirement("req-noise")


class ConflictAndGateTests(unittest.TestCase):
    def test_open_safety_conflict_blocks_release(self) -> None:
        svc, ids = build_happy_case()
        svc.register_conflict(
            "c-safety", ConflictKind.REQUIREMENT, True,
            "防爆要求与当前箱体材料冲突",
            requirement_ids=("req-dust",),
        )
        with self.assertRaises(ReleaseGateError):
            svc.release_baseline(ids["config"], "bl-1", TODAY)

    def test_safety_conflict_cannot_be_accepted_as_deviation(self) -> None:
        svc, ids = build_happy_case()
        svc.register_conflict(
            "c-safety", ConflictKind.REQUIREMENT, True,
            "安全联锁要求与维护便捷性冲突",
            requirement_ids=("req-dust",),
        )
        with self.assertRaises(ValueError):
            svc.accept_deviation(
                "c-safety", "先这样发货", ("某矿业集团",), date(2027, 1, 1), TODAY,
            )

    def test_resolve_safety_conflict_with_decision_allows_release(self) -> None:
        svc, ids = build_happy_case()
        svc.register_conflict(
            "c-safety", ConflictKind.REQUIREMENT, True,
            "密封方案选型冲突", requirement_ids=("req-dust", "req-noise"),
        )
        svc.record_decision(
            "d-seal", ids["config"], "采用双唇密封+迷宫结构", "双唇密封",
            ("单唇密封",), ("req-dust",), linked_conflict_id="c-safety", decided_at=TODAY,
        )
        svc.resolve_conflict("c-safety", "d-seal")
        baseline = svc.release_baseline(ids["config"], "bl-1", TODAY)
        self.assertEqual(baseline.revision, 1)
        self.assertTrue(baseline.fingerprint)

    def test_decision_must_reference_conflict_to_resolve(self) -> None:
        svc, ids = build_happy_case()
        svc.register_conflict(
            "c1", ConflictKind.REQUIREMENT, False, "外观颜色分歧",
            requirement_ids=("req-noise",),
        )
        svc.record_decision(
            "d1", ids["config"], "颜色定为工业灰", "灰", ("蓝",),
            ("req-noise",), decided_at=TODAY,
        )
        with self.assertRaises(ValueError):
            svc.resolve_conflict("c1", "d1")

    def test_deviation_requires_customers_and_future_expiry(self) -> None:
        svc, ids = build_happy_case()
        svc.register_conflict(
            "c-dev", ConflictKind.REQUIREMENT, False, "噪音只能做到 78dB",
            requirement_ids=("req-noise",),
        )
        with self.assertRaises(ValueError):
            svc.accept_deviation("c-dev", "客户现场无值守", (), date(2027, 1, 1), TODAY)
        with self.assertRaises(ValueError):
            svc.accept_deviation(
                "c-dev", "客户现场无值守", ("某矿业集团",), TODAY, TODAY,
            )
        svc.accept_deviation(
            "c-dev", "井下无人值守区域可接受", ("某矿业集团",), date(2027, 12, 31), TODAY,
        )
        baseline = svc.release_baseline(ids["config"], "bl-1", TODAY)
        self.assertEqual(baseline.accepted_deviation_ids, ("c-dev",))
        conflict = svc.conflicts["c-dev"]
        self.assertEqual(conflict.status, ConflictStatus.ACCEPTED_DEVIATION)
        self.assertEqual(conflict.deviation_customers, ("某矿业集团",))

    def test_expired_deviation_blocks_release(self) -> None:
        svc, ids = build_happy_case()
        svc.register_conflict(
            "c-dev", ConflictKind.REQUIREMENT, False, "噪音偏差",
            requirement_ids=("req-noise",),
        )
        svc.accept_deviation(
            "c-dev", "过渡期", ("某矿业集团",), date(2026, 12, 31), TODAY,
        )
        with self.assertRaises(ReleaseGateError):
            svc.release_baseline(ids["config"], "bl-1", date(2027, 1, 15))

    def test_missing_validation_coverage_blocks_release(self) -> None:
        svc = LocalDesignService()
        svc.record_voice(
            "v1", "c1", SourceKind.CUSTOMER_DOCUMENT, "TD-1",
            "客户甲", TODAY, "要求抗腐蚀", "研发",
        )
        svc.create_requirement(
            "r1", "c1", ("v1",), "防腐", "盐雾 720h", ("environment",), ("客户甲",),
        )
        svc.clarify_requirement("r1")
        svc.confirm_requirement("r1", Priority.P2_HIGH)
        svc.create_configuration("cfg1", "c1", "风电型", ("客户甲",))
        svc.assign_requirements("cfg1", ("r1",))
        svc.record_calculation(
            "calc1", "cfg1", "防腐计算", "ISO 12944", ("r1",),
            VerificationStatus.PASSED, "合格",
        )
        with self.assertRaises(ReleaseGateError):
            svc.release_baseline("cfg1", "bl-1", TODAY)


class ValidationFailureTests(unittest.TestCase):
    def test_failed_validation_registers_conflict_and_propagates(self) -> None:
        svc, ids = build_happy_case()
        svc.release_baseline(ids["config"], "bl-1", TODAY)
        svc.record_validation(
            "val-retest", "cfg-mining", "PT-M02", ("req-dust",),
            VerificationStatus.FAILED, "复测时密封圈老化渗漏", TODAY,
        )
        risks = svc.open_risks(config_id=ids["config"])
        types = [r["type"] for r in risks]
        # req-dust 类别是 environment，冲突登记为普通开放冲突
        self.assertIn("open_conflict", types)
        self.assertIn("failed_validation", types)
        self.assertTrue(any(r["type"] == "baseline_stale" for r in risks))
        with self.assertRaises(ReleaseGateError):
            svc.deliver_unit("SN-009", ids["config"], TODAY)

    def test_failed_safety_validation_conflict_blocks(self) -> None:
        svc = LocalDesignService()
        svc.record_voice(
            "v1", "c1", SourceKind.FIELD_REPORT, "FR-1", "客户甲", TODAY,
            "制动安全距离要求", "现场",
        )
        svc.create_requirement(
            "r1", "c1", ("v1",), "制动安全", "急停距离≤0.5m",
            ("safety",), ("客户甲",),
        )
        svc.clarify_requirement("r1")
        svc.confirm_requirement("r1", Priority.P1_SAFETY)
        svc.create_configuration("cfg1", "c1", "机器人型", ("客户甲",))
        svc.assign_requirements("cfg1", ("r1",))
        svc.record_calculation(
            "calc1", "cfg1", "制动校核", "ISO 13849", ("r1",),
            VerificationStatus.PASSED, "达标",
        )
        svc.record_validation(
            "val1", "cfg1", "PT-R1", ("r1",),
            VerificationStatus.FAILED, "实测 0.7m", TODAY,
        )
        conflict = svc.conflicts["conflict-vf-val1"]
        self.assertTrue(conflict.is_safety)
        with self.assertRaises(ReleaseGateError):
            svc.release_baseline("cfg1", "bl-1", TODAY)


class ObsolescenceTests(unittest.TestCase):
    def test_obsolescence_blocks_without_approved_substitution(self) -> None:
        svc, ids = build_happy_case()
        svc.add_part_usage(ids["config"], "BRG-6208", "深沟球轴承", "某外资轴承厂")
        svc.propose_substitution(
            "sub-1", ids["config"], "BRG-6208", "国产轴承厂", "原供应商停产、国产化替代",
        )
        svc.report_part_obsolescence("BRG-6208", "供应商发布 EOL 通知", TODAY)
        risks = svc.open_risks(config_id=ids["config"])
        self.assertIn("pending_substitution", [r["type"] for r in risks])
        with self.assertRaises(ReleaseGateError):
            svc.release_baseline(ids["config"], "bl-1", TODAY)
        # 未使用该零件的配置不应受传播
        svc.create_configuration("cfg-wind", "case-rv-01", "RV 风电型", ("某风电客户",))
        event = svc.events["evt-part_obsolescence-BRG-6208-2026-10-05"]
        self.assertNotIn("cfg-wind", event.affected_config_ids)

    def test_approved_substitution_via_change_allows_release(self) -> None:
        svc, ids = build_happy_case()
        svc.add_part_usage(ids["config"], "BRG-6208", "深沟球轴承", "某外资轴承厂")
        svc.report_part_obsolescence("BRG-6208", "EOL", TODAY)
        svc.propose_substitution(
            "sub-1", ids["config"], "BRG-6208", "国产轴承厂", "国产化替代",
        )
        svc.record_change(
            "ec-1", ids["config"], "轴承供应商切换", "sub-1",
            linked_requirement_ids=("req-dust",),
        )
        svc.approve_substitution("sub-1", change_id="ec-1")
        svc.implement_change("ec-1")
        # 停产冲突仍开放（需要设计决定确认替代满足要求）
        svc.record_decision(
            "d-bearing", ids["config"], "采用国产等型号轴承并加严入厂检验",
            "国产 BRG-6208", ("继续寻货原厂",), ("req-dust",),
            linked_conflict_id="conflict-ob-BRG-6208-cfg-mining", decided_at=TODAY,
        )
        svc.resolve_conflict("conflict-ob-BRG-6208-cfg-mining", "d-bearing")
        baseline = svc.release_baseline(ids["config"], "bl-1", TODAY)
        self.assertEqual(baseline.substitution_ids, ("sub-1",))


class RegulationTests(unittest.TestCase):
    def test_regulation_update_propagates_and_blocks(self) -> None:
        svc, ids = build_happy_case()
        svc.release_baseline(ids["config"], "bl-1", TODAY)
        svc.deliver_unit("SN-100", ids["config"], TODAY)
        event = svc.report_regulation_update(
            "GB-X-2027", "矿山设备防护新增外部防火要求", TODAY,
            config_ids=(ids["config"],), requirement_ids=("req-dust",),
        )
        self.assertIn(ids["config"], event.affected_config_ids)
        self.assertIn("SN-100", event.affected_unit_serials)
        conflict = svc.conflicts["conflict-reg-GB-X-2027"]
        self.assertTrue(conflict.is_safety)
        with self.assertRaises(ReleaseGateError):
            svc.deliver_unit("SN-101", ids["config"], TODAY)


class BaselineAndUnitTests(unittest.TestCase):
    def test_baseline_snapshot_is_immutable_basis(self) -> None:
        svc, ids = build_happy_case()
        baseline = svc.release_baseline(ids["config"], "bl-1", TODAY)
        unit = svc.deliver_unit("SN-001", ids["config"], TODAY)
        basis_fingerprint = unit.requirement_fingerprints["req-dust"]

        # 交付后再次澄清修订需求
        svc.clarify_requirement("req-dust", "矿山井下工况外壳防护不低于 IP67")
        self.assertNotEqual(
            svc.requirements["req-dust"].fingerprint(), basis_fingerprint
        )
        basis = svc.unit_delivery_basis("SN-001")
        self.assertTrue(basis["basis_immutable"])
        self.assertEqual(
            basis["basis_requirement_fingerprints"]["req-dust"], basis_fingerprint
        )
        self.assertEqual(basis["baseline_fingerprint"], baseline.fingerprint)
        drift_ids = [row["req_id"] for row in basis["post_delivery_drift"]]
        self.assertIn("req-dust", drift_ids)

    def test_revision_increments_on_re_release(self) -> None:
        svc, ids = build_happy_case()
        first = svc.release_baseline(ids["config"], "bl-1", TODAY)
        self.assertEqual(first.revision, 1)
        # 事件导致基线失效
        svc.withdraw_requirement("req-noise", "客户取消", TODAY)
        # 重发布前需消解：撤回冲突未产生开放冲突，直接以新清单再发布
        second = svc.release_baseline(ids["config"], "bl-2", TODAY)
        self.assertEqual(second.revision, 2)
        self.assertEqual(
            [r.req_id for r in second.requirements], ["req-dust"]
        )
        # 再发布后恢复交付
        unit = svc.deliver_unit("SN-020", ids["config"], TODAY)
        self.assertEqual(unit.baseline_id, "bl-2")

    def test_cannot_deliver_without_baseline(self) -> None:
        svc, ids = build_happy_case()
        with self.assertRaises(ValueError):
            svc.deliver_unit("SN-001", ids["config"], TODAY)


class QueryTests(unittest.TestCase):
    def test_effective_requirements_excludes_withdrawn(self) -> None:
        svc, ids = build_happy_case()
        self.assertEqual(
            sorted(r.req_id for r in svc.effective_requirements(ids["config"])),
            ["req-dust", "req-noise"],
        )
        svc.withdraw_requirement("req-noise", "客户取消", TODAY)
        self.assertEqual(
            [r.req_id for r in svc.effective_requirements(ids["config"])],
            ["req-dust"],
        )

    def test_verification_coverage(self) -> None:
        svc, ids = build_happy_case()
        coverage = svc.verification_coverage(ids["config"])
        self.assertTrue(coverage["all_fully_covered"])
        rows = coverage["requirements"]
        self.assertTrue(rows["req-dust"]["calc_passed"])
        self.assertTrue(rows["req-noise"]["validation_passed"])

    def test_open_risks_safety_first(self) -> None:
        svc, ids = build_happy_case()
        svc.register_conflict(
            "c-safe", ConflictKind.REQUIREMENT, True, "安全冲突",
            requirement_ids=("req-dust",),
        )
        svc.register_conflict(
            "c-cos", ConflictKind.REQUIREMENT, False, "外观冲突",
            requirement_ids=("req-noise",),
        )
        risks = svc.open_risks(case_code=ids["case"])
        self.assertEqual(risks[0]["type"], "safety_conflict")
        self.assertEqual(risks[0]["ref"], "c-safe")

    def test_bidirectional_trace(self) -> None:
        svc, ids = build_happy_case()
        svc.record_decision(
            "d-seal", ids["config"], "双唇密封", "双唇密封", (),
            ("req-dust",), decided_at=TODAY,
        )
        svc.release_baseline(ids["config"], "bl-1", TODAY)
        svc.deliver_unit("SN-001", ids["config"], TODAY)

        # 声音向下追踪
        voice_trace = svc.trace_voice("v-mining-dust")
        req_ids = [row["req_id"] for row in voice_trace["requirements"]]
        self.assertIn("req-dust", req_ids)
        serials = []
        for row in voice_trace["requirements"]:
            serials.extend(row["delivered_serials"])
        self.assertIn("SN-001", serials)

        # 需求双向追踪
        req_trace = svc.trace_requirement("req-dust")
        self.assertEqual(req_trace["voices"][0]["source_ref"], "FR-2026-031")
        self.assertIn("d-seal", req_trace["decisions"])
        self.assertEqual(req_trace["baselines"][0]["baseline_id"], "bl-1")
        self.assertIn("SN-001", req_trace["delivered_serials"])

        # 配置整体追踪
        config_trace = svc.trace_config(ids["config"])
        self.assertEqual(config_trace["current_baseline_id"], "bl-1")
        self.assertEqual(config_trace["delivered_serials"], ["SN-001"])
        self.assertEqual(len(config_trace["voices"]), 2)


if __name__ == "__main__":
    unittest.main()
