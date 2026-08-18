"""The compact Stage 1 documentation must stay bounded and internally consistent."""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_STAGE1 = _ROOT / "docs" / "stage1"


def _read(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    (
        "README.md",
        "CONTRACTS.md",
        "ACCEPTANCE.md",
        "S1-07K3_TASK.md",
        "S1-07K4_TASK.md",
        "S1-08_TASK.md",
        "S1-08R2_TASK.md",
        "S1-08R3_TASK.md",
        "S1-01_ACCEPTANCE.md",
        "S1-02_ACCEPTANCE.md",
        "S1-03_ACCEPTANCE.md",
        "S1-04_ACCEPTANCE.md",
        "S1-05_ACCEPTANCE.md",
        "S1-06_ACCEPTANCE.md",
        "S1-07K4_ACCEPTANCE.md",
        "S1-07K4_IMPLEMENTATION.md",
        "S1-08R2_IMPLEMENTATION.md",
        "S1-08R2_ACCEPTANCE.md",
        "S1-08R3_IMPLEMENTATION.md",
        "S1-08_ACCEPTANCE.md",
    ),
)
def test_compact_stage1_documents_exist(name: str) -> None:
    assert (_STAGE1 / name).is_file()


@pytest.mark.parametrize(
    "name",
    (
        "DATA_CONTRACTS.md",
        "QUALITY_CONTRACTS.md",
        "SOURCE_MATRIX.md",
        "HANDOFF.md",
        "STAGE1_COMPLETION_HANDOFF.md",
        "TASKS.md",
        "S1-07K1_TASK.md",
        "S1-07K1_IMPLEMENTATION_HANDOFF.md",
        "S1-07K2_TASK.md",
        "S1-07K2_IMPLEMENTATION_HANDOFF.md",
    ),
)
def test_superseded_current_documents_are_removed(name: str) -> None:
    assert not (_STAGE1 / name).exists()


def test_current_status_records_stage1_completion_without_starting_stage2() -> None:
    combined = "\n".join(
        _read(relative)
        for relative in (
            "README.md",
            "docs/ARCHITECTURE.md",
            "docs/ROADMAP.md",
            "docs/stage1/README.md",
            "docs/stage1/ACCEPTANCE.md",
        )
    )
    assert "f98f1d9" in combined
    assert "0ebf84b" in combined
    assert "S1-08" in combined
    assert "S1-08 首次" in combined
    assert "RESOLUTION_BLOCKED" in combined
    assert "授权已结束" in combined or "授权已经结束" in combined
    assert "Stage 1 完成" in combined
    assert "Stage 2 前置已满足" in combined
    assert "Stage 2" in combined and "未授权" in combined
    assert "Stage 1 尚未完成" not in combined
    assert "R3D2 待执行" not in combined


def test_single_contract_keeps_history_and_the_accepted_three_role_route() -> None:
    text = _read("docs/stage1/CONTRACTS.md")
    for phrase in (
        "daily_bar_unadjusted.v1",
        "query_history_k_data_plus",
        "stock_zh_a_hist_tx",
        "stock_zh_a_daily",
        "BAOSTOCK",
        "TENCENT",
        "SINA",
        "ADR-021",
        "东财和 FTShare 已退出正式来源集合",
        "XTQUANT_LEGACY",
        "lookback_days=15",
        "comparison-thresholds.daily-bar-unadjusted.k1.v1",
        "ResolutionDecision",
        "SourceOverride",
        "无两源一致",
        "(code, trade_date)",
        "共同缺行不构成",
    ):
        assert phrase in text


def test_s1_08_history_records_the_bounded_failed_formal_run() -> None:
    text = _read("docs/stage1/S1-08_TASK.md")
    for phrase in (
        "首次正式运行的不可变事实",
        "RESOLUTION_BLOCKED",
        "conflict 80",
        "授权已消耗",
        "000001.SZ",
        "600000.SH",
        "2026-06-11..2026-08-09",
        "2026-05-27..2026-08-09",
        "共 12 次",
        "上界估算 24",
        "应用层零重试",
        "E:\\QuantKingV3Data",
        "S1-07K4",
        "S1-08R2",
        "S1-08R3",
        "Stage 2 未开始",
    ):
        assert phrase in text


