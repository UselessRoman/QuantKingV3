# Stage 1 / S1-04 主验收证据

## 结论与范围

- 验收日期：2026-07-22。
- 结论：S1-04（离线质量门、跨源差异、备用源评估与显式 `SourceOverride`）完成主验收。
- 主验收提交：`2f99491e9dc410acf85ca81c131a77cae08d0cc3`。
- Alembic 唯一 head：`20260722_0009`。
- 本结论确认离线、合成数据范围内的质量、比较、评估和 override 合同；不构成真实 XTQuant、
  QMT、BaoStock、AKShare 或 Tushare 能力证明。
- S1-05 已获离线实施授权但尚未开始；S1-06～S1-08 未开始、未获授权。Stage 1 尚未完成。

## 主验收确认

主验收重新审计了质量运行与发布凭证、差异报告、备用源评估、精确审批范围、故障恢复、
SQLite 防绕过约束和迁移往返，没有直接采信开发交接报告。最终确认：

- `QualityRunner` 只消费逐项验证的 PUBLISHED reference，质量失败阻断发布。
- `DifferenceReport` 绑定双方 batch、version、content SHA 和 PASSED quality run；报告产物、
  manifest、COMPLETE 和行级证据均需复验。
- 合成备用源证据不能标记 `PRODUCTION_READY`，默认矩阵不允许自动切换或自动接管。
- `field_replacements` 与 `fallback_only_keys` 是唯一权威审批范围；兼容摘要只能从精确单元
  确定性投影。
- FIELD_REPLACEMENT 与 ROW_INSERT 只影响明确批准的单元，生成同数据集直接子版本并重新
  通过完整质量门和原子发布。
- application fingerprint、精确范围双 SHA、DifferenceReport、manifest provenance、
  application/attempt 和 APPLIED 审计形成闭合血缘。
- 发布前失败使用新 child 重试；持久化提升后的中断恢复同一 child；失败证据稳定脱敏。
- application、attempt、PUBLISHED child 和 APPLIED override 的终态事实不可原地修改或删除。
- 旧 `0007` 非终态 override 缺少精确单元时在隔离迁移中安全阻断，不猜测批准范围，不修改
  原数据库。

## 独立验收结果

| 验证 | 结果 |
|---|---|
| R6/R5/迁移/并发定向回归 | `115 passed` |
| API 摘要与精确范围冲突 | 已拒绝 |
| SQLite 决策期精确范围替换 | 已拒绝 |
| 迁移触发器数量 | `0009=14`、`0008=14`、`0007=5`、往返 `0009=14` |
| 迁移往返触发器集合 | 完全一致，行为回归通过 |
| 完整离线 pytest | `788 passed, 2 skipped, 3 deselected` |
| Ruff lint / format | 通过 |
| mypy | 100 个源文件通过 |
| 架构边界 / Markdown 链接 / Git whitespace | 通过 |
| 前端 typecheck / 8 项单测 / 生产构建 / 1 项 E2E | 通过 |

完整 `scripts/quality.ps1` 由主验收 Agent 独立运行并以退出码 0 结束。4 条 warning 来自既有
第三方依赖弃用提示，不影响 S1-04 合同。

## 外部访问与后续边界

- 验收未访问网络、QMT、XTQuant 或任何备用供应商。
- 未读写 `E:\QuantKingV3Data`、`E:\QuantKing` 或 `F:\QuantKingV3Backups`。
- 全部数据测试使用 pytest 临时目录、SQLite、Parquet 和合成数据。
- 真实 XTQuant 五类能力证据和真实备用源评估仍须在 S1-07 或专项验收中逐项授权。
- S1-05 只能实现离线 EOD、Worker、CLI 与 API，不得引入调度器、自动切源、自动发布或
  任何下单/撤单能力。
