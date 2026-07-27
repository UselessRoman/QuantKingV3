# Stage 1 / S1-05 主验收证据

## 结论与范围

- 验收日期：2026-07-27。
- 结论：S1-05（EOD、Worker、CLI 与 API）完成主验收。
- 主验收提交：`e580d6963e01b1301dc26f29ed57a0b5b4770a0f`。
- Alembic 唯一 head：`20260722_0009`；S1-05 未新增迁移。
- 本结论确认离线、Fake Gateway 和临时数据根范围内的 EOD 请求、执行、恢复、证据及入口
  合同；不构成真实 XTQuant、QMT 或正式数据根运行能力证明。
- S1-06 已获离线实施授权但尚未开始；S1-07～S1-08 未开始、未获授权。Stage 1 尚未完成。

## 主验收确认

主验收没有直接采信开发交接报告，而是重新检查实现、运行定向攻击回归和完整质量门。最终确认：

- CLI 和 Web API 共用固定 `data.eod.v1` Command Service；POST 只入队，不同步执行 Gateway
  或 Worker。
- request/config/version 三类持久 manifest、规范 SHA、显式 base version 和 frozen command
  共同约束 Worker 与终态 evidence。
- Worker 只在显式入口执行，使用持久 heartbeat 租约、`row_version` 条件更新和全 handler
  生命周期续租；普通 Web/CLI runtime 不恢复任务。
- 每个数据集顺序复用 ingestion、`QualityRunner` 和 `CanonicalPublishCoordinator`；
  `PUBLISHED_NEW` 绑定冻结 base 与本次 batch，`REUSED_UNCHANGED` 复用 base 的真实
  parent、input batches 和质量/内容凭证。
- 增量抓取按实际 `FetchChunk` 的证券和日期范围替换基表切片，未抓取范围与历史窗口保持
  不变；新版本是 base 的直接子版本。
- success、failure 和 checkpoint 证据严格绑定 JobRun、batch、DatasetVersion、
  PASSED QualityRun、Warehouse manifest/COMPLETE/content SHA，重新签名不能掩盖业务篡改。
- provisional success 不具权威终态；最终提交故障会保留 forensic success、写入不可覆盖
  failure evidence，并由显式 stale recovery 幂等复用唯一已发布结果。
- 重复恢复和新幂等键重试不会重复调用 Gateway，也不会重复创建 batch、version 或
  QualityRun；新鲜租约或仍在运行的任务不会被错误复用。
- import、`--help` 和 API 入队不会加载 XTQuant SDK；生产能力矩阵仍只允许已验证的
  `exchange_calendar`，其余五类在 Gateway 前安全失败。

## 独立验收结果

| 验证 | 结果 |
|---|---|
| S1-05 定向回归 | `164 passed` |
| success/failure 重新签名与血缘攻击 | 已拒绝 |
| root/child/mixed no-change 与发布后恢复 | 通过 |
| 完整离线 pytest | `918 passed, 2 skipped, 3 deselected` |
| Ruff lint / format | 通过 |
| mypy | 109 个源文件通过 |
| 架构边界 / Markdown 链接 / Git whitespace | 通过 |
| 前端 typecheck / 8 项单测 / 生产构建 / 1 项 E2E | 通过 |
| Alembic 唯一 head | `20260722_0009` |

完整 `scripts/quality.ps1` 由主验收 Agent 独立运行并以退出码 0 结束。

## 外部访问与后续边界

- 验收未访问网络、QMT、XTQuant 或任何备用供应商。
- 未读写 `E:\QuantKingV3Data`、`E:\QuantKing` 或 `F:\QuantKingV3Backups`。
- 全部数据测试使用 pytest 临时目录、临时 SQLite/Warehouse 和合成数据。
- S1-03 的五类真实 XTQuant 能力证据和真实备用源评估仍须在 S1-07 或专项验收中逐项授权。
- S1-06 只允许离线实现数据中心 Web 和只读查询 API，并复用现有 EOD 入队/状态/取消接口；
  不得运行真实 Worker、引入调度器、自动切源、自动发布或任何下单/撤单能力。
