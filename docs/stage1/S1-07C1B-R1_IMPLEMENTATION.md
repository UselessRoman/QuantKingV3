# S1-07C1B-R1 仓库外授权材料路径边界修复

状态：**“C1B-R1 纯离线仓库外路径边界修复实现完成，等待独立主验收；真实运行数为 0。”**

基提交：`72b10fd36a821da1b4c4e1c8d561d37e5357d2f7`。

## 范围

- Stage：Stage 1 / S1-07C1B-R1。
- 固定单元仍是 `XTQUANT × trading_status × st_warning_603377_sh`。
- 仅修复 C1B 初版独立主验收发现的仓库内授权材料路径边界；不修改 C0A、C0B 或
  C1A-R3 已验收合同、工件、签名或 failure-only runner。
- C1B 初版独立主验收未通过；本实现记录不是主验收证据。

## 仓库外路径策略

`c1b_operation` 从自身已解析的源文件向上定位 `.git`，不使用当前工作目录。根定位和
全部候选路径都经过绝对路径、逐段 symlink/reparse point 与 `resolve` 检查；Windows 比较使用
大小写无关的规范化路径和组件边界，故仓库根、任意后代和 `.gitignore` 忽略目录均不能绕过。

该策略统一覆盖：

- `plan` 和完整 `authorization` 的写入前置检查、排他创建和读取；仓库内目标在 `os.open` 或
  `read_bytes` 前拒绝。
- CLI `prepare` 在构造计划或读取 signer 材料前检查 plan 目标；CLI `issue` 在读取合规 plan 后、
  签发 authorization 前检查 authorization 目标。
- `review` 与 `execute` 通过同一 authorization 读取路径拒绝仓库内文件，因而在 claim、runner
  import 或 output 创建之前失败关闭。
- authorization 与 runner 私钥在 `prepare` 中、canonical plan/auth 重载中，以及运行时
  key-configuration 重验中都必须位于仓库外；拒绝消息不含路径或密钥字节。

仓库外的受控临时目录仍可正常完成 `prepare → plan → issue → review`。该修复不提供默认
输出路径、默认密钥、环境变量降级或自动执行路径。

## 回归与边界

定向测试覆盖仓库内 plan 写入/读取、authorization 写入/review/execute、CLI 签发前置检查、两类
仓库内私钥、Windows 大小写路径与 `.gitignore` 忽略路径；全部拒绝均断言无 plan/auth、claim 或
output 副作用。测试仅使用仓库外 `tmp_path` 的临时 Ed25519 key、SQLite、bundle 和 dist-info。

本任务没有 migration，Alembic head 仍为 `20260729_0012`。没有真实 invocation、REAL marker、
XTQuant SDK/QMT/网络、正式数据根、旧项目或 F 盘访问；新增和实际执行的 REAL marker 均为 0。

真实 XTQuant 运行仍须获得新的、逐 source×dataset×case×invocation 的明确授权。C2、S1-07D、
S1-08 与 Stage 2 未开始、未获授权。
