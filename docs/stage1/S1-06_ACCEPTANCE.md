# Stage 1 / S1-06 主验收证据

## 结论与范围

- 验收日期：2026-07-28。
- 结论：S1-06（数据中心 Web、只读查询 API 与关键 E2E）完成主验收。
- 主验收提交：`413805074b3a82c2d2a090d2b73c854a238e2e19`。
- 父提交：`12b576b6a6a702364f135ba1cdd59a45d8f8d754`。
- Alembic 唯一 head：`20260722_0009`；S1-06 未新增迁移。
- 本结论确认离线、临时 SQLite/Warehouse、合成 seed 和 loopback 范围内的数据中心查询、
  显式版本预览、DifferenceReport、SourceOverride、EOD Job 与 Web 交互合同。
- 本结论不构成真实 XTQuant、QMT、备用供应商、正式数据根或旧项目数据访问能力证明。
- S1-07～S1-08 未开始、未获授权；Stage 1 尚未完成。

## 主验收确认

主验收独立复查实现、执行攻击回归和完整质量门，最终确认：

- 数据中心通过 framework-neutral 查询服务和窄 port 组合 SQLite 元数据仓储、
  CanonicalQueryAdapter、Warehouse verifier、DifferenceReportVerifier 与既有 Job/EOD 服务；
  FastAPI 路由不读取 SQLite、Parquet、artifact 或供应商 SDK。
- 所有列表使用绑定资源、规范过滤和排序版本的稳定 Cursor；显式预览只接受完整
  `PUBLISHED version_id`，并在 DuckDB 查询前复验 manifest、`COMPLETE`、文件集合和 SHA。
- DifferenceReport 详情、行差异和 Override 展示前均重新交叉验证 SQLite、artifact、
  双方 PUBLISHED version、PASSED QualityRun 和 Warehouse 凭证。
- Override 状态关系执行双向验证；伪造的 `APPROVED + APPLIED` application 图稳定拒绝并由
  API 映射为脱敏 `409 data_lineage_invalid`，合法 `APPROVED + PUBLISHED` 恢复窗口保留。
- 总览失败数由 SQL 聚合产生，范围与统一失败分页完全一致；超过列表上限时没有静默截断。
- 来源页分别展示权威 matrix 和最新持久 assessment，没有证据时不伪造 `REAL`，
  `SYNTHETIC` 不会提升为生产可用或触发自动切源。
- React 页面提供过滤、独立详情分页、显式版本血缘与规范化 EOD 幂等请求；真实 E2E 使用
  临时 SQLite、迁移、Warehouse、DuckDB、质量、DifferenceReport 和 Override 查询链，
  不注入内存数据中心。

## 独立验收结果

| 验证 | 结果 |
|---|---|
| Override 矛盾状态攻击 | `OVERRIDE_ATTACK=REJECTED` |
| 150 条失败统计 | `SUMMARY_FAILURES=150` |
| S1-06-R2 定向验收 | `38 passed` |
| 完整离线 pytest | `958 passed, 2 skipped, 3 deselected` |
| Ruff lint / format / mypy | 通过 |
| 架构边界 / Markdown 链接 / Git whitespace | 通过 |
| 前端 Vitest | `17 passed` |
| TypeScript / 生产构建 | 通过 |
| 真实应用查询链 Playwright | `1 passed` |
| Alembic 唯一 head | `20260722_0009` |

完整 `scripts/quality.ps1` 由主验收 Agent 独立运行并以退出码 0 结束。

## 外部访问与后续边界

- 验收未访问网络、QMT、XTQuant 或 BaoStock、AKShare、Tushare。
- 未读写 `E:\QuantKingV3Data`、`E:\QuantKing` 或 `F:\QuantKingV3Backups`。
- 全部数据测试使用临时 SQLite/Warehouse、Fake Gateway、合成数据和 loopback Web。
- 未启动真实 EOD Worker，未建设调度器、自动切源、自动发布、交易、下单或撤单能力。
- S1-03 的五类真实 XTQuant 能力缺口和备用源外部验证阻断仍然存在。
- S1-07～S1-08 未开始、未获授权；开始任何真实样本、旧项目读取或正式数据构建前必须取得
  单独授权。
