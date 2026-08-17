# S1-07K4 任务卡：三角色数据路线与旧日线迁移

## 目标与边界

所属 Stage：Stage 1。状态：K4A/K4B 离线实现完成；K4C 第一次代表性真实三源实测经独立验收
**不通过**（S1-07K4C-R1），R1 离线修复已完成；**K4C-R2 真实实测已于 2026-08-16 获授权执行**
（36/36 成功、极值可复算、`representative_evidence_sufficient=false` 因三个 objective 未在
本窗口出现）；**K4C-R3 合同修订（ADR-022）与 Phase B 已按用户授权完成**（9/9 成功，R3 与
composite evidence 冻结）；**K4C-R4 证据链验证与停牌来源闭链修复（ADR-023）已交付并于
2026-08-16 独立验收通过**；**`comparison-thresholds.daily-bar-unadjusted.k4.v1`
（OHLC/`pre_close` 零容差、量额绝对 50/相对 0）已按用户决定接受重新冻结（ADR-024）**。
**K4D 真实只读 inventory 已于 2026-08-16 获授权执行一次并成功**（5,208 文件全部可读、零
读取错误、Stage 0 基线差异 0/0，ADR-025）。**K4D 真实 pilot 两次 attempt 均已获授权执行
并失败即停**（ADR-026；数据根零写入、授权已消费：第 1 次 2026-08-16 在第 6 次调用
000858.SZ × 新浪失败；第 2 次 2026-08-17 为 18/18 调用成功后触发
`PILOT_CANONICAL_COUNT_UNEXPECTED` 冻结守卫——8,609/26,200 行违反未复权契约被隔离）。
**旧 XTQuant 日线迁移已按用户决定拒绝并完成 K4D 决策收口（ADR-027）：不执行第 3 次
pilot、全量诊断或 full。**
**K4E 独立验收已于 2026-08-17 通过并形成单一 K4 提交；下一步是 S1-08R2**
（见 [S1-07K4 验收记录](S1-07K4_ACCEPTANCE.md)与
[S1-07K4 实施记录](S1-07K4_IMPLEMENTATION.md)）。前置决策：ADR-020、ADR-021、ADR-022、
ADR-023、ADR-024、ADR-025、ADR-026、ADR-027。

本任务把已提交的历史四角色实现迁移为唯一现行路线：

```text
BaoStock A + AKShare/腾讯 B → 常态互验
                         └→ 异常范围调用 AKShare/新浪 C
旧 XTQuant X → DATA_ONLY 候选评估 → inventory 成功 / pilot 失败 → ADR-027 迁移拒绝
任意发布行没有两个实际来源一致 → 隔离，不发布
```

ADR-027 拒绝迁移后，`XTQUANT_LEGACY` 不进入 ResolutionDecision、Raw、staging、Canonical、Warehouse 或任何发布链。

本任务拥有来源适配器、仲裁语义、数据质量、代表性实测、比较规则重基线和旧数据候选评估及拒绝处置。不拥有 SQLite 发布恢复、正式 R2 Job 或 Stage 1 最终验收；这些分别属于 S1-08R2/R3。

## K4A：三角色实现重构

- 删除东财正式 runtime、registry、预算和新 Job 指纹分支；保留历史 Raw、migration 和证据读取。
- 把腾讯固定为 B、 新浪固定为 C；更新 Resolution、DifferenceReport、artifact/verifier、
  DataCenter、CLI/Web 投影和 provider-neutral 测试。
- 正常任务只计划 A/B；C 只按受影响证券/日期调用。A/B 一致时 C 的 planned/actual 都为 0。
- BaoStock、腾讯、 新浪分别记录 provider/upstream、endpoint、库版本、批次、质量和错误状态。
- 所有服务调用使用适配器层硬超时或受控子进程；应用层零重试，预算不足在首次调用前拒绝。
- lookback、单位策略、路由、阈值和代码版本进入可重放输入指纹。

## K4B：仲裁与质量语义

- 任一来源持有行时，其他来源的共同缺行不能形成值共识；恰好一条实际行最终必须隔离。
- 多源合法整范围空结果使用独立 zero-row 原因码，不能冒充逐行价格一致。
- DifferenceReport 与 Resolution 共用一个版本化 Decimal `ThresholdSet`。
- Canonical `pre_close` 对全部服务源统一由完整未复权序列的前一行 `close` 派生；vendor 参考
  昨收只保留 Raw。
- 引入绑定版本和内容 SHA 的交易日历；逐证券检查预期交易日、缺口、新鲜度、上市/停牌例外。
- Raw/质量失败保存稳定脱敏错误码、异常链、影响计数和有界行级证据。

## K4C：代表性三源实测与规则冻结

离线实现和完整门禁通过后，先输出 dry-run，再单独申请真实网络授权。样本至少覆盖普通、除权、
停牌、低流动性、量额边界、缺行/空结果和沪深不同代码段。

