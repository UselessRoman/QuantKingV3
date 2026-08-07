# S1-07C0B 实施记录：受控执行、持久化与 DataCenter 读侧纵切

状态：**C0B 实现完成、等待独立主验收**。

## 任务身份与授权原文

- Stage：Stage 1 / S1-07C0B。
- 基提交：`7d6bd44eecd3817fcd40b84f37697c166845686a`。
- 前置：S1-07C0A-R1 已独立主验收，接受提交
  `5a48909355d7a023fd404780688d070106a2522f`，记录见
  [S1-07C0A-R1_ACCEPTANCE.md](S1-07C0A-R1_ACCEPTANCE.md)。

本任务的授权原文为：

> 授权实施 Stage 1 / S1-07C0B：受控执行、持久化和 DataCenter 读侧纵切。仅实施纯离线、仓库内 Fake-provider 受控执行、assessment seal 持久化和 DataCenter 深验读侧纵切；不允许真实供应商运行、REAL marker、真实密钥或外部数据访问。必须完成单一严格 `RuntimeAuthorizationEnvelope` 文件入口、全部预检后的一次性 claim、claim 后的固定 runner import/输出、独立 signer/trust policy、不可覆盖的成功/失败/崩溃证据、受管目录 v3 seal 深验持久化、DataCenter 每读重验与失败关闭。必须覆盖授权替换/过期/重放/并发、import/output 时序、Fake→REAL、失败夹带成功凭证、run 覆盖、手工 DTO/SQL 伪造、seal 清单/SHA/路径/reparse 攻击及 DataCenter seal 损坏。不得运行 REAL marker，不得访问网络、QMT/XTQuant/其他供应商或 SDK，不得访问正式数据根、旧项目或 F 盘；不得开始 C1、S1-07D、S1-08 或 Stage 2。完成时创建结果提交、执行完整质量门并保持工作树干净。

## 开始前审计与已封闭旁路

开始时 HEAD 与基提交一致，工作树干净；Git 仅报告了用户全局 ignore 文件的读取权限警告，未修改任何用户文件。

审计确认并处理的断点如下：

| 原断点 | C0B 收口方式 |
|---|---|
| CLI 只读取 `ExecutionAuthorization`，而运行时需要 `RuntimeAuthorizationEnvelope` | CLI 授权文件改为严格、规范的 `RuntimeAuthorizationEnvelope` 解析；唯一可执行入口 `fake-execute` 只接受该文件，不接受 source、dataset、case、bundle、输出目录或模块参数。 |
| `RuntimeAuthorizationEnvelope` 无 canonical file codec，调用方可组装 DTO | 新增严格 `from_bytes()` / `to_bytes()`、未知字段/非 canonical 字节拒绝、嵌套 contract/profile/identity 精确字段集校验，以及带私有 seal 的 `RuntimeAuthorizationFileLoader`；C0B runtime 不接收 DTO。旧 DTO 参数被显式拒绝。 |
| `RealValidationRuntime` 只有 readiness blocker | 保留其 fail-closed 行为，不启用任何 REAL runner；新增独立的 C0B Fake-only runtime，固定 import `quantking.data.validation.runners.fake`。 |
| v2-only sealed persistence/read verifier | 保留 v2 专用分支，新增显式 v3 seal 分支和 `SqliteV3SourceAssessmentPersistenceAdapter`；未知/未配置的 v3 reader 失败关闭。 |
| 正式 composition root 的 DataCenter UoW 没有 sealed reader | `build_eod_runtime()` 向 `DataSqliteUnitOfWorkFactory` 注入同一 sealed reader。生产默认没有 v3/REAL trust context，遇到此类 row 失败关闭。 |

不存在环境变量授权入口；C0B runtime 只从授权文件得到输出 scope、身份和冻结输入。

## 信任模型与授权/claim 顺序

严格 envelope 绑定：source、dataset、assessment/invocation/session/run ID、排序且唯一的
case IDs、reference bundle ID/SHA、source contract、promotion profile、全部八项版本、精确
output scope、有效期、plan SHA 与一次性 authorization ID/claim nonce。嵌套字段和 canonical
bytes 均被验证，执行前会将 envelope 再与受控 context 逐项比对。

实际顺序为：

```text
canonical authorization-file load + signature verification
  → output path / source / dataset / case / bundle / contract / profile / versions / runner readiness precheck
  → atomic one-time claim
  → fixed Fake runner module import
  → immutable v3 output (or terminal .incomplete failure record)
```

claim 使用原有原子 `O_EXCL` claim store，因而防并发和重放；claim 后任何失败、异常或进程中断路径均不删除 claim。成功 run ID、已有 incomplete 目录和不同内容同 ID 全部拒绝覆盖。

