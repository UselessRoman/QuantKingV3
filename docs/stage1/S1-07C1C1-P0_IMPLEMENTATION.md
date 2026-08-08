# S1-07C1C1-P0 正式 XTData observation 操作入口与逐次授权交接

状态：**“S1-07C1C1-P0 已完成独立主验收（纯离线正式 file-only 操作入口与逐次授权交接）；C1C1 真实运行数为 0，仍未授权、未开始。”**

- Stage：Stage 1 / S1-07C1C1-P0。
- 基提交：`17354fa160a8f97e4721a2c620709ec12b43e401`。
- 实现已由 [P0 独立主验收记录](S1-07C1C1-P0_ACCEPTANCE.md) 接受；该记录及本文件均不构成真实运行授权。

## 范围

本任务将 C1B-R1 与 C1C0-R3 已验收的签名控制链封装为唯一正式的 C1C0 文件操作入口：

```text
C1B canonical authorization file
  -> C1C0 canonical plan file
  -> C1C0 canonical authorization file
  -> deterministic review / authorization SHA
  -> user explicit per-invocation authorization
  -> execute
```

新增 `scripts/run_s1_07c1c0.py` 与
`src/quantking/data/validation/c1c0_cli.py`。固定子命令是 `prepare`、`issue`、`review` 与
`execute`；CLI 不提供 DTO、环境变量或调用方字段来构造或覆写授权。错误输出只有稳定
`error_code`。

`prepare` 使用 C1B 的正式 canonical loader，并显式执行无副作用 extension precheck 后才调用
C1C0 plan builder；它不 claim、导入 SDK、调用 XTData、写输出或写数据库。`issue` 只从 C1C0
canonical plan 文件读取输入，且在签名前预检仓库外目标。`execute` 继续只接受 canonical C1C0
authorization 文件和精确 acknowledgement。

## 文件与 review 契约

`c1c0_operation` 新增公开 `write_plan`、`load_plan`、`load_authorization` 与
`review_authorization`。plan/auth 文件使用逐字节 canonical JSON、精确字段集、精确类型、内嵌 SHA
和签名复验；仓库根、仓库内 ignored 目录、symlink/reparse point、已存在目标及非规范字节均失败关闭。

`review_authorization` 从已签名 authorization 推导稳定 JSON，包含 task/stage、authorization 和
C1B SHA、plan SHA、五个 identity、source/dataset/case、固定 request、package/runner/ref-bundle/
metadata 绑定、路径根、有效期、key ID、精确 acknowledgement、允许结论及
`real_execution_authorized=false`。它不泄露私钥、claim nonce、签名、SDK response 或供应商 payload。

## 纯离线验证

定向测试仅使用 `tmp_path` 下的 Fake SDK、临时 Ed25519 密钥、SQLite metadata 和 authority bundle，覆盖：

- CLI `prepare -> issue -> review -> execute`、claim 后才 import、唯一固定调用与阻断结论；
- canonical plan/auth 的未知字段、类型、字节和 SHA 漂移；
- 仓库内、ignored、reparse 路径、C1B/C1C0 交叉分派、错误 acknowledgement、过期和 claim replay；
- review 确定性与敏感字段不泄露，以及仅 `error_code` 的 CLI 失败输出。

没有启动 MiniQMT、导入或检查本机真实 XTQuant、访问网络/QMT/正式数据根/旧项目/F 盘，未执行 REAL
marker，未生成真实 invocation 的 plan、authorization 或 claim；不修改 fixed sample v1–v4、reference
bundle v3、生产 capability、既有验收产物或 Alembic head `20260729_0012`。
