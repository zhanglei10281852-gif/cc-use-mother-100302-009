"""本地化设计闭环服务的规则测试。

场景以同一减速机在矿山、风电、机器人产线的不同要求为背景，
覆盖：来源需求、澄清/冲突/优先级门禁、安全冲突发布拦截、偏差期限、
基线固化与序列号原依据、撤回/法规更新/停产/验证失败传播、双向追踪。
"""

from __future__ import annotations

import unittest

from local_design import (
    ChangeKind,
    ConflictType,
    ConfigStatus,
    LocalDesignLoop,
    LoopError,
    Priority,
    RequirementStatus,
    Source,
    VerificationKind,
    VerificationResult,
)


MINING_SOURCE = Source(
    source_type="site_report",
    reference="SITE-MN-2026-017",
    customer="西北矿业集团",
    recorded_at="2026-03-02",
    contact="王工",
)
WIND_SOURCE = Source(
    source_type="customer_email",
    reference="MAIL-WP-2026-044",
    customer="华东风电整机厂",
    recorded_at="2026-03-10",
)
ROBOT_SOURCE = Source(
    source_type="sales_minutes",
    reference="MT-RB-2026-009",
    customer="长三角机器人产线集成商",
    recorded_at="2026-03-15",
)


class ClosedLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = LocalDesignLoop()
        self.loop.register_voc(
            "VOC-MN-DUST", "GBX-900", "矿山现场多粉尘，要求更高防护等级", MINING_SOURCE
        )
        self.loop.register_voc(
            "VOC-WP-IP", "GBX-900", "风电客户承诺整机寿命期免开盖维护、IP68", WIND_SOURCE
        )
        self.loop.register_voc(
            "VOC-RB-MT", "GBX-900", "机器人产线要求季度可开盖点检、维护窗口 8 小时", ROBOT_SOURCE
        )
        self.loop.register_voc(
            "VOC-MN-TEMP", "GBX-900", "矿山井下环境温度 45℃ 持续运行", MINING_SOURCE
        )

    def _ready_requirement(self, code: str, voc: str, title: str, statement: str,
                           *, customers: tuple[str, ...] = (), safety: bool = False,
                           priority: Priority = Priority.P2) -> str:
        self.loop.create_requirement(
            code, "GBX-900", title, statement, (voc,),
            applicable_customers=customers, safety_related=safety,
        )
        self.loop.clarify_requirement(code, f"{title} 的环境/载荷/维护边界已澄清")
        self.loop.prioritize_requirement(code, priority)
        return code

    # ------------------------------------------------------------ 来源与门禁

    def test_verbal_promise_without_source_cannot_become_requirement(self) -> None:
        with self.assertRaises(LoopError):
            self.loop.create_requirement(
                "REQ-NO-SRC", "GBX-900", "销售口头承诺", "无来源的耐低温承诺", ()
            )
        with self.assertRaises(LoopError):
            self.loop.create_requirement(
                "REQ-FAKE-SRC", "GBX-900", "虚构来源", "不存在的会议纪要", ("VOC-NOPE",)
            )

    def test_draft_or_unprioritized_requirement_cannot_enter_baseline(self) -> None:
        self.loop.create_requirement(
            "REQ-X", "GBX-900", "未澄清需求", "现场报告的问题", ("VOC-MN-DUST",)
        )
        with self.assertRaises(LoopError):
            self.loop.draft_configuration("CFG-X", "GBX-900", "试验配置", ("REQ-X",))
        self.loop.clarify_requirement("REQ-X", "已澄清")
        with self.assertRaises(LoopError):
            self.loop.draft_configuration("CFG-X", "GBX-900", "试验配置", ("REQ-X",))

    # ------------------------------------------------------- 安全冲突与发布

    def test_unresolved_safety_conflict_blocks_release(self) -> None:
        ip68 = self._ready_requirement(
            "REQ-IP68", "VOC-WP-IP", "IP68 全密封", "20 年免开盖、IP68",
            customers=("华东风电整机厂",), safety=True, priority=Priority.P1,
        )
        temp = self._ready_requirement(
            "REQ-TEMP45", "VOC-MN-TEMP", "高温持续运行", "45℃ 环温热容量校核",
            customers=("西北矿业集团",), safety=True, priority=Priority.P1,
        )
        self.loop.register_conflict(
            "CNF-SAFE-01", ConflictType.SAFETY, (ip68, temp),
            "全密封方案在 45℃ 井下热容量不足，存在润滑失效安全风险",
        )
        self.loop.add_decision(
            "DEC-DRAIN-OLD", "旧方案：呼吸阀+散热筋", (ip68, temp),
            calculation_refs=("CALC-THERM-01",),
        )
        self.loop.draft_configuration(
            "CFG-GEAR-A", "GBX-900", "矿用/风电减速机 A 型", (ip68, temp),
            ("DEC-DRAIN-OLD",),
        )
        with self.assertRaises(LoopError) as caught:
            self.loop.release_configuration("CFG-GEAR-A")
        self.assertIn("安全冲突 CNF-SAFE-01 未解决", str(caught.exception))

        # 安全冲突不允许用偏差放行
        with self.assertRaises(LoopError):
            self.loop.accept_deviation(
                "CNF-SAFE-01", "先放行后改", ("西北矿业集团",), "2026-12-31"
            )

        # 以设计决定解决后才能发布
        self.loop.add_decision(
            "DEC-COOLING", "新方案：独立冷却回路+磁耦合密封", (ip68, temp),
            calculation_refs=("CALC-THERM-02",), conflict_codes=("CNF-SAFE-01",),
        )
        self.loop.add_verification(
            "VRF-PROTO-01", VerificationKind.PROTOTYPE, (ip68, temp), "DEC-COOLING",
            result=VerificationResult.PASSED, report_ref="RPT-PROTO-09",
            executed_at="2026-06-01",
        )
        self.loop.resolve_conflict(
            "CNF-SAFE-01", "独立冷却回路同时满足密封与热容量", "DEC-COOLING"
        )
        released = self.loop.release_configuration("CFG-GEAR-A", released_at="2026-07-01")
        self.assertEqual(released.status, ConfigStatus.RELEASED)
        self.assertIsNotNone(released.baseline_fingerprint)

    # ------------------------------------------------------------- 偏差规则

    def _build_released_robot_config(self) -> None:
        ip68 = self._ready_requirement(
            "REQ-IP68", "VOC-WP-IP", "IP68 全密封", "20 年免开盖、IP68",
            customers=("华东风电整机厂",), safety=True, priority=Priority.P1,
        )
        access = self._ready_requirement(
            "REQ-ACCESS", "VOC-RB-MT", "季度开盖点检", "8 小时维护窗口",
            customers=("长三角机器人产线集成商",), priority=Priority.P2,
        )
        self.loop.register_conflict(
            "CNF-MAINT-01", ConflictType.REQUIREMENT, (ip68, access),
            "全密封与季度开盖点检互斥",
        )
        self.loop.add_decision("DEC-SEAL", "密封箱体+外置状态监测", (ip68, access))
        self.loop.draft_configuration(
            "CFG-GEAR-R", "GBX-900", "机器人产线减速机 R 型", (ip68, access),
            ("DEC-SEAL",),
        )

    def test_deviation_requires_customers_and_valid_term(self) -> None:
        self._build_released_robot_config()
        with self.assertRaises(ValueError):
            self.loop.accept_deviation(
                "CNF-MAINT-01", "暂不处理", (), "2026-12-31"
            )
        with self.assertRaises(ValueError):
            self.loop.accept_deviation(
                "CNF-MAINT-01", "暂不处理", ("长三角机器人产线集成商",), "bad-date"
            )

    def test_deviation_must_cover_requirement_customer_scope(self) -> None:
        self._build_released_robot_config()
        # REQ-IP68 适用客户为华东风电，偏差却只批给机器人客户 -> 不能发布
        self.loop.accept_deviation(
            "CNF-MAINT-01", "机器人客户首年免开盖，用状态监测替代",
            ("长三角机器人产线集成商",), "2027-03-15",
        )
        with self.assertRaises(LoopError) as caught:
            self.loop.release_configuration("CFG-GEAR-R", released_at="2026-08-01")
        self.assertIn("偏差适用客户未覆盖", str(caught.exception))

        # 补充覆盖全部适用客户后可发布
        self.loop.accept_deviation(
            "CNF-MAINT-01", "机器人客户首年免开盖，用状态监测替代；风电客户同步适用",
            ("长三角机器人产线集成商", "华东风电整机厂"), "2027-03-15",
        )
        released = self.loop.release_configuration("CFG-GEAR-R", released_at="2026-08-01")
        self.assertTrue(released.is_released)

    def test_expired_deviation_blocks_release_of_new_revision(self) -> None:
        self.test_deviation_must_cover_requirement_customer_scope()
        self.loop.deliver_unit(
            "SN-R-2601", "CFG-GEAR-R", "长三角机器人产线集成商", "2026-08-10"
        )
        # 偏差到期后创建新版本（假设加入新需求），发布门禁必须拦截到期偏差
        thermal = self._ready_requirement(
            "REQ-TEMP45", "VOC-MN-TEMP", "高温持续运行", "45℃ 热容量",
            customers=("西北矿业集团",), priority=Priority.P2,
        )
        revision = self.loop.revise_configuration(
            "CFG-GEAR-R",
            requirement_codes=("REQ-IP68", "REQ-ACCESS", thermal),
        )
        self.assertEqual(revision.revision, 2)
        with self.assertRaises(LoopError):
            self.loop.release_configuration("CFG-GEAR-R", released_at="2027-06-01")

    # ------------------------------------------- 交付固化与变更传播（原依据）

    def test_withdrawal_propagates_but_delivered_serial_keeps_basis(self) -> None:
        # 准备并发布一台矿用配置
        thermal = self._ready_requirement(
            "REQ-TEMP45", "VOC-MN-TEMP", "高温持续运行", "45℃ 热容量",
            customers=("西北矿业集团",), safety=True, priority=Priority.P1,
        )
        self.loop.add_decision("DEC-THERM", "热容量设计", (thermal,),
                               calculation_refs=("CALC-THERM-02",))
        self.loop.add_verification(
            "VRF-THERM", VerificationKind.CALCULATION, (thermal,), "DEC-THERM",
            result=VerificationResult.PASSED, report_ref="CALC-THERM-02",
            executed_at="2026-05-20",
        )
        self.loop.draft_configuration("CFG-MINE", "GBX-900", "矿用机型", (thermal,), ("DEC-THERM",))
        released = self.loop.release_configuration("CFG-MINE", released_at="2026-06-01")
        original_fingerprint = released.baseline_fingerprint
        unit = self.loop.deliver_unit("SN-M-1001", "CFG-MINE", "西北矿业集团", "2026-06-10")

        change = self.loop.withdraw_requirement(
            "REQ-TEMP45", "矿山客户撤回 45℃ 持续运行要求，改为 40℃"
        )
        self.assertEqual(change.kind, ChangeKind.REQUIREMENT_WITHDRAWAL)
        self.assertIn("CFG-MINE", change.impacted_config_codes)
        self.assertIn("SN-M-1001", change.notified_serials)

        # 已交付序列号依据不变
        stored = self.loop.get_unit("SN-M-1001")
        self.assertEqual(stored.baseline_fingerprint, original_fingerprint)
        self.assertEqual(unit.baseline_fingerprint, stored.baseline_fingerprint)

        # 当前最新配置的有效需求中不再包含已撤回需求，并出现开放风险
        view = self.loop.configuration_view("CFG-MINE")
        self.assertNotIn("REQ-TEMP45", [r.req_code for r in view.effective_requirements])
        self.assertTrue(any("REQ-TEMP45" in risk or "待消化变更" in risk for risk in view.open_risks))
        self.assertIn(change.change_code, view.configuration.open_change_codes)
        self.assertEqual(self.loop.get_requirement("REQ-TEMP45").status,
                         RequirementStatus.WITHDRAWN)

    def test_verification_failure_propagates_to_affected_configuration(self) -> None:
        thermal = self._ready_requirement(
            "REQ-TEMP45", "VOC-MN-TEMP", "高温持续运行", "45℃ 热容量",
            customers=("西北矿业集团",), priority=Priority.P1,
        )
        self.loop.add_decision("DEC-THERM", "热容量设计", (thermal,))
        self.loop.draft_configuration("CFG-MINE", "GBX-900", "矿用机型", (thermal,), ("DEC-THERM",))
        self.loop.release_configuration("CFG-MINE", released_at="2026-06-01")
        self.loop.deliver_unit("SN-M-1002", "CFG-MINE", "西北矿业集团", "2026-06-20")

        self.loop.add_verification(
            "VRF-FIELD-09", VerificationKind.FIELD, (thermal,), "DEC-THERM",
            result=VerificationResult.FAILED, report_ref="RPT-FIELD-09",
            executed_at="2026-09-01", failure_note="夏季井下实测油温超限 8℃",
        )
        change = self.loop.get_change("CHG-VF-VRF-FIELD-09")
        self.assertIn("CFG-MINE", change.impacted_config_codes)
        self.assertIn("SN-M-1002", change.notified_serials)
        risks = self.loop.open_risks("CFG-MINE")
        self.assertTrue(any("验证失败待返工" in risk for risk in risks))

    def test_regulation_update_supersedes_requirement_and_propagates(self) -> None:
        old = self._ready_requirement(
            "REQ-SAFETY-OLD", "VOC-MN-DUST", "防爆等级（旧版）", "符合旧版煤安规程",
            safety=True, priority=Priority.P1,
        )
        self.loop.add_decision("DEC-EX-OLD", "旧版防爆结构", (old,))
        self.loop.draft_configuration("CFG-EX", "GBX-900", "矿用防爆机型", (old,), ("DEC-EX-OLD",))
        self.loop.release_configuration("CFG-EX", released_at="2026-02-01")

        change = self.loop.update_regulation(
            "REQ-SAFETY-OLD", "REQ-SAFETY-NEW",
            "防爆等级（新版煤安规程）", "符合新版煤安规程 MA-2027",
            "煤安规程换版，2027 年起旧版防爆结构不得新装机",
            regulation_ref="MA-2027", priority=Priority.P1, effective_at="2027-01-01",
        )
        self.assertEqual(change.kind, ChangeKind.REGULATION_UPDATE)
        self.assertIn("CFG-EX", change.impacted_config_codes)
        self.assertEqual(self.loop.get_requirement("REQ-SAFETY-OLD").status,
                         RequirementStatus.SUPERSEDED)
        new_req = self.loop.get_requirement("REQ-SAFETY-NEW")
        self.assertEqual(new_req.supersedes, "REQ-SAFETY-OLD")
        self.assertEqual(new_req.regulation_ref, "MA-2027")
        self.assertEqual(new_req.priority, Priority.P1)
        # 新需求可进入新版本基线
        revision = self.loop.revise_configuration("CFG-EX", requirement_codes=("REQ-SAFETY-NEW",))
        self.assertEqual(revision.requirement_codes, ("REQ-SAFETY-NEW",))

    def test_supplier_substitution_propagates_to_configuration(self) -> None:
        thermal = self._ready_requirement(
            "REQ-TEMP45", "VOC-MN-TEMP", "高温持续运行", "45℃ 热容量",
            customers=("西北矿业集团",), priority=Priority.P2,
        )
        self.loop.add_decision("DEC-THERM", "热容量设计（指定密封件供应商 A）", (thermal,))
        self.loop.draft_configuration("CFG-MINE", "GBX-900", "矿用机型", (thermal,), ("DEC-THERM",))
        self.loop.release_configuration("CFG-MINE", released_at="2026-06-01")
        self.loop.deliver_unit("SN-M-1003", "CFG-MINE", "西北矿业集团", "2026-07-01")

        change = self.loop.propagate_event(
            "CHG-SUB-SEAL-01", ChangeKind.SUPPLIER_SUBSTITUTION,
            "密封件供应商 A 的骨架油封停产，以供应商 B 等效件替代",
            (thermal,), decision_codes=("DEC-THERM",), effective_at="2026-11-01",
        )
        self.assertIn("CFG-MINE", change.impacted_config_codes)
        self.assertIn("SN-M-1003", change.notified_serials)
        latest = self.loop.get_latest_configuration("CFG-MINE")
        self.assertIn("CHG-SUB-SEAL-01", latest.open_change_codes)

    # -------------------------------------------------------- 追踪与覆盖视图

    def test_bidirectional_traceability(self) -> None:
        ip68 = self._ready_requirement(
            "REQ-IP68", "VOC-WP-IP", "IP68 全密封", "20 年免开盖、IP68",
            customers=("华东风电整机厂",), safety=True, priority=Priority.P1,
        )
        self.loop.add_decision(
            "DEC-SEAL", "磁耦合密封方案", (ip68,),
            calculation_refs=("CALC-SEAL-01",),
        )
        self.loop.add_verification(
            "VRF-SEAL", VerificationKind.PROTOTYPE, (ip68,), "DEC-SEAL",
            result=VerificationResult.PASSED, report_ref="RPT-SEAL-01",
            executed_at="2026-05-10",
        )
        self.loop.draft_configuration("CFG-WIND", "GBX-900", "风电机型", (ip68,), ("DEC-SEAL",))
        self.loop.release_configuration("CFG-WIND", released_at="2026-06-01")
        self.loop.deliver_unit("SN-W-2001", "CFG-WIND", "华东风电整机厂", "2026-06-15")

        forward = {(node.kind, node.code) for node in self.loop.trace_from_voc("VOC-WP-IP")}
        self.assertIn(("requirement", "REQ-IP68"), forward)
        self.assertIn(("decision", "DEC-SEAL"), forward)
        self.assertIn(("verification", "VRF-SEAL"), forward)
        self.assertIn(("configuration", "CFG-WIND@rev1"), forward)
        self.assertIn(("serial", "SN-W-2001"), forward)

        backward = self.loop.trace_to_origin("DEC-SEAL")
        origin = [node for node in backward if node.kind == "voc"]
        self.assertEqual(len(origin), 1)
        self.assertIn("MAIL-WP-2026-044", origin[0].summary)
        self.assertIn("华东风电整机厂", origin[0].summary)

        backward_req = self.loop.trace_to_origin("REQ-IP68")
        self.assertTrue(any(node.code == "VOC-WP-IP" for node in backward_req))

    def test_configuration_view_reports_coverage_and_risks(self) -> None:
        ip68 = self._ready_requirement(
            "REQ-IP68", "VOC-WP-IP", "IP68 全密封", "20 年免开盖",
            customers=("华东风电整机厂",), safety=True, priority=Priority.P1,
        )
        thermal = self._ready_requirement(
            "REQ-TEMP45", "VOC-MN-TEMP", "高温持续运行", "45℃ 热容量",
            customers=("西北矿业集团",), safety=True, priority=Priority.P1,
        )
        self.loop.add_decision("DEC-ALL", "综合方案", (ip68, thermal))
        self.loop.add_verification(
            "VRF-SEAL", VerificationKind.PROTOTYPE, (ip68,), "DEC-ALL",
            result=VerificationResult.PASSED, report_ref="RPT-1", executed_at="2026-05-10",
        )
        self.loop.draft_configuration("CFG-MIX", "GBX-900", "混合机型", (ip68, thermal), ("DEC-ALL",))
        self.loop.release_configuration("CFG-MIX", released_at="2026-06-01")

        view = self.loop.configuration_view("CFG-MIX")
        self.assertEqual({r.req_code for r in view.effective_requirements},
                         {"REQ-IP68", "REQ-TEMP45"})
        self.assertEqual(view.uncovered_requirements, ["REQ-TEMP45"])
        self.assertTrue(
            any("安全相关需求 REQ-TEMP45 尚无通过结论的验证覆盖" in risk for risk in view.open_risks)
        )
        self.assertEqual(view.verification_coverage["REQ-IP68"][0].verification_code, "VRF-SEAL")
        self.assertEqual(view.delivered_serials, [])


if __name__ == "__main__":
    unittest.main()