def test_k3_history_preserves_rejected_routes_and_migration_baseline() -> None:
    text = _read("docs/stage1/S1-07K3_TASK.md")
    for phrase in (
        "BAOSTOCK / BAOSTOCK",
        "AKSHARE / EASTMONEY",
        "AKSHARE / TENCENT",
        "AKSHARE / SINA",
        "XTQUANT_LEGACY / XTQUANT",
        "5,208",
        "527,708,270",
        "DATA_ONLY",
        "应用层零自动重试",
        "S1-07K4",
        "S1-08R2",
    ):
        assert phrase in text


def test_successor_cards_have_single_ownership_and_strict_order() -> None:
    k4 = _read("docs/stage1/S1-07K4_TASK.md")
    for phrase in (
        "BaoStock A + AKShare/腾讯 B",
        "三角色实现重构",
        "仲裁与质量语义",
        "代表性三源实测与规则冻结",
        "旧 XTQuant 日线 DATA_ONLY 迁移",
        "旧根前后指纹一致",
    ):
        assert phrase in k4

    r2 = _read("docs/stage1/S1-08R2_TASK.md")
    for phrase in (
        "发布引擎离线收口",
        "SQLITE_BUSY/SQLITE_LOCKED",
        "manifest 驱动的范围和预算",
        "只读 verifier",
        "不执行真实网络或正式 Job",
    ):
        assert phrase in r2

    r3 = _read("docs/stage1/S1-08R3_TASK.md")
    for phrase in (
        "正式发布与 Stage 1 终验",
        "唯一正式 Job",
        "PUBLISHED DatasetVersion",
        "独立只读复验与双幂等",
        "S1-08_ACCEPTANCE.md",
    ):
        assert phrase in r3


def test_k4_task_card_alone_carries_the_rejection_flow_not_the_import_route() -> None:
    """The K4 task card itself must not mix the retired import route with ADR-027.

    Reading only S1-07K4_TASK.md (never a concatenation with other docs),
    the retired "managed import into the same ResolutionDecision" flow and
    the old ownership sentence must be absent, while the ADR-027
    candidate-evaluation / rejection / exclusion statements must be present.
    This catches a task card that keeps both the new and the old wording.
    """

    normalized = " ".join(_read("docs/stage1/S1-07K4_TASK.md").split())
    for retired in (
        "旧 XTQuant X → DATA_ONLY 受管导入 → 同一 ResolutionDecision",
        "本任务拥有来源适配器、仲裁语义、数据质量、代表性实测、比较规则重基线和旧数据迁移。",
    ):
        assert retired not in normalized
    for required in (
        "旧 XTQuant X → DATA_ONLY 候选评估 → inventory 成功 / pilot 失败 → ADR-027 迁移拒绝",
        "XTQUANT_LEGACY` 不进入 ResolutionDecision、Raw、staging、Canonical、Warehouse"
        " 或任何发布链",
        "旧数据候选评估及拒绝处置",
        "ADR-027",
    ):
        assert required in normalized


def test_stage1_history_and_architecture_decisions_remain_auditable() -> None:
    for task in ("S1-01", "S1-02", "S1-03", "S1-04", "S1-05", "S1-06"):
        assert (_STAGE1 / f"{task}_ACCEPTANCE.md").is_file()
    decisions = _read("docs/DECISIONS.md")
    assert "ADR-018" in decisions
    assert "ADR-021" in decisions
    assert "Stage 1 三角色来源路线" in decisions
    assert "Stage 1 控制面简化" in decisions
    assert "生产运行时只支持 `daily_bar_unadjusted`" in decisions


def test_retired_s1_07_modules_and_current_documents_are_absent() -> None:
    assert not list((_ROOT / "src" / "quantking" / "data" / "validation").glob("*.py"))
    assert not list((_ROOT / "tests" / "data" / "validation").glob("*.py"))
    assert not (_STAGE1 / "S1-07C1C1_RUNBOOK.md").exists()


