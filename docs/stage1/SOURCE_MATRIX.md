# Stage 1 数据源矩阵

状态：S1-04-R4 修复已提交、等待主验收。本文件固化 Stage 1 数据源的生产角色与当前评估
状态。权威来源是 `src/quantking/data/assessment/matrix.py` 的 `default_source_matrix()`；
文档不得替代源码与契约测试。

## 生产矩阵

| 源 | 数据集 | 角色 | 当前状态 | 阻塞原因 | auto_switch |
|---|---|---|---|---|---|
| XTQUANT | exchange_calendar | PRIMARY | PRODUCTION_READY | - | False |
| XTQUANT | 其余五类 | PRIMARY | BLOCKED_EXTERNAL_VALIDATION | dataset semantics require external validation | False |
| BAOSTOCK/AKSHARE/TUSHARE | 六类 | FALLBACK | BLOCKED_EXTERNAL_VALIDATION | requires authorized external validation | False |

## 规则

- XTQuant 是指定的 PRIMARY 生产源；其余三个为 FALLBACK 候选源。
- 三个 FALLBACK 源当前状态为 `BLOCKED_EXTERNAL_VALIDATION`，必须经授权真实外部验证后
  才能转为 `PRODUCTION_READY`。
- **不设 auto_switch**：系统不会在主源失败时静默切换到备用源；任何 fallback 选择必须通过
  显式、已审批的 `SourceOverride` 绑定到已验证的 `DifferenceReport`。
- 合成证据（`EvidenceKind.SYNTHETIC`）永远不能将任何源标记为 `PRODUCTION_READY`。
- `UNVERIFIED` 和 `BLOCKED_EXTERNAL_VALIDATION` 状态必须携带 `blocking_reason`；
  `PRODUCTION_READY` 不得携带 `blocking_reason`。

## 真实验证路径

FALLBACK 源的真实外部验证须在 S1-07 或专项验收中按项单独授权：

1. 在授权下用真实供应商响应运行 `SourceCandidateAssessment`，记录 `EvidenceKind.REAL`。
2. 通过 semantic contract、schema/unit、coverage、quality、fixed-sample differences、
   stability 全部门禁后，状态可转为 `PRODUCTION_READY`。
3. 未通过门禁或未授权时，状态保持 `BLOCKED_EXTERNAL_VALIDATION`，并保留阻塞原因。

合成测试不构成真实能力证明；离线测试通过不等于主验收通过。

## 不变量

- 矩阵不得声明 `auto_switch=True`；fallback 选择必须显式审批。
- 矩阵必须按 `source × dataset` 列出所有已知组合；条目按组合唯一。
- 主源变更必须通过架构决策，不得在任务级别静默修改。
