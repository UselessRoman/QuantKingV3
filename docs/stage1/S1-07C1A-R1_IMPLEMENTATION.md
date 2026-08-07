# S1-07C1A-R1 离线实现记录

状态：R1 实现完成、等待独立主验收（纯离线）

## 范围

本次只修复 S1-07C1A 的三个离线控制链阻断，固定单元仍为
`XTQUANT × trading_status × st_warning_603377_sh`。没有执行 REAL marker，
没有导入 XTQuant SDK，也没有访问网络、QMT 或运行数据/备份根目录。

## REAL trust anchor

- 新增 R1 专用 Ed25519 私钥 signer 与公钥 verifier anchor。私钥和读侧公钥分别由
  显式绝对本地 PEM 文件加载；生产代码不生成默认密钥，也不从环境、Git、产物或日志读取。
- key-id 固定从验证公钥的 Raw Ed25519 bytes 的 SHA-256 指纹推导为
  `real-ed25519-sha256-...`。anchor 先校验该推导值，再与独立配置的 allowlist 比较；
  任何调用方传入的 key-id 都不能建立 REAL 信任。
- 已知 C0A/C0B Fake key material 与 `quantking.fake-*` 身份被永久拒绝为 REAL。
  C0A/C0B 的 HMAC/Fake 合同、字节表示和 SYNTHETIC 语义均未改变。

## R1 evidence 与 lineage

- 新建显式版本的 `source-validation-real-envelope.v1`、
  `source-validation-real-result-attestation.v1` 与
  `source-assessment-real-manifest.v1`；C0B 的 v3 目录仍按原分派读取。
- 每个 REAL run 固定记录并复验 canonical authorization envelope SHA、canonical plan SHA、
  immutable claim receipt SHA。receipt 绑定 authorization、invocation、run ID、claim 时间、
  nonce、authorization 文件 SHA 及 plan SHA。
- run attestation、run manifest、assessment manifest 和 sealed formal reader 都复验这些
  binding。缺失、替换或非 canonical authorization/plan/receipt 会使执行、持久化或读取
  fail closed。
- 现有 SQLite 行已保存 immutable manifest、attestation 和 seal SHA；R1 lineage 已封入
  这些已封存工件，未新增机器可查询字段，因此没有 migration，Alembic head 仍为
  `20260729_0012`。

## runner 边界

- 生产 `xtquant_real` runner 已不依赖 `testing_v3.build_v3_run`，改用专用、clock 驱动
  R1 writer。attestation 的开始/结束时间来自实际 runner 执行，并强制处于 authorization
  有效期内。
- 正式 factory 不接收 adapter 或成功 builder；它只能通过现有适配器产生
  `CAPABILITY_UNSUPPORTED`。isolated success writer 仅由测试路径直接使用，不进入生产
  factory。
- `trading_status` capability 继续保持 `False`；REAL 失败工件固定为可信
  `REAL/BLOCKED`，没有 success evidence 或 READY gate。

## 回归证据

- `tests/data/validation/test_s1_07c1a_real_control_chain.py`：13 passed。
- C0A/C0B、旧 v2/v3 persistence/read 回归及 R1：170 passed。
- 全量离线 pytest：1285 passed、2 skipped、3 deselected、4 warnings。
- Ruff、格式检查与 `mypy src/quantking`：通过。
- architecture boundary、Markdown link、Alembic head（`20260729_0012`）与
  `git diff --check`：通过。

完整 `scripts/quality.ps1` 的 R1 运行因执行环境拒绝访问既有离线 UV cache 而未能完成；
该限制不涉及网络或 REAL marker。其余可在仓库内直接执行的离线定向回归均已完成。

未开始 C1B、C2、S1-07D、S1-08 或 Stage 2。