def test_current_status_docs_record_k4c_r4_acceptance_and_k4d_readiness() -> None:
    combined = " ".join(
        " ".join(_read(relative).split())
        for relative in (
            "README.md",
            "docs/ARCHITECTURE.md",
            "docs/IMPLEMENTATION_PLAN.md",
            "docs/DATA_ARCHITECTURE.md",
            "docs/ROADMAP.md",
            "docs/stage1/README.md",
            "docs/stage1/ACCEPTANCE.md",
            "docs/stage1/CONTRACTS.md",
        )
    )
    assert "K4C-R4 已于 2026-08-16 独立验收通过" in combined
    assert "ADR-024" in combined
    assert "K4D inventory dry-run" in combined
    assert "只读 inventory 已于 2026-08-16 获授权执行" in combined
    assert "ADR-025" in combined
    assert "ADR-026" in combined
    assert "真实 pilot 两次 attempt" in combined
    assert "失败即停" in combined
    assert "数据根零写入" in combined
    for outdated in (
        "样本选择 BLOCKED",
        "阈值重新冻结建议待独立验收",
        "阈值重新验收待独立验收",
        "阈值重新冻结待独立验收",
        "真实复测待授权",
        "真实复测须另行授权",
        "R3 Phase A 离线完成、样本选择 BLOCKED",
        "旧根尚未读取",
        "旧根迁移仍未授权",
        "K4D 旧根迁移授权",
        "真实 pilot 未执行",
        "pilot 成功",
        "pilot 已完成",
        "pilot 重试/full",
        "pilot 修复重试/full",
        "K4D full 待授权",
        "full 迁移仍未授权",
        "重试待修复后授权",
        "重试须先修复证据链留证",
        "修订冻结预期或扩大诊断面",
    ):
        assert outdated not in combined


def test_current_status_docs_record_k4d_migration_rejection_closeout() -> None:
    """The ADR-027 rejection closeout is the single authoritative K4D state.

    The docs must state that the legacy candidate evaluation finished with a
    frozen rejection (not a successful migration), that no third pilot, full
    scan, or full import is pending authorization, and that K4E is next.
    """

    combined = " ".join(
        " ".join(_read(relative).split())
        for relative in (
            "README.md",
            "AGENTS.md",
            "docs/ARCHITECTURE.md",
            "docs/IMPLEMENTATION_PLAN.md",
            "docs/DATA_ARCHITECTURE.md",
            "docs/ROADMAP.md",
            "docs/DECISIONS.md",
            "docs/stage1/README.md",
            "docs/stage1/ACCEPTANCE.md",
            "docs/stage1/CONTRACTS.md",
            "docs/stage1/S1-07K4_TASK.md",
            "docs/stage1/S1-07K4_IMPLEMENTATION.md",
        )
    )
    assert "ADR-027" in combined
    assert "拒绝旧 XTQuant 日线迁移" in combined
    assert "K4D 决策收口" in combined
    assert "评估完成、迁移拒绝" in combined
    assert "XTQUANT_LEGACY` 不进入发布链" in combined or "XTQUANT_LEGACY` 不进入正式" in combined
    assert "两次 attempt" in combined
    assert "PILOT_CANONICAL_COUNT_UNEXPECTED" in combined
    assert "8,609" in combined
    for forbidden in (
        "已成功迁移",
        "迁移成功",
        "迁移已完成",
        "pilot 全部成功并完成迁移",
        "虚假成功 evidence",
        "第三次 pilot 已执行",
        "第 3 次 pilot 已执行",
    ):
        assert forbidden not in combined


def test_adr_027_rejection_decision_is_recorded_with_frozen_facts() -> None:
    decisions = _read("docs/DECISIONS.md")
    assert "ADR-027" in decisions
    assert "拒绝旧 XTQuant DATA_ONLY 日线迁移" in decisions
    assert "已接受" in decisions
    assert "2026-08-17" in decisions
    for fact in (
        "不保证任何候选最终可迁移",
        "8,609 行",
        "32.9%",
        "PILOT_CANONICAL_COUNT_UNEXPECTED",
        "不执行第 3 次 pilot",
        "不执行 K4D full",
        "评估完成、迁移拒绝",
        "不得重放任何历史 authorization_id",
    ):
        assert fact in decisions


