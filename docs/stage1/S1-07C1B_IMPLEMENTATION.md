# S1-07C1B 纯离线运行就绪化实现

状态：**“C1B 纯离线就绪化实现完成，等待独立主验收；真实运行数为 0。”**

基提交：`23a8212d191f10ad7a6037b86026661b5bff11b2`。

后续状态：C1B 初版独立主验收未通过，原因是计划、完整 authorization 和 signer 私钥的
仓库内路径没有被拒绝。该阻断仅由 S1-07C1B-R1 的纯离线路径边界修复处理；本文件不是
主验收证据，也不表示 C1B 已主验收。

## 范围与授权边界

- Stage：Stage 1 / S1-07C1B。
- 固定唯一单元：`XTQUANT × trading_status × st_warning_603377_sh`。
- 授权只包括仓库内的纯离线运行就绪化；没有执行真实 invocation 或 REAL marker，未导入
  XTQuant SDK，未连接 QMT/网络，未写正式数据根，未读取旧项目或访问 F 盘。
- C1B 包装并复验已验收的 C1A-R3 failure-only v2 合同；没有修改、重签或扩展
  C0A/C0B/C1A 的合同、工件或 `CAPABILITY_UNSUPPORTED` 边界。

## 唯一正式入口

`scripts/run_s1_07c1b.py` 是该固定单元的唯一正式操作入口，命令彼此分离：

```text
prepare（别名 plan） → issue（别名 authorize） → review → execute
```

- `prepare` 只生成 canonical、可审查计划，不签发授权、claim 或执行。
- `issue` 只从 canonical 计划签发一个 canonical C1B authorization 文件，不执行。
- `review` 仅输出脱敏审查字段；不会输出私钥字节、签名、claim nonce 或完整授权文件。
- `execute` 只接受 `--authorization-file` 和精确的
  `I_ACKNOWLEDGE_C1B_AUTHORIZED_XTQUANT_TRADING_STATUS_EXECUTION`；不接受 source、输出路径、
  key ID、旧 DTO 或其他调用方覆盖参数。

`prepare` 的所有输入都是必填并进入 plan SHA：source、dataset、case、显式 XTQuant
site-packages 根及精确包版本、reference bundle ID/SHA、output/claim root、预先存在的
metadata SQLite、assessment/session/invocation/run/authorization ID、authorization/runner 的
私钥/公钥文件和 verifier allowlist、时间窗、claim nonce、以及 adapter/package/code/schema/
rule/threshold/sample/config 版本。C1B 只在这些值与 C1A 重建计划逐项一致时签发；authorization
再同时绑定 C1B plan SHA 和 C1A plan SHA。

计划和 authorization 必须保存在 Git 外的受控目录。私钥字节、签名和完整 authorization 均不写入
Git、日志或对话。

## 执行控制

执行前重新读取受控 bundle、显式 dist-info `METADATA`、密钥对和 allowlist，并逐项复验 plan 与
authorization 的 identity、bundle、profile、versions、output scope 和 C1A envelope。相对路径、
reparse point、包版本漂移、key substitution、过期或已消费 authorization 均失败关闭，且在 claim、
SDK/runner import 和 output 创建之前拒绝。

通过预检后顺序固定为：

```text
PRECHECK → CLAIM → IMPORT → OUTPUT → VERIFY → SEAL → PERSIST
```

`IMPORT` 只导入 C1A 固定 runner；该 runner 仍不导入 `xtquant`，只生成
`CAPABILITY_UNSUPPORTED` 的 failure evidence。没有生产 `SUCCEEDED` writer、READY capability 或
历史 ST 推断。claim 后的 import、runner 或 output 失败由 C1A 保留一次性 claim 与不可覆盖的
`.incomplete` 证据；verify/seal/persist 失败也保留既有失败证据和持久化失败记录。

## 迁移与验证

没有新增机器可查询字段或 Alembic migration；head 保持 `20260729_0012`。

定向离线测试位于 `tests/data/validation/test_s1_07c1b_operation.py`，全部使用 `tmp_path`、临时
Ed25519 key、临时 SQLite 和临时 dist-info 元数据。未新增 REAL marker；新增 marker 数为 0，实际
执行 marker 数为 0。

本实现不开始真实 invocation、C2、S1-07D、S1-08 或 Stage 2。真实 XTQuant 运行仍需要新的、
单独的 source×dataset×case×invocation 授权。
