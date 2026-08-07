# S1-07C1A 实施记录：XTQuant REAL 单单元控制链（纯离线）

状态：S1-07C1A 实现完成、等待独立主验收（纯离线）。

## 范围与边界

- Stage：Stage 1 / S1-07C1A。
- 基提交：`f2e60f0c6cf282530fb1c70987190761944fcc75`。
- 固定唯一单元：`XTQUANT × trading_status × st_warning_603377_sh`。
- 本次仅实现仓库内的 REAL 授权、签名、runner、v3 evidence/seal、SQLite 持久化和正式读侧
  信任分派；全部测试使用 `tmp_path`、临时 key 和临时 `xtquant-*.dist-info`。
- 未运行 `real_xtquant` 或 `real_external` marker，未访问网络、QMT、XTQuant 安装目录、
  `E:\QuantKingV3Data`、`E:\QuantKing` 或 `F:\QuantKingV3Backups`。REAL marker 数仍为 0。

这不是 C1、真实 XTQuant 单元或任何真实来源能力的完成声明。它只建立了以后独立授权真实运行
所需的、离线可复核的控制边界。

## REAL 授权与信任边界

新增 `runtime-authorization-envelope.real.v1`，它与 C0B 的
`runtime-authorization-envelope.v2` 分离；purpose 固定为
`S1-07C1A_XTQUANT_TRADING_STATUS_REAL`。旧 v2/Fake envelope、旧 CLI DTO 和任何环境变量
都不能进入该 loader。

严格 canonical 文件绑定：source、dataset、唯一 approved case、authority bundle ID/SHA、精确
output root、八项版本、assessment/authorization/invocation/session/run ID、有效期/过期时间、
nonce、一次性 claim，以及 verifier-owned runner key ID。解析拒绝未知字段、JSON 类型漂移和
非 canonical 字节；运行时逐项重建固定 plan 并比较全部 binding。

REAL signer/verifier 仅从显式绝对私钥文件读取；不生成默认 key，不从 Git、artifact、日志或
环境变量读取。`RealV3RunnerTrustPolicy` 的 allowlist 由 verifier 提供的 key ID 决定，并独立
推导 `EvidenceKind.REAL`；Fake key ID 被构造、loader 和 trust policy 三层拒绝。默认没有
REAL trust resolver 时，正式读侧继续拒绝 REAL row。

## Runner 与 capability

`RealXtQuantValidationRuntime` 的执行顺序固定为：

```text
canonical REAL file load
  → output / authority bundle / case / signer / fixed runner declaration / explicit package metadata / versions precheck
  → one-time claim
  → fixed runner import
  → immutable v3 evidence (or terminal .incomplete failure)
```

package 版本只由显式 site-packages root 下的 `xtquant-*.dist-info/METADATA` 只读探针得到，
从不读取本机安装环境；runner 自身不导入 `xtquant`。SDK 的未来延迟加载位置仅能在
`data/adapters`。

`trading_status` 的 capability 继续为 `False`。默认适配器不从成交量、当前名称或
`InstrumentStatus` 推断历史 ST 状态，不加载 SDK，并在 claim 后产出可信的
`CAPABILITY_UNSUPPORTED` 失败 attestation。该失败是 `REAL/BLOCKED_EXTERNAL_VALIDATION`，
不存在 canonical/quality/difference 等 success credential，更不可能形成 READY gate。

测试中的 isolated Fake adapter 与临时 REAL key 只用于验证控制链中成功/失败两条内部时序；它们
不访问供应商，不能计入 REAL marker 或真实稳定运行。

## v3 seal、SQLite 与正式读侧

沿用既有 v3 immutable evidence 与 0012 SQLite write gate，未修改 C0A/C0B 的 v2/v3 字节语义。
REAL assessment 同样只能从受管目录 finalize → seal → 深验 → SQLite；调用方不能写入 assessment
DTO、gate 或状态。

读侧新增按持久行 evidence family 分派的 resolver：C0B 的 SYNTHETIC trust 不变；只有
`build_eod_runtime(..., real_v3_trust_resolver=...)` 明确注入后，才选择 REAL trust。每次读都会
重新验证 seal、authority bundle、context、签名和行血缘。缺 resolver、seal/row 篡改、bundle 或
key/trust context 变化均失败关闭。

## Migration

没有新增 Alembic migration，head 继续为 `20260729_0012`。本任务没有新增机器可查询字段或状态：
REAL/SYNTHETIC、BLOCKED/READY、seal SHA 和证据 lineage 都由既有表字段与 sealed artifact 的深验
合同表达。没有修改 0012。

## 离线测试覆盖

定向测试覆盖：Fake/C0B key 与旧 envelope 拒绝、未知字段/类型/非 canonical、过期、source/dataset/
case/bundle/output/version/key-ID binding、缺 trust/runner/package metadata 的 claim 前停止、claim 后
import/runner 崩溃保留、默认 capability failure、isolated Fake adapter 成功链、v3 seal → SQLite →
DataCenter/UoW 读侧、默认无 REAL trust 拒绝，以及 verifier key 改变后的失败关闭。

S1-07C1A 没有开始 C1B、C2、S1-07D、S1-08 或 Stage 2。