def test_k4d_inventory_dry_run_evidence_is_registered() -> None:
    text = _read("docs/stage1/evidence/README.md")
    assert "k4d_inventory_dry_run_2026-08-16.json" in text
    assert "46272de4" in text
    assert "f4fee828" in text


def test_k4d_pilot_dry_run_evidence_is_registered() -> None:
    text = _read("docs/stage1/evidence/README.md")
    assert "k4d_pilot_dry_run_2026-08-16.json" in text
    assert "c77b2d8e" in text
    assert "76e928aa" in text


def test_current_status_docs_record_stage1_acceptance_and_stage2_boundary() -> None:
    """Current status must close Stage 1 without authorizing Stage 2."""

    current_status = " ".join(
        " ".join(_read(relative).split())
        for relative in (
            "AGENTS.md",
            "README.md",
            "docs/ARCHITECTURE.md",
            "docs/DATA_ARCHITECTURE.md",
            "docs/IMPLEMENTATION_PLAN.md",
            "docs/ROADMAP.md",
            "docs/stage1/README.md",
            "docs/stage1/ACCEPTANCE.md",
            "docs/stage1/S1-08R3_TASK.md",
            "docs/stage2/README.md",
            "docs/stage2/TASKS.md",
        )
    )
    for required in (
        "S1-07K4 独立验收通过",
        "形成单一 K4 提交",
        "S1-08R2 独立只读验收通过",
        "S1-08R3D2-R2",
        "Stage 1 完成",
        "Stage 2 前置已满足",
        "Stage 2 实施仍未获得用户单独授权",
        "S2-01",
    ):
        assert required in current_status
    for outdated in (
        "K4E 独立验收与提交是下一步",
        "剩余 K4E 独立验收与提交",
        "剩余 K4E 独立提交",
        "K4E 独立提交尚未执行",
        "K4E 独立验收与提交待执行",
        "K4E 独立提交与 S1-08R2/R3 仍须分别授权",
        "K4 提交与不可变验收记录尚未完成",
        "K4E 待验收",
        "K4E 待提交",
        "S1-08R2 待开发",
        "下一步是 S1-08R2",
        "下一步是 S1-08R3",
        "Stage 1 尚未完成",
        "R3D2 待执行",
    ):
        assert outdated not in current_status

    acceptance = _read("docs/stage1/S1-07K4_ACCEPTANCE.md")
    for phrase in (
        "S1-07K4 独立验收通过",
        "K4D 迁移未成功",
        "K4D 候选评估及拒绝决策已完成",
        "b8f4859",
        "2026-08-17",
        "本验收记录与 S1-07K4 全部变更位于同一个 K4 提交中",
        "140978ea3e3f4d9139c15d641c92564af8f58928bf77017117393ce988a04ce2",
        "b75f27b4096571f04eb790e8cd78f1da1faf174187dc44ab0ca09d20fe1c02e1",
        "87b04863756a6e92614f62fe26cfb7d8b1b591c101bcc28d902a7fc3c1bdd300",
        "368 passed",
    ):
        assert phrase in acceptance
    for pending in ("待执行", "待验收", "待提交", "待完成"):
        assert pending not in acceptance

    matrix = _read("docs/stage1/ACCEPTANCE.md")
    assert "S1-07K4_ACCEPTANCE.md" in matrix
    assert "已主验收" in matrix


