# S1-07C0A-R1 实现证据：v3 信任合同收紧

状态：已于 2026-08-07 完成独立主验收，结果提交为
`5a48909355d7a023fd404780688d070106a2522f`，证据见
[S1-07C0A-R1 主验收证据](S1-07C0A-R1_ACCEPTANCE.md)。本记录不授予 C0B、REAL signer/runner、
授权 claim、持久化、CLI、DataCenter、API、Web 或任何真实来源访问权限。

## 任务与边界

- 任务：Stage 1 / S1-07C0A-R1。
- R1 基提交：`07fa7d125c8143297076739fe4a432c607a6ad5c`。
- 原始 C0A 基提交：`0edda4880f1a7839babb71e572543163e9bafb08`；未回退或改写历史提交。
- 授权：只修复独立主验收指出的 v3 信任合同和离线攻击回归；不访问网络、QMT/XTQuant、
  `E:\QuantKingV3Data`、`E:\QuantKing` 或 F 盘，不安装/导入供应商 SDK。
- 数据库迁移：无；Alembic head 保持 `20260729_0012`。

## R1 修复

| 主验收问题 | R1 约束 |
|---|---|
| Fake signer 自报 REAL | `C0ARunnerTrustPolicy` 独立地将唯一受控 key-id `quantking.fake-runner.v1` 固定映射为 `SYNTHETIC`；签名 verifier 不再声明或决定 evidence kind，C0A 拒绝任何其他 key-id/REAL profile。 |
| Difference gate 只看元数据 | promotion profile 明确绑定 `maximum_total_differences`（默认 0）；gate 复验差异数，并要求 envelope source 位于正确 primary/fallback 侧，且该侧 Canonical content SHA 与 QualityRun ID 同时匹配。 |
| v3 verified DTO 可伪造 | run 与 assessment 都要求私有 issuer seal、模块私有 issuing context，以及由模块私有 issuer key 对全部深验字段生成的 binding；`dataclasses.replace()` 篡改或清空 issuer binding、evidence kind、gate、summary 或字段都会失败。 |
| failure 含未知嵌套字段 | outer envelope、result attestation 和 failure evidence 都要求精确的 `category`/`retryable` 字段集与规范字节；未知字段在深验前拒绝。 |
| v2/v3 dispatch 使用 `cast` | `ValidationEvidenceVerifier.verify`、`verify_run`、`verify_assessment` 显式返回 v2/v3 联合类型；v2 调用方使用拒绝 v3 的 `*_v2` 专用入口，因此没有不相容的伪装返回。 |

v2 request 仍是 `source-validation-request.v1` 并原样验证；v2 envelope/assessment seal、Fake signer
和失败产物没有迁移、重签或修改。v3 result attestation、envelope 和 assessment 的现有 schema
名称不变，R1 仅收紧其深验证和受控 profile 语义。

## 攻击回归与检查

- `tests/data/validation/test_s1_07c0a_contracts.py`：`21 passed`，新增 Fake→REAL 包装器、三次
  稳定运行、超过差异阈值、错误来源侧、failure 未知字段，以及 verified run/assessment
  `replace()` 篡改回归。
- 全部 validation：`207 passed`；其中既有 S1-07A/S1-07B 为 `186 passed`。
- `scripts/quality.ps1`：退出码 0；后端 `1247 passed, 2 skipped, 3 deselected`，Ruff、format、
  mypy（`130 source files`）、架构边界、Markdown 链接与 `git diff --check` 均通过；前端
  typecheck、17 项单测、生产构建与 Playwright 1 项均通过。

## 副作用与遗留

- REAL marker：0；没有 REAL 运行身份或 `PRODUCTION_READY` 结论。
- 外部读写：无网络、QMT/XTQuant、供应商 SDK、正式数据根、旧项目或 F 盘访问；仅仓库、临时测试
  路径和既有本机离线工具链用于检查。
- 留给后续独立授权：C0B 的受控 signer/runner、持久化和读侧纵切，以及任何真实执行。
