# S1-07C1C0 MiniQMT/XTData 真实只读 runner 纯离线实现

状态：**“C1C0 MiniQMT/XTData 真实只读 runner 纯离线实现完成，等待独立主验收；真实运行数为 0。”**

基提交：`8e9f0fb0f0e987cd9d235b657e539665c31f2c09`。

## 范围

- Stage：Stage 1 / S1-07C1C0。
- 新增独立、版本化 C1C0 plan、authorization、runner contract/profile 与 provider-observation artifact。
- 唯一固定读取是延迟加载后的 `xtdata.get_his_st_data("603377.SH")`；调用方不能覆盖代码、接口或参数。
- C1C0 authorization 绑定 runner contract ID/profile、显式 site-packages root、精确 XTQuant 包版本、adapter 版本、plan SHA、claim lineage 和 Ed25519 签名；C1B authorization 不能调度它。
- runner 不导入或使用交易、账户、报单接口；PRECHECK 通过后才 CLAIM，SDK import 只能发生在 CLAIM 之后。import、调用或解析失败都会保留 claim 与不可变 `.incomplete` 证据。
- response canonical JSON、SHA、版本、固定请求、计划/authorization/claim lineage 和 runner 签名都被深验证。即使观测到 ST/*ST/PT 区间，结论恒为 `BLOCKED/CONTRACT_INCOMPLETE`，不推断停牌、退市整理或完整交易状态；`{}` 明确表示未取得可判定数据。

## 兼容性和测试

C1B v1 failure-only runner、fixed sample v1～v4、reference bundle v3 与生产 capability 布尔值均未修改；无 migration。定向测试只创建临时 Fake `xtquant.xtdata` 包，覆盖延迟 import、唯一固定调用、package/key/contract claim 前拒绝、严格解析、claim 后失败证据和 artifact 深验证。本任务没有启动 MiniQMT、导入本机真实 XTQuant、连接 QMT/网络、读取真实行情或执行 REAL marker。
