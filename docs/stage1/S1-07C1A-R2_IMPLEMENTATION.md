# S1-07C1A-R2 纯离线实现

状态：等待独立主验收。

基提交：`a49c1a082a324a9317a80f2e678b9c59681e9ad7`。

本次只收紧固定单元 `XTQUANT × trading_status × st_warning_603377_sh` 的 C1A
离线 REAL 控制链，不开始 C1B、C2、S1-07D、S1-08 或 Stage 2，也不执行 REAL
marker、网络、QMT、XTQuant SDK 或正式数据访问。

## R2 控制

- R2 evidence、assessment 与 claim receipt 使用显式的新版本合同；C0A/C0B 的
  v2/v3 合同与字节保持不变。
- claim receipt 由显式本地 authorization 私钥签发，持久化与读侧以独立配置的
  authorization 公钥锚点复验其签名、authorization ID、invocation/run ID、nonce、
  envelope SHA 和 plan SHA。
- REAL reader、SQLite seal 复验外层 runtime authorization 及内层 execution
  authorization 的 Ed25519 签名，并与独立的 runner 公钥锚点分别绑定。
- reader 复验 envelope、plan、receipt、attestation 和 assessment 的 identity、
  bundle、contract/profile、sample、versions、case、output scope、key ID 与 SHA；
  时间顺序强制为
  `valid_from <= claimed_at <= started_at < ended_at < expires_at`。
- C1A production writer 只生成 `CAPABILITY_UNSUPPORTED` 的 `REAL/FAILED/BLOCKED`
  evidence。`SUCCEEDED` 不能由正式 runtime、persistence 或 reader 接受；成功形状
  的恶意构造仅存在于测试代码，用于验证拒绝行为。

## 回归覆盖

- 授权内外层签名置零、重新计算 lineage 并由受信 runner 重签，正式 reader 拒绝。
- 授权签名正确但 claim receipt 重签为执行后时间，正式 reader 拒绝。
- 测试专用的签名 `SUCCEEDED` 形状被正式 runtime 拒绝。
- 保留 Fake key 隔离、key substitution、canonical/expiry/preclaim、package/runner
  precheck、claim 保留、lineage 替换、SQLite seal、UoW 与 DataCenter 读侧回归。

## 数据库结论

没有新增机器可查询字段；未新增 migration，Alembic head 保持
`20260729_0012`。