`ControlledV3RunnerTrustPolicy` 是生产 C0B 的显式 key-id allowlist：唯一内置 key 为
`quantking.fake-runner.v1`，独立 policy 永远将它推导为 `SYNTHETIC`。没有 REAL 私钥、默认 REAL
key、生产 REAL profile、信任根生成或自助 key 注册。REAL/BLOCKED 与 REAL/PRODUCTION_READY 只由
测试文件内的临时隔离 signer/policy 覆盖，不进入生产 allowlist、临时目录之外的 artifact 或文档结论。

runner 包本身没有供应商 SDK import；所有未来 SDK 仍只能由 `data/adapters` 的固定适配器延迟加载。

## 证据、持久化与读侧

- 成功 Fake run 写不可变 v3 evidence，由独立 verifier 推导为 `SYNTHETIC`。
- 受控失败写 v3 failure evidence，缺少 Raw/Canonical/Quality/Difference 成功凭证。
- import/runner 崩溃在 claim 后留下带 identity 与 plan SHA 的不可回收 `.incomplete/failure.json`；不能被同一 run ID 修复为成功。
- v3 persistence 只接收受管 assessment 目录，复制完整声明文件树至 zip seal，再从 seal 解压、深验 v3 summary、所有 run manifest、trust context、attestation digest 和完整文件清单，之后才通过 0012 的连接局部一次性 SQLite gate 写入。
- `SealedAssessmentReadVerifier` 按 `_assessment` / `_assessment-v3` 明确分派；每次 repository/DataCenter read 都重新校验 seal SHA、路径、reparse、zip inventory、summary 和 DB-row lineage。seal 缺失、篡改、额外/缺失文件、context 不匹配或 verifier 未配置均失败关闭。
- `ControlledFakeAssessmentService` 将一个完成的 C0B Fake assessment 目录依次 finalize 为
  v3 summary、交给唯一 v3 seal persistence adapter，并由注入相同 verifier 的 UoW 再次读取；
  定向测试覆盖这条 Fake run → seal → SQLite → sealed read 纵切，最终仍为
  `SYNTHETIC/BLOCKED_EXTERNAL_VALIDATION`。

## Migration

本任务**没有新增 Alembic revision**，`20260729_0012` 保持不变。理由是没有新增机器可查询字段、状态或数据库语义：v3 的完整 contract/trust context/run manifests 都由 sealed artifact 绑定，已有表的 evidence/seal SHA、attestation 摘要与 0012 generic write gate 已足以保存它们。新库升级、历史 0012、唯一 head、历史隔离、触发器回滚和恢复路径继续由既有 Stage 1 migration regressions 覆盖；C0B 新增 v3 persistence 测试在新库上验证 0012 gate 能保存和拒绝篡改读侧。

## 攻击回归与质量门

新增的 C0B 定向覆盖包括：

- unknown field、purpose/case/bundle/profile/contract 替换、canonical file、并发 claim、重放和 run ID 覆盖；
- 可断言的 `PRECHECK → CLAIM → IMPORT → OUTPUT` 时序、claim 后 crash/失败、无成功凭证；
- Fake key 不可包装为 REAL、未知/不配置 v3 signer 失败关闭；
- 受管 v3 seal 的 REAL/BLOCKED 和测试隔离 REAL/PRODUCTION_READY 推导；
- 手工/数据库状态不可信、seal SHA 损坏、reader/verifier context 不一致与 DataCenter fail-closed。

已实际执行的完整离线质量门为 `scripts/quality.ps1`，退出码为 0：

- 后端：`1265 passed, 2 skipped, 3 deselected, 4 warnings`；warnings 来自既有 Qlib
  NumPy timedelta 弃用提示和 Starlette TestClient 弃用提示。
- `uv lock --check --offline`、离线 frozen sync、Ruff、format、mypy（133 个 source files）、
  架构边界、Markdown links 与 `git diff --check` 均通过。
- 前端：类型检查通过，4 个测试文件/17 项测试通过，生产 build 通过，Playwright 1 项通过。
- C0B 定向 runtime/persistence 回归为 18 项通过；C0A 的 21 项攻击回归通过；全 validation
  （含 C0B v3 persistence）定向复跑为 224 项通过。v2 persistence、DataCenter 和 migration
  的交叉回归也已在完整门与定向命令中通过。

结果提交 ID 只在完成最终 `git diff --check`、提交并确认工作树干净后回填到本实施交接，不在本文件
预填未产生的提交 ID。

## 外部副作用与遗留风险

本任务仅在 pytest 临时目录、仓库源文件和 Git 元数据内读写。REAL marker 数为 0；未访问网络、QMT、XTQuant、BaoStock、AKShare、Tushare、任何供应商 SDK、`E:\QuantKingV3Data`、`E:\QuantKing` 或 `F:\QuantKingV3Backups`。

未开始：C1、S1-07D、S1-08 和 Stage 2。C0B 也不改变任何 source×dataset 的生产可用结论；所有生产 source matrix 单元仍保持既有 blocked 状态。
