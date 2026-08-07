# S1-07C1A-R3 纯离线实现

状态：等待独立主验收。

基提交：`a9b7207955e30ab40cf20ee3afaa4bc77e8ae552`。

本次仅修复固定单元 `XTQUANT × trading_status × st_warning_603377_sh` 的 C1A
REAL 读侧交叉绑定与测试 lineage 构造；未开始 C1B、C2、S1-07D、S1-08 或 Stage 2，
未执行 REAL marker、网络、QMT、XTQuant SDK 或正式数据访问。

## R3 控制

- 正式 reader 现在将 envelope identity 与 manifest identity 精确比较；plan、attestation
  和 receipt 继续分别与该 identity 交叉绑定，因此 assessment、session、invocation、run
  与 authorization 均属于同一次受控执行。
- envelope 的 source/dataset contract、promotion profile、reference bundle ID/SHA、sample
  与 verifier-owned context 精确比较；plan 也与同一 context 精确比较。
- 保留 R2 的 authorization 内外签名、claim receipt 签发证明、runner 锚点、case、output
  scope、versions、SHA 与严格时间顺序复验。
- 测试专用工件重写器改用 `plan.json` 中声明且独立复算过的 unsigned-plan SHA，不再以
  整个 plan 文件 SHA 作为 lineage。

## 回归覆盖

使用受信 authorization、receipt 和 runner 私钥重签的工件分别制造 session identity、
bundle、contract 和 profile 漂移；每例在验证签名仍有效、plan SHA 正确后被正式 reader
拒绝。R2 的无效 authorization 签名、晚 claim、`SUCCEEDED`、Fake key、SQLite seal、UoW
和 DataCenter 回归保留。

## 数据库结论

没有新增机器可查询字段；未新增 migration，Alembic head 保持 `20260729_0012`。
