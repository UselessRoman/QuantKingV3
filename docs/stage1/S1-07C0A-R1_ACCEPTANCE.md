# S1-07C0A-R1 独立主验收证据

状态：**已主验收（纯离线证据合同演进）**。

## 验收范围与结果

- 任务：Stage 1 / S1-07C0A-R1。
- 接受结果提交：`5a48909355d7a023fd404780688d070106a2522f`。
- 验收结论：通过，无需 R2。
- 范围严格限于 S1-07C0A 的纯离线版本化证据合同；不证明任何 REAL 来源能力。

## 独立复核证据

- C0A-R1 攻击回归：`21 passed`。
- Fake→REAL 包装仍推导为 `SYNTHETIC/BLOCKED`。
- 非零差异与错误来源侧均关闭 Difference gate。
- envelope、result attestation 与 failure evidence 三层的 failure 未知字段均被拒绝。
- 7 种 verified DTO `replace()` 篡改均被拒绝。
- v2 专用入口拒绝 v3；通用入口返回明确的 v3 类型。
- 既有 S1-07A/S1-07B validation：`186 passed`；全部 validation：`207 passed`。
- `scripts/quality.ps1` 退出码 0：后端 `1247 passed, 2 skipped, 3 deselected, 4 warnings`；
  Ruff、format、mypy、架构边界、Markdown、前端 17 项、生产构建与 Playwright 1 项均通过。
- Alembic 唯一 head：`20260729_0012`；最终 HEAD 正确、工作树干净、`git diff --check` 通过。

## 未授权边界

本验收不授权或自动开始 C0B、真实 signer/runner、持久化、真实运行、S1-07D、S1-08 或 Stage 2。
REAL 运行数仍为 0；任何后续任务均须由用户按任务明确授权。
