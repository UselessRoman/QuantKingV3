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
- S1-05-R3 修复已提交、等待主验收；S1-06～S1-08 未开始、未获授权。

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
    → S1-05 EOD、Worker、CLI 与 API
    → S1-06 数据中心 Web
    → S1-07 经授权真实样本与旧数据 DATA_ONLY 判定
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
S1-05-R3 修复已提交、等待主验收；实现只使用 Fake Gateway、临时 SQLite 和临时
Warehouse，没有访问真实网络、QMT 或正式数据根。S1-06～S1-08 未开始、未获授权。

## 失败产物纪律

- 失败产物不得原地修复或改成成功。
- 失败目录、日志和输入清单必须保留。
- 修复必须创建新任务和新 ID。
- `PUBLISHED` 数据版本和 `APPLIED` override 不可原地修改或退回。
- `FAILED` 版本和失败批次不能转成成功；重试必须创建新 ID。

## 相关文档

- [任务卡](TASKS.md)
- [验收矩阵](ACCEPTANCE.md)
- [S1-02 主验收证据](S1-02_ACCEPTANCE.md)
- [S1-03 离线验收证据](S1-03_ACCEPTANCE.md)
- [S1-04 主验收证据](S1-04_ACCEPTANCE.md)
- [数据合同](DATA_CONTRACTS.md)
- [质量与跨源合同](QUALITY_CONTRACTS.md)
- [数据源矩阵](SOURCE_MATRIX.md)
- [数据架构规范](../DATA_ARCHITECTURE.md)
- [实施计划](../IMPLEMENTATION_PLAN.md)
