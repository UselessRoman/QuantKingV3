# Stage 1 / S1-02 主验收证据

## 结论与范围

- 验收日期：2026-07-18。
- 结论：S1-02（Warehouse、原子发布与显式版本查询）完成主验收。
- 原实现提交：`3235aba1fe758394bcee39f03945f5cd2ab4aa83`。
- R1 提交：`d3d8b17f66d8f0e9e8bb9faaa801b7d1b7d7fb47`。
- 主验收补强提交：`79f9fa3e682e212cf74e7886a04c6fb52158f302`。
- S1-03～S1-08 未开始、未获授权；本结论不代表 Stage 1 整体完成。

## 独立验收与修复

主验收没有直接采信 R1 交接报告，而是重新审计路径边界、发布协调、磁盘与 SQLite
凭证绑定、manifest/Parquet 严格验证和查询合同。新增独立回归首次得到
`15 failed, 60 passed, 5 skipped`，随后补齐：

- 在创建任何 `.incomplete` 目录前检查受管理根、所有已有中间组件和最终 resolved
  containment；Raw 路径组件拒绝全部控制字符和不安全 Windows 名称。
- Canonical 发布前强制 BUILDING 状态以及 descriptor schema、code version、rule
  version 与 DatasetVersion 精确一致。
- 发布协调器支持携带 `EmptyResultInfo` 的合法空版本，并从实际 manifest 写入覆盖、
 证券数、行数和 SHA 凭证。
- 严格验证 manifest 必填字段和类型、规范 JSON、自身 SHA、COMPLETE、文件集合、
  完整 Arrow 类型/nullability、Parquet 行数总计和重复分区。
- Canonical reconcile 比较父版本、输入批次、分区方案、覆盖摘要和内容；现有目录
  不一致时只报冲突，不覆盖、不删除、不修补。
- Query 对 SQLite 与磁盘 manifest 执行全凭证精确比较，规范解析 `data_version`
  UUID4；合法空投影保持调用者字段顺序。
- 已发布目录出现 `failure.json`、重解析点或任何未声明文件时拒绝复用和查询。

## 验收结果

| 验证 | 结果 |
|---|---|
| 独立 S1-02/R1 回归 | `80 passed` |
| S1-02 原测试与独立回归 | `152 passed, 2 skipped` |
| Stage 1 数据域与状态文档 | `301 passed, 2 skipped` |
| 完整离线 pytest | `514 passed, 2 skipped, 3 deselected` |
| Ruff lint / format | 通过 |
| mypy | 72 个源文件通过 |
| 架构边界 / Markdown 链接 / Git whitespace | 通过 |
| 前端 typecheck / 8 项单测 / 生产构建 / 1 项 E2E | 通过 |

剩余 2 个 skip 属于原有环境相关用例，不影响 Warehouse 合同。Windows junction 已使用
普通临时目录真实创建并验证，Raw、Canonical、Query 的根和中间路径拒绝用例均通过。

## 外部访问与副作用

- 未访问网络、QMT、XTQuant 或备用数据源。
- 未读写 `E:\QuantKingV3Data`、`E:\QuantKing` 或 `F:\QuantKingV3Backups`。
- 数据测试只使用 pytest 临时目录和合成数据。
- 完整质量门仅在授权后读取既有离线工具链缓存；未下载依赖。
- 未实现 S1-03 数据源适配器、质量/跨源业务、EOD、Worker、CLI/API/Web 或真实数据发布。
