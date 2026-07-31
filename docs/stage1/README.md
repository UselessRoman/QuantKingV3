# Stage 1：日线数据仓与数据质量

状态：Stage 0 已完成；Stage 1 已获实施授权并进行中，但尚未完成。本文描述目标范围与任务顺序，不代表功能已验收可用。

Stage 1 的目标是完成首个可发布、可查询和可追溯的 A 股日频数据版本。本目录记录 Stage 1 的任务顺序、范围、合同和验收要求。

## 当前状态

- Stage 0 已于 2026-07-16 完成验收，证据见 [Stage 0 最终验收报告](../stage0/FINAL_ACCEPTANCE.md)。
- Stage 1 已获得离线开发实施授权并进入进行中状态，但尚未完成。
- S1-01 已于 2026-07-16 在提交 `7e4e5b38b7fc91edaf8727ed55b1e26435373aea` 完成主验收；细节见 [S1-01 主验收证据](S1-01_ACCEPTANCE.md)。
- S1-02 已于 2026-07-18 在提交 `79f9fa3e682e212cf74e7886a04c6fb52158f302`
  完成主验收；细节见 [S1-02 主验收证据](S1-02_ACCEPTANCE.md)。
- S1-03-R1 修复中/待主验收；离线测试不代表主验收通过。
- S1-04 已于 2026-07-22 在提交 `2f99491e9dc410acf85ca81c131a77cae08d0cc3`
  完成主验收；细节见 [S1-04 主验收证据](S1-04_ACCEPTANCE.md)。
- S1-05 已于 2026-07-27 在提交 `e580d6963e01b1301dc26f29ed57a0b5b4770a0f`
  完成主验收；细节见 [S1-05 主验收证据](S1-05_ACCEPTANCE.md)。
- S1-06 已于 2026-07-28 在提交 `413805074b3a82c2d2a090d2b73c854a238e2e19`
  完成主验收；细节见 [S1-06 主验收证据](S1-06_ACCEPTANCE.md)。
- S1-07A 已于 2026-07-29 在提交 `37512be531e15bbd5a698e6a227b0753781ef5e6`
  完成主验收。该验收仅覆盖离线验证框架及其信任边界；24 个 source×dataset 单元仍为
  BLOCKED_EXTERNAL_VALIDATION，真实运行数为 0。
- S1-07B 最终实现已提交、等待主验收：权威核心样本 v4、首个 AUTHORITY_APPROVED
  case（603377.SH ST）、reference bundle v2 证据链和按 case 放行运行时已定版；
  17 个 purpose 仍是覆盖 backlog，不是 S1-07B 全局阻断门。
  S1-07C/D、S1-08 尚未开始、未获授权。

## 授权边界

Stage 1 已获得离线开发授权，但以下操作仍须按任务单独取得明确授权：

- 真实网络访问。
- 读取或下载真实 XTQuant 行情。
- 启动或连接 QMT。
- 写 `E:\QuantKingV3Data`。
- 读写 `E:\QuantKing`（旧项目）。
- 写 `F:\QuantKingV3Backups`。
- 旧项目 `DATA_ONLY` 数据迁移。

一次批准不自动延续到失败后的重试。所有默认离线测试只能使用 pytest 临时目录和合成数据。

## 任务顺序

详见 [任务卡](TASKS.md)。顺序为：

```text
S1-01 数据合同、元数据持久化与安全迁移基础（已主验收）
    → S1-02 Warehouse、原子发布与查询（已主验收）
    → S1-03 XTQuant 主数据适配器与增量抓取
    → S1-04 质量、跨源差异与 SourceOverride（已主验收）
    → S1-05 EOD、Worker、CLI 与 API（已主验收）
    → S1-06 数据中心 Web（已主验收）
    → S1-07A 离线验证框架（已主验收）
    → S1-07B 权威固定样本、引用证据合同和 REAL runtime/signer readiness
    → S1-07C 逐项授权的真实来源运行和差异评估
    → S1-07D 旧项目候选 DATA_ONLY/REJECTED 审计
    → S1-08 真实或明确范围 Canonical 构建及 Stage 1 最终验收
```

前一任务的完成条件未满足前，不得开始后一任务。

