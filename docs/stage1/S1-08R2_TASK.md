# S1-08R2 任务卡：发布引擎离线收口

## 目标与前置

所属 Stage：Stage 1。状态：已验收（2026-08-17 独立只读验收通过，见
[S1-08R2_ACCEPTANCE.md](S1-08R2_ACCEPTANCE.md)）。本任务只解决正式发布前的存储、恢复、
参数化和读侧可靠性，不执行真实网络或正式 Job。

最终验收依赖已提交并独立验收的 S1-07K4；纯离线代码可以提前开发，但不得用合成证据替代
K4 的真实来源与迁移结果。

## R2A：存储、幂等和恢复

- 数据 UOW 对可安全重放的短事务实现 `SQLITE_BUSY/SQLITE_LOCKED` 有界退避和超限失败。
- 合法空版本的内容摘要和重复发布必须幂等收敛。
- 正式拓扑实行可证明的单发布者约束或进程间协调；同一 version ID 不得竞争发布。
- legacy `.incomplete` 和普通 Job 在硬中断后有明确判定、收束和新任务重跑合同。
- 增量 base coverage 表达真实连续段和内部缺口，不能用全局 `(min,max)` 冒充连续覆盖。
- 失败批次保留脱敏原因链、质量影响计数和有界行级证据，不污染 Warehouse。

## R2B：manifest 驱动的范围和预算

- 证券集合、日期范围、分块、A/B/C planned/maximum budget、血缘数量上限均由冻结 manifest
  和安全总上限派生。
- 删除首次两证券、固定日期、固定批次/质量/差异数量和过时 code version 常量。
- manifest 绑定证券集合规范排序/SHA、交易日历版本/SHA、逐证券 coverage、缺口/豁免、来源与
  规则版本、legacy import manifest SHA 和发布意图。
- 预算 guard 是正式 composition root 必需项，不能通过漏注入绕过。

## R2C：verifier、DataCenter 与 Web

- 只读 verifier 由显式 version ID 启动，拒绝 `latest`、路径和 SQL；连接只读且不得改变
  data root 文件集合、mtime 或 SHA。
- 复验 Job/Version、manifest/`COMPLETE`、文件 SHA、Raw/import、Quality、Difference、
  Resolution、逐证券完整性、两源共识和预算守恒。
- DataCenter 按显式版本展示 BaoStock A、腾讯 B、 新浪 C、XTQUANT_LEGACY、逐证券 coverage、
  freshness、质量、差异、迁移缺口和隔离计数；不泄露绝对路径或供应商载荷。
- 使用超过两证券和首次固定批次数量的合成 manifest 完成 EOD→verifier→DataCenter E2E。

## 完成条件

- 全部离线质量门和独立验收通过，形成单独 R2 提交。
- dry-run 能从 K4 manifest 生成 S1-08R3 所需的全部冻结输入和有界调用计划。
- 不读取 `E:\QuantKing`，不写 `E:\QuantKingV3Data`，不运行真实 provider，不开始 Stage 2。