def test_s1_08_r2_acceptance_record_is_frozen_with_real_gate_numbers() -> None:
    """The R2 acceptance record must carry the independently rerun numbers.

    The record must state the pass verdict, the baseline commit, the offline
    scope (zero network / legacy root / data-root writes / F drive), the
    independently rerun full-suite count, and the R3 authorization boundary.
    """

    acceptance = _read("docs/stage1/S1-08R2_ACCEPTANCE.md")
    for phrase in (
        "S1-08R2 独立验收通过",
        "2026-08-17",
        "48d8180",
        "1309 passed",
        "1 passed",
        "纯离线只读验收",
        "零真实网络",
        "零旧根读取",
        "零正式数据根写入",
        "零 F 盘访问",
        "e7b9ca1c869dded7c12602f14067d1125423a55293dc2a7a392b4693098d3b9e",
        "f3786ef74aef7939e0807a6da19c78655cb40e62af1cac0ac9f3521f24ed3230",
        "下一步是 S1-08R3",
        "须由单独任务与新授权启动",
        "Stage 1 尚未完成",
        "Stage 2 未开始",
    ):
        assert phrase in acceptance
    for pending in ("待验收", "待提交", "待完成", "验收不通过"):
        assert pending not in acceptance

    matrix = _read("docs/stage1/ACCEPTANCE.md")
    assert "S1-08R2_ACCEPTANCE.md" in matrix
    task = _read("docs/stage1/S1-08R2_TASK.md")
    assert "已验收" in task


def test_adr_028_personal_local_trust_model_and_final_r3_state_are_recorded() -> None:
    """ADR-028, R3 fixes and the independent final acceptance stay auditable."""

    decisions = _read("docs/DECISIONS.md")
    for phrase in (
        "ADR-028",
        "个人本地信任模型与适度授权控制",
        "同一管理边界",
        "防止误执行",
        "不以防御本机管理员或恶意本地进程为目标",
        "不是密钥或隐私",
        "流程偏差",
        "一次性 attempt ledger",
        "两个\n   实际来源",
    ):
        assert phrase in decisions

    implementation = _read("docs/stage1/S1-08R3_IMPLEMENTATION.md")
    for phrase in (
        "S1-08R3F-LITE 精简修复轮",
        "<marker>:<authorization_id>:<envelope_sha256>",
        "assert_formal_entry_boundary",
        "parse_calendar_bootstrap_plan",
        "calendar_bootstrap_token",
        "本次未重跑 R3C",
        "verify_release_row_proof",
        "S1-08R3-R3V2-READONLY-VERIFY-V1",
        "S1-08R3-R3V2-7e4c1b9d2a68",
        "R3V2 纯只读复验",
        "已批准并执行",
        "2026-08-18 VERIFIED",
        "82/82 全部 passed",
        "E1 未重跑、C1 未重跑、R3C 未重跑",
        "R3D2-R2 最终独立验收",
        "S1-08R3D2-R2 PASS",
        "Stage 1 完成",
    ):
        assert phrase in implementation
    for forbidden in (
        "R3C 已重跑",
        "E1 已重跑",
        "C1 已重跑",
        "Stage 2 已授权",
    ):
        assert forbidden not in implementation
    acceptance = _read("docs/stage1/S1-08_ACCEPTANCE.md")
    for phrase in (
        "S1-08R3D2-R2",
        "最终判定",
        "eac4eac10816747dff13002c9fab7655c1bddb27",
        "d95db32",
        "58 passed",
        "1440 passed / 6 skipped / 3 deselected",
        "1442 passed / 6 skipped / 3 deselected",
        "82 个唯一",
        "A/B 实际来源行各 82",
        "6f7f906e066a3d35fccadb0e2a3bbc52e3575e61612f6a18e92fbbee0e3c686c",
        "6c96fe46ca1ffffb6259198b686568a31ae08a28437d54a7f8e5d4e4b6929591",
        "Stage 2 实施仍未获得用户单独授权",
    ):
        assert phrase in acceptance

    contracts = _read("docs/stage1/CONTRACTS.md")
    assert "个人本地信任模型与正式入口边界" in contracts
    assert "ADR-028 / S1-08R3F" in contracts
    assert "不是身份认证" in contracts

    evidence = _read("docs/stage1/evidence/README.md")
    for phrase in (
        "s1_08r3_r3v2_plan_2026-08-18.json",
        "cff0c1a8f791cf80addb2f91c72b85abd2d871f3b34f7f6403cc13a18f7473c4",
        "s1_08r3_r3v2_result_2026-08-18.json",
        "82/82 全部 passed",
        "s1_08r3_r3v2_row_proof_2026-08-18.jsonl",
    ):
        assert phrase in evidence
