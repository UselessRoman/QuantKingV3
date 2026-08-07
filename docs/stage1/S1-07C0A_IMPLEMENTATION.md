# S1-07C0A 实现证据：版本化运行证据合同

状态：S1-07C0A-R1 已于 2026-08-07 完成独立主验收，结果提交为
`5a48909355d7a023fd404780688d070106a2522f`，证据见
[S1-07C0A-R1 主验收证据](S1-07C0A-R1_ACCEPTANCE.md)。本记录不授予 C0B、S1-07C
真实运行、S1-07D、S1-08 或 Stage 2 的实施权限，也不表示任何来源具有 REAL 或
`PRODUCTION_READY` 能力。

## 任务范围

- 任务：Stage 1 / S1-07C0A。
- 基提交：`0edda4880f1a7839babb71e572543163e9bafb08`。
- 授权：仅仓库内、纯离线证据合同演进；不访问网络、QMT/XTQuant、
  `E:\QuantKingV3Data`、`E:\QuantKing` 或 F 盘；不安装或导入供应商 SDK；不启用
  REAL signer/runner、真实 marker、持久化、DataCenter、API 或 Web。
- 数据库迁移：无。Alembic head 保持 `20260729_0012`。

## 版本与兼容策略

| 组件 | 既有合同 | C0A 新合同 | 策略 |
|---|---|---|---|
| request | `source-validation-request.v1` | `source-validation-request.v2` | 既有 v1 原样验证；v3 只接受 identity、排序 case 集及受控上下文完整绑定。 |
| run envelope | `source-validation-envelope.v2` | `source-validation-envelope.v3` | 按 manifest schema 分派，v2 路径未迁移、未重签。 |
| result attestation | 无 schema discriminator 的 S1-07A attestation | `source-validation-result-attestation.v2` | v3 独立解析、签名与文件名；v2 Fake signer/字节锚保持有效。 |
| assessment summary | `source-assessment-manifest.v2` | `source-assessment-manifest.v3` | v3 summary 列出每个 run 的完整身份和 frozen context；混用 v2/v3 拒绝。 |

S1-07A 固定样本 v2 与 S1-07B 样本 v4、authority bundle v3 均未修改。v3 默认没有
trust context 或 REAL signer 时失败关闭；随包提供的 `quantking.fake-runner.v1` 仍永久为
`SYNTHETIC`。

## v3 绑定与推导规则

v3 request 在执行前绑定 assessment、authorization、invocation、session、run 身份，以及：

- 排序、唯一、非空的 `selected_case_ids` 与完整 case 描述；
- fixed sample version/SHA；
- reference bundle ID/manifest SHA；
- source×dataset semantic/unit/coverage contract 版本与 SHA；
- dataset promotion profile/config 版本与 SHA、所需 case 集、稳定运行数和受控最大差异数；
- adapter/package/code/schema/rule/threshold/sample/config 版本。

`request_sha256` 绑定每次 attempt 的全部身份；稳定 `run_fingerprint` 绑定相同的非身份合同
输入，使独立 authorization/invocation/session/run 仍可做三次稳定性比较。envelope、v3
attestation 和 assessment summary 逐项重绑这些值，任何单层改动（即使重新计算普通 SHA）均不
足以通过深验证。

v3 attempt 明确为 `SUCCEEDED` 或 `FAILED`：

- `FAILED` 必有规范化、脱敏的失败分类（包括 `CAPABILITY_UNSUPPORTED`、权限、限流、服务
  不可用），只允许 request、attestation 和 failure evidence；禁止任何 Raw、Canonical、
  Quality、Difference、semantic 或 schema/unit 成功凭证，五项 gate 固定由 verifier 归零。
- `SUCCEEDED` 必有完整 Raw、Canonical、Quality、Difference、semantic 和 schema/unit
  凭证，并全部和 trusted attestation 的 inventory/SHA/identity/context 相互绑定。业务成功
  不等于 gate 全绿。
- semantic、schema/unit、coverage、quality、difference gate 分别由语义违规列表、单位实测
  映射、Canonical 行覆盖、完整 QualityRun 与严格 DifferenceReport 重新计算。writer、envelope
  和 attestation 均不接受 gate 布尔值。
- C0A 的独立 key-id trust policy 只承认 bundled Fake key，且永久推导为 `SYNTHETIC`；本阶段
  拒绝一切 REAL profile。未来由另行授权任务提供的 REAL 身份、全部成功证据、完整且独立的
  稳定运行、且 selected case 精确满足 promotion profile 所有 required cases 时，才可推导
  `PRODUCTION_READY`。单个 `AUTHORITY_APPROVED` case 仅可作为执行前置条件，绝不自动提升
  source×dataset。

## C0A 攻击回归

新增 `tests/data/validation/test_s1_07c0a_contracts.py`，覆盖：

- v2 固定 release/SHA 与 v2/v3 schema 分派、默认无 v3 trust context 的失败关闭；
- case 缩减、增补、重复和重排；
- reference bundle ID/SHA、source×dataset contract/profile 替换；
- 替换/重新自签 signer；
- 可信失败保持 BLOCKED、failure 冒充 success、failure 夹带成功 artifact；
- 调用方伪造 gates 与篡改 summary 后重算普通 SHA；
- 单 case 伪装 dataset 级生产就绪；
- 不同 run 的 case/bundle/profile/fingerprint 混用。

实施对话的离线验证记录如下；它们不构成独立主验收：

- C0A 初始定向攻击回归：`16 passed`；R1 的补充攻击回归见
  [S1-07C0A-R1 实现证据](S1-07C0A-R1_IMPLEMENTATION.md)。
- 既有 S1-07A/S1-07B validation 回归：`186 passed`；当时整个 validation 目录（含 C0A）为
  `202 passed`。S1-07A persistence/finalized-seal
  集成回归：`53 passed`。
- `scripts/quality.ps1`：退出码 0；后端 `1242 passed, 2 skipped, 3 deselected`，Ruff、
  format、mypy、架构边界、Markdown 链接和 `git diff --check` 均通过；前端 typecheck、
  17 项单测、生产构建和 Playwright 1 项均通过。
- REAL marker：0。完整门禁只创建临时测试 SQLite/本机 loopback WebServer 与前端构建目录；
  未读取或写入任何被授权边界外的数据根、旧项目或备份根。

## 明确留给 C0B 及以后

本任务没有实现 runtime authorization 文件加载、REAL signer allowlist、provider runner、
授权 claim 流程、assessment seal 持久化、DataCenter 深验注入、数据库迁移或任何真实执行。
后续任务仍须获得新的明确授权；当前 REAL marker 执行数为 0。
