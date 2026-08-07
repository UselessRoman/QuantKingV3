# S1-07C1A-R3 主验收证据（纯离线）

- 验收结论：**通过**。
- 验收范围：固定单元 `XTQUANT × trading_status × st_warning_603377_sh` 的 REAL
  authorization、签名、claim、固定 runner、evidence、seal、SQLite persistence 与
  DataCenter 读侧控制链合同。
- 结果提交：`564523eb883c15b6b52f0815f22a70b10fced5cf`。
- 主验收性质：仅为纯离线控制链合同。它不证明 XTQuant 真实数据能力、实时/历史 ST 语义、
  网络/QMT 可用性或任何 `REAL/READY` 结果，也不启动 C1、C1B、C2、S1-07D、S1-08 或
  Stage 2。

## 验收证据

- C1A 定向回归：`20 passed`。
- S1-07 validation 历史回归：`249 passed`。
- 完整 `scripts/quality.ps1`：退出码 `0`；后端
  `1292 passed, 2 skipped, 3 deselected, 4 warnings`；Ruff、格式、mypy、架构边界、
  Markdown 链接、前端 17 单测、生产构建与 Playwright 1 项均通过。
- Alembic 唯一 head：`20260729_0012`；未新增 migration。
- `git diff --check` 通过，验收时工作树 clean。

## 已复验的失败关闭

下列工件攻击均被正式 reader 拒绝：

1. 内层或外层 authorization Ed25519 签名无效。
2. 有效签名但 `claimed_at` 晚于执行结束。
3. 受信 signer 重新签发的 envelope identity 漂移。
4. 受信 signer 重新签发的 envelope bundle、contract 或 profile 漂移。
5. `REAL/SUCCEEDED` 注入。

上述证据只说明控制链拒绝未绑定或不合规的离线工件；它不构成供应商能力或真实运行证明。

## 授权边界

验收未执行 `real_xtquant`/`real_external` marker，未访问外部网络、XTQuant/QMT、正式数据
目录、旧项目或 F 盘。真实 XTQuant 单元运行、任何 C1B/C2 工作及其后续阶段仍须另行按任务
取得明确授权。
