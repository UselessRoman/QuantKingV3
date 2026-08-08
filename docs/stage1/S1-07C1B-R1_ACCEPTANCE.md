# S1-07C1B-R1 主验收证据（纯离线运行就绪化）

- 验收结论：**通过**。
- 验收范围：固定单元 `XTQUANT × trading_status × st_warning_603377_sh` 的纯离线运行
  就绪化，以及 plan、完整 authorization 与 signer 私钥的仓库外路径边界。
- 基提交：`72b10fd36a821da1b4c4e1c8d561d37e5357d2f7`。
- 结果提交：`85bab3fb25840b73958c0c5fdfe2e0301605353d`。
- 主验收性质：**“S1-07C1B-R1 已完成独立主验收（纯离线运行就绪化）；真实运行数为 0。”**

## 初版拒绝与 R1 修复

C1B 初版独立主验收未通过：结果提交为 `72b10fd36a821da1b4c4e1c8d561d37e5357d2f7`。其
路径策略只检查绝对路径、reparse point 与排他创建，未拒绝 Git 仓库根及后代路径。因此
plan、完整签名 authorization 和 authorization/runner 私钥可位于仓库内。

R1 在提交 `85bab3fb25840b73958c0c5fdfe2e0301605353d` 建立以源码 `.git` 根为锚点、大小写无关且
resolve/reparse 复验的仓库外策略。它在 plan/auth 的读写、`prepare`、`issue`、`review`、
`execute` 和私钥重验处失败关闭，并保证拒绝发生在文件 I/O、签发、claim、runner import 或
output 创建之前。

## 独立验收证据

- 提交链正确：`72b10fd… → 85bab3f…`。
- 独立路径探针确认仓库根、ignored 子目录和 Windows 大小写变体均被拒绝；
  `QuantKingV3Data` 同前缀目录及系统临时目录未被误判。
- C1B-R1 与状态文档定向：`68 passed`。
- S1-07 validation 历史回归：`268 passed`。
- 完整 `scripts/quality.ps1`：退出码 `0`；后端
  `1314 passed, 2 skipped, 3 deselected, 4 warnings`；Ruff、format、mypy、架构边界、
  Markdown 链接、Git whitespace、前端 17 项单测、生产构建与 Playwright 1 项均通过。
- 首次质量门仅被沙箱工具链 cache 权限阻挡；取得只读 cache 权限后从头完整重跑并通过。
- Alembic 唯一 head：`20260729_0012`；未新增 migration。
- `git diff --check` 通过；验收时 HEAD 未变且工作树 clean。

## 授权边界

本验收未执行 REAL marker、真实 invocation、XTQuant/QMT、网络或外部数据访问；各项实际计数均为
0。它不证明 XTQuant 真实数据能力、实时/历史 ST 语义或任何 `READY`/生产 capability，也不授权
真实 invocation、C2、S1-07D、S1-08 或 Stage 2。真实运行仍须取得新的、逐
source×dataset×case×invocation 的明确授权。
