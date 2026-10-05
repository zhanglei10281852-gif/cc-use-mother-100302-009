"""传动设备本地化设计闭环命令行冒烟入口。

演示从客户现场问题/销售口头承诺到设计基线发布与序列号追溯的完整链路。
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from datetime import date

from local_design import (
    ConflictKind,
    EventKind,
    LocalDesignService,
    Priority,
    ReleaseGateError,
    RequirementStatus,
    SourceKind,
    SubstitutionStatus,
    VerificationStatus,
)
from local_design import LocalizationCase


TODAY = date(2026, 10, 5)


def build_demo() -> LocalDesignService:
    svc = LocalDesignService()

    # 1) 客户声音：现场问题 + 销售口头承诺（必须有会议纪要作为凭证）
    svc.record_voice(
        "v-dust", "case-rv-01", SourceKind.FIELD_REPORT, "FR-2026-031",
        "某矿业集团", date(2026, 3, 10),
        "井下粉尘导致密封失效，要求 IP66 以上防护", "现场工程师王工",
    )
    svc.record_voice(
        "v-noise", "case-rv-01", SourceKind.SALES_VERBAL, "MM-2026-044-会议纪要",
        "某矿业集团", date(2026, 4, 2),
        "销售口头承诺 1 米处噪音 ≤75dB", "销售李工",
    )

    # 2) 转成带来源的需求，澄清并确认优先级
    svc.create_requirement(
        "req-dust", "case-rv-01", ("v-dust",),
        "高粉尘防护", "外壳防护不低于 IP66", ("environment",), ("某矿业集团",),
    )
    svc.create_requirement(
        "req-noise", "case-rv-01", ("v-noise",),
        "低噪音", "1 米处声压级 ≤75dB", ("environment",), ("某矿业集团",),
    )
    svc.clarify_requirement("req-dust")
    svc.clarify_requirement("req-noise")
    svc.confirm_requirement("req-dust", Priority.P1_SAFETY)
    svc.confirm_requirement("req-noise", Priority.P3_MEDIUM)

    # 3) 矿山配置
    svc.create_configuration("cfg-mining", "case-rv-01", "RV 矿山型", ("某矿业集团",))
    svc.assign_requirements("cfg-mining", ("req-dust", "req-noise"))

    # 4) 计算与样机验证
    svc.record_calculation("calc-dust", "cfg-mining", "密封防护校核", "GB/T 4208",
                           ("req-dust",), VerificationStatus.PASSED, "满足 IP66")
    svc.record_calculation("calc-noise", "cfg-mining", "噪音校核", "GB/T 6404",
                           ("req-noise",), VerificationStatus.PASSED, "预估 73dB")
    svc.record_validation("val-dust", "cfg-mining", "PT-M01", ("req-dust",),
                          VerificationStatus.PASSED, "粉尘试验通过", TODAY)
    svc.record_validation("val-noise", "cfg-mining", "PT-M01", ("req-noise",),
                          VerificationStatus.PASSED, "实测 72dB", TODAY)

    # 5) 噪音只能做到 78dB：非安全冲突，按偏差接受（限定客户与期限）
    svc.register_conflict(
        "c-noise", ConflictKind.REQUIREMENT, False,
        "样机实测 78dB，高于承诺的 75dB", requirement_ids=("req-noise",),
    )
    svc.accept_deviation(
        "c-noise", "井下无人值守区域可接受 78dB，2027 年前完成低噪改型",
        ("某矿业集团",), date(2027, 12, 31), TODAY,
    )

    # 6) 零件停产 -> 供应商替代 -> 工程变更 -> 设计决定关闭冲突
    svc.add_part_usage("cfg-mining", "BRG-6208", "深沟球轴承", "某外资轴承厂")
    svc.report_part_obsolescence("BRG-6208", "供应商发布 EOL 通知", TODAY)
    svc.propose_substitution("sub-bearing", "cfg-mining", "BRG-6208", "国产轴承厂", "国产化替代")
    svc.record_change("ec-bearing", "cfg-mining", "轴承供应商切换", "sub-bearing",
                      linked_requirement_ids=("req-dust",))
    svc.approve_substitution("sub-bearing", change_id="ec-bearing")
    svc.implement_change("ec-bearing")
    svc.record_decision(
        "d-bearing", "cfg-mining", "采用国产等型号轴承并加严入厂检验",
        "国产 BRG-6208", ("继续寻货原厂",), ("req-dust",),
        linked_conflict_id="conflict-ob-BRG-6208-cfg-mining", decided_at=TODAY,
    )
    svc.resolve_conflict("conflict-ob-BRG-6208-cfg-mining", "d-bearing")

    # 7) 发布基线并交付序列号
    baseline = svc.release_baseline("cfg-mining", "bl-mining-1", TODAY)
    svc.deliver_unit("SN-2026-0001", "cfg-mining", TODAY)

    # 8) 交付后法规更新：传播到配置与已交付序列号，旧序列号依据冻结
    svc.report_regulation_update(
        "GB-X-2027", "矿山设备新增外部防火要求", TODAY,
        config_ids=("cfg-mining",), requirement_ids=("req-dust",),
    )
    return svc, baseline


def main() -> None:
    item = LocalizationCase(
        **{'case_code': 'case-code-001', 'product_line': 'product-line-001',
           'customer': 'customer-001', 'revision': 1}
    )
    print(json.dumps({"item": asdict(item), "fingerprint": item.fingerprint()},
                     ensure_ascii=False, sort_keys=True))

    svc, baseline = build_demo()

    print("\n=== 基线发布 ===")
    print(json.dumps({
        "baseline_id": baseline.baseline_id,
        "revision": baseline.revision,
        "fingerprint": baseline.fingerprint,
        "accepted_deviation_ids": list(baseline.accepted_deviation_ids),
        "substitution_ids": list(baseline.substitution_ids),
    }, ensure_ascii=False, indent=2))

    print("\n=== 法规更新后的开放风险（安全冲突置顶）===")
    print(json.dumps(svc.open_risks(config_id="cfg-mining"), ensure_ascii=False, indent=2))

    print("\n=== 已交付序列号依据（冻结，含漂移标注）===")
    print(json.dumps(svc.unit_delivery_basis("SN-2026-0001"), ensure_ascii=False, indent=2))

    print("\n=== 客户声音 -> 设计决定 双向追踪 ===")
    trace = svc.trace_requirement("req-dust")
    print(json.dumps({
        "req_id": trace["requirement"]["req_id"],
        "upstream_voices": trace["voices"],
        "decisions": trace["decisions"],
        "baselines": trace["baselines"],
        "delivered_serials": trace["delivered_serials"],
    }, ensure_ascii=False, indent=2))

    print("\n=== 事件传播记录 ===")
    print(json.dumps([
        {"event_id": e.event_id, "kind": e.kind.value,
         "affected_configs": list(e.affected_config_ids),
         "affected_serials": list(e.affected_unit_serials)}
        for e in svc.events.values()
    ], ensure_ascii=False, indent=2))

    # 门禁演示：法规冲突开放期间不得再交付
    try:
        svc.deliver_unit("SN-2026-0002", "cfg-mining", TODAY)
    except ReleaseGateError as exc:
        print(f"\n发布门禁生效：{exc}")


if __name__ == "__main__":
    main()