- 固定证券、target/lookback、每源 endpoint/HTTP 上界、硬超时、零重试和失败即停。
- 验证 BaoStock、腾讯、 新浪的字段、单位、未复权、日期、完整性、错误和实际差值。
- OHLC/`pre_close` 默认零容差；量额容差只能由代表性证据和边界测试支持。
- 新阈值、单位和路由版本冻结为不可变合同；历史 K1/K3 证据继续可复验。

R3 补充样本（用户接受的合同修订，ADR-022）：缺失场景改用**上市前合法整范围空**
（`legal_full_range_empty_pre_listing`，成功码 `LEGAL_EMPTY_PRE_LISTING_OBSERVED`，
解释 `legal_pre_listing_empty`，永不声称 `vendor_partial_missing`）：688825.SH 上市日
2026-07-27，请求窗 2026-07-24（不跨上市日），A/B/C 全部 SUCCEEDED+complete+
`upstream_row_count=0`、零 raw/canonical 行、日历参考证明请求日为交易日且无预期交易会话、
composite 零行原因码 `ZERO_ROW_LEGAL_EMPTY_CONSENSUS`。禁止 error→empty、参数错误、
窗口跨上市日、预期交易日缺行、raw 有行而 comparison 空、缺上市/日历参考、与停牌重复计数。

一次小样本成功不证明全市场稳定性。失败必须保留证据，不能在同一授权下自动重跑或扩大范围。

## K4D：旧 XTQuant 日线 DATA_ONLY 迁移

清单、pilot、full 是三次独立授权，不得合并：

1. **只读 inventory**：扫描 `E:\QuantKing\data\kline\1d`，冻结相对路径、大小、mtime、
   SHA-256、schema、行数、代码/日期覆盖、空值、重复键和读取错误；旧根前后指纹一致。
2. **pilot**：固定少量代表性证券复制到受管 Raw/staging，确认字段、单位、未复权、停牌和
   `pre_close` 语义，与 A/B/C 复核；失败对象隔离。
3. **full**：全部候选导入或明确隔离，发现/读取/复制/转换/复核/隔离计数和字节守恒。

inventory 零读取 dry-run 已离线冻结（ADR-025，plan SHA `f4fee828…`）；实际只读 inventory
已于 2026-08-16 按该 plan SHA 与 `S1-07K4D-INVENTORY-REAL-READONLY-V1` 获授权执行一次
并成功（closed-scan 合同满足，5,208 文件/527,708,270 字节、零读取错误、基线差异 0/0，
evidence `evidence/k4d_inventory_2026-08-16.json`）。该授权已消费。pilot 离线合同、
执行器与零外部 I/O dry-run 已就绪（ADR-026，plan SHA `76e928aa…`）：六固定证券
（000001.SZ/000858.SZ/300750.SZ/600000.SH/688237.SH/688981.SH）、语义合同
`legacy-import-semantics.k4d.v1`（HYPOTHESIS/CONFIRMED）、单位/未复权确认规则、A/B/C
18 次调用预算（窗口 2025-05-01..2026-06-03）、K4 0/50 recheck、一次性 attempt 守卫的
staging 合同（语义确认成功后才写、永不发布 Warehouse）。

**最终处置（ADR-027，2026-08-17 用户决定）**：两次真实 pilot 均失败即停（第 1 次服务
调用失败；第 2 次 18/18 调用成功但 8,609/26,200 行因负 OHLC 等违反未复权契约被隔离，
`PILOT_CANONICAL_COUNT_UNEXPECTED` 守卫正确触发）；复权语义不可信被判定为系统性问题，
迁移**拒绝**，full 不再需要或授权。旧文件、inventory、两次失败 attempt 与诊断证据冻结
保留；`XTQUANT_LEGACY` 不进入正式 Raw、staging、Canonical、Warehouse 或发布候选；
legacy importer 离线代码与合成测试保留为历史审计框架。未来重启须作为新任务另行立项
授权，不得重放历史 authorization_id。

Stage 0 的 5,208 个文件、527,708,270 字节只是元数据基线，不得冒充实际成功迁移（迁移
已按 ADR-027 拒绝）。旧文件不修改、不硬链接、不启动旧项目、XTQuant 或 QMT。

## K4E：验收与交付（已于 2026-08-17 完成）

- 锁文件、Ruff、format、mypy、全部离线 pytest、前端、E2E、Markdown、`git diff --check`
  全部通过。
- 三角色 topology、仲裁/质量、真实实测与 K4D 拒绝迁移收口（ADR-027 冻结结论 + 两次
  失败 attempt 证据字节保护）由独立 agent 验收。
- 形成一个独立 K4 提交和不可变验收记录
  （[S1-07K4_ACCEPTANCE.md](S1-07K4_ACCEPTANCE.md)）；随后才能开始 S1-08R2 验收与
  S1-08R3 正式执行。旧 XTQuant full 不是 K4E 或 S1-08 的前置条件。

## 授权边界

任务卡和离线开发不授权网络、读取旧项目、写 `E:\QuantKingV3Data`、访问 F 盘、启动 QMT/
XTQuant/券商或开始 Stage 2。每个真实步骤必须有独立 marker、固定范围、预算、零重试和失败
后停止条件。
