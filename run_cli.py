"""传动设备本地化设计闭环命令行冒烟入口。

演示：客户现场报告（带来源）-> 需求澄清/优先级 -> 安全冲突与设计决定 ->
计算/样机验证 -> 配置发布与序列号交付 -> 验证失败传播 -> 双向追踪。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from local_design import (
    ConflictType,
    LocalDesignLoop,
    Priority,
    Source,
    VerificationKind,
    VerificationResult,
)


def main() -> None:
    loop = LocalDesignLoop()

    # 1. 客户现场问题登记来源凭证（矿山粉尘/高温报告）
    source = Source(
        source_type="site_report",
        reference="SITE-MN-2026-017",
        customer="西北矿业集团",
        recorded_at="2026-03-02",
        contact="王工",
    )
    loop.register_voc("VOC-MN-TEMP", "GBX-900", "矿山井下 45℃ 持续运行，油温偏高", source)

    # 2. 转为带来源的需求，澄清并确认优先级
    loop.create_requirement(
        "REQ-TEMP45", "GBX-900", "高温持续运行",
        "环境温度 45℃ 下额定载荷持续运行，油温不超限",
        ("VOC-MN-TEMP",), applicable_customers=("西北矿业集团",), safety_related=True,
    )
    loop.clarify_requirement("REQ-TEMP45", "环境 45℃、额定载荷、24h 连续工况")
    loop.prioritize_requirement("REQ-TEMP45", Priority.P1)

    # 3. 设计决定与计算/样机验证
    loop.add_decision(
        "DEC-COOLING", "独立冷却回路+磁耦合密封", ("REQ-TEMP45",),
        calculation_refs=("CALC-THERM-02",),
    )
    loop.register_conflict(
        "CNF-SAFE-01", ConflictType.SAFETY, ("REQ-TEMP45",),
        "密封方案热容量不足（随冷却回路一并处置）",
    )
    loop.resolve_conflict("CNF-SAFE-01", "独立冷却回路保证热容量", "DEC-COOLING")
    loop.add_verification(
        "VRF-PROTO-01", VerificationKind.PROTOTYPE, ("REQ-TEMP45",), "DEC-COOLING",
        result=VerificationResult.PASSED, report_ref="RPT-PROTO-09",
        executed_at="2026-06-01",
    )

    # 4. 形成配置基线并发布、交付序列号
    loop.draft_configuration(
        "CFG-MINE", "GBX-900", "矿用减速机", ("REQ-TEMP45",), ("DEC-COOLING",)
    )
    released = loop.release_configuration("CFG-MINE", released_at="2026-07-01")
    loop.deliver_unit("SN-M-1001", "CFG-MINE", "西北矿业集团", "2026-07-10")

    # 5. 现场验证失败 -> 自动传播到受影响配置，已交付序列号保持原依据
    loop.add_verification(
        "VRF-FIELD-09", VerificationKind.FIELD, ("REQ-TEMP45",), "DEC-COOLING",
        result=VerificationResult.FAILED, report_ref="RPT-FIELD-09",
        executed_at="2026-09-01", failure_note="夏季井下实测油温超限 8℃",
    )

    view = loop.configuration_view("CFG-MINE")
    unit = loop.get_unit("SN-M-1001")
    summary = {
        "baseline_fingerprint": released.baseline_fingerprint,
        "delivered_serial": unit.serial_number,
        "serial_basis_unchanged": unit.baseline_fingerprint == released.baseline_fingerprint,
        "effective_requirements": [r.req_code for r in view.effective_requirements],
        "open_risks": view.open_risks,
        "open_changes": list(view.configuration.open_change_codes),
        "trace_from_voc": [
            {"kind": node.kind, "code": node.code} for node in loop.trace_from_voc("VOC-MN-TEMP")
        ],
        "trace_to_origin": [
            {"kind": node.kind, "code": node.code}
            for node in loop.trace_to_origin("DEC-COOLING")
        ],
        "audit_tail": list(loop.audit_log)[-3:],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