Stage 1 已获得离线开发授权。S1-01 已于 2026-07-16 完成主验收（提交
`7e4e5b38b7fc91edaf8727ed55b1e26435373aea`，证据见 [S1-01 主验收证据](S1-01_ACCEPTANCE.md)）。
S1-02 已于 2026-07-18 完成主验收（提交
`79f9fa3e682e212cf74e7886a04c6fb52158f302`，证据见
[S1-02 主验收证据](S1-02_ACCEPTANCE.md)）。S1-03-R1 修复中/待主验收；
该适配器只产生 Canonical 候选表，不调用正式发布。S1-04 已于 2026-07-22 完成主验收
（提交 `2f99491e9dc410acf85ca81c131a77cae08d0cc3`，证据见
[S1-04 主验收证据](S1-04_ACCEPTANCE.md)）。真实备用源验证仍须逐项授权。
S1-05 已于 2026-07-27 完成主验收（提交
`e580d6963e01b1301dc26f29ed57a0b5b4770a0f`，证据见
[S1-05 主验收证据](S1-05_ACCEPTANCE.md)）；该验收只使用 Fake Gateway、临时 SQLite
和临时 Warehouse，没有访问真实网络、QMT 或正式数据根。S1-06 已于 2026-07-28 在提交
`413805074b3a82c2d2a090d2b73c854a238e2e19` 完成主验收，证据见
[S1-06 主验收证据](S1-06_ACCEPTANCE.md)；S1-07A 已于 2026-07-29 在提交
`37512be531e15bbd5a698e6a227b0753781ef5e6` 完成主验收。该验收仅覆盖离线验证框架及其
信任边界；24 个 source×dataset 单元仍为 BLOCKED_EXTERNAL_VALIDATION，真实运行数为 0。
S1-07B 最终实现已提交、等待主验收：权威核心样本 v4、首个 AUTHORITY_APPROVED
case（603377.SH ST）、reference bundle v2 证据链和按 case 放行运行时已定版；
17 个 purpose 仍是覆盖 backlog，不是 S1-07B 全局阻断门。
S1-07C/D、S1-08 尚未开始、未获授权。

## S1-06 主验收

数据中心现提供 framework-neutral 查询服务与窄 Web port。SQLite 仅读元数据，
Canonical 预览每次先复验 `PUBLISHED` 状态、SQLite 凭证、manifest、`COMPLETE`、
文件集合和 SHA，再由 DuckDB 按 Canonical sort key 执行字段 allowlist、过滤、keyset
和 `LIMIT`。统一 DifferenceReport loader 精确交叉绑定 SQLite 与 artifact 的双方
source/batch/version/content/quality、样本、阈值、汇总、创建时间和报告 SHA，并重新复验
双方 PUBLISHED Warehouse 与 PASSED QualityRun。Override 详情不再吞掉异常，会区分从未
申请、FAILED attempt 和 active application，并核对 fingerprint、child 及 manifest
`source_override` provenance。

R2 在读侧加入双向 Override 状态矩阵：`PENDING/REJECTED` 不得携带 application，
`APPROVED` 只接受无 application、历史 `FAILED`、active `CLAIMED` 或发布恢复窗口
`PUBLISHED`，`APPLIED` 必须绑定同一 active application、attempt 和已发布 child。
总览失败数由 SQLite 对批次、版本、质量和 `data.eod.v1` Job 分别执行聚合计数，状态集合
与统一失败列表一致，不再受列表页上限影响。

React 页面覆盖总览、来源、批次、版本与显式预览、质量、差异、Override、EOD 和失败。
所有 Cursor 绑定规范查询指纹；列表上限 100、默认 50，预览上限 200。Playwright 使用
临时 SQLite、真实查询服务、Warehouse/DuckDB 与 verifier，不注入内存数据中心；EOD
只通过既有命令服务入队，不启动 Worker。S1-06 已完成主验收；该结论不表示 Stage 1
完成，也不授权 S1-07、S1-08 或任何真实外部访问。

## 失败产物纪律

- 失败产物不得原地修复或改成成功。
- 失败目录、日志和输入清单必须保留。
- 修复必须创建新任务和新 ID。
- `PUBLISHED` 数据版本和 `APPLIED` override 不可原地修改或退回。
- `FAILED` 版本和失败批次不能转成成功；重试必须创建新 ID。

## 相关文档

- [任务卡](TASKS.md)
- [验收矩阵](ACCEPTANCE.md)
- [S1-06 主验收证据](S1-06_ACCEPTANCE.md)
- [S1-07A 离线框架交接证据](S1-07A_ACCEPTANCE.md)
- [S1-07B 最终实现交接](S1-07B_ACCEPTANCE.md)
- [S1-02 主验收证据](S1-02_ACCEPTANCE.md)
- [S1-03 离线验收证据](S1-03_ACCEPTANCE.md)
- [S1-04 主验收证据](S1-04_ACCEPTANCE.md)
- [S1-05 主验收证据](S1-05_ACCEPTANCE.md)
- [数据合同](DATA_CONTRACTS.md)
- [质量与跨源合同](QUALITY_CONTRACTS.md)
- [数据源矩阵](SOURCE_MATRIX.md)
- [数据架构规范](../DATA_ARCHITECTURE.md)
- [实施计划](../IMPLEMENTATION_PLAN.md)
