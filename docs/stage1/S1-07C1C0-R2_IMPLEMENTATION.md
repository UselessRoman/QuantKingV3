# S1-07C1C0-R2：闭合 XTData ST observation 证据链

Stage：Stage 1
基提交：`b0104634af0a55f12fbc03088671a8a9b822300b`

**S1-07C1C0-R2 纯离线实现完成、等待独立主验收；真实运行数为 0。**

## 范围与未授权边界

- 本任务只实现离线代码和临时 Fake `xtquant.xtdata` 包回归；未启动 MiniQMT，未导入本机真实 XTQuant，未访问网络或正式数据目录，未执行 REAL marker。
- 唯一 SDK 业务调用仍固定为 `xtdata.get_his_st_data("603377.SH")`。C1C0 继续拒绝 `xtquant.xttrader`、交易、账户、订单、下载和订阅路径。
- C1C0 的观察无论是否取得 ST 区间，结论均为 `BLOCKED/CONTRACT_INCOMPLETE`，不补全停牌、退市整理期或完整交易状态，也不晋级 `READY`。
- 不新增 migration；不修改 fixed sample v1–v4、reference bundle v3、生产 capability 开关、C1B v1 failure-only 入口或其历史 artifact/verifier 语义。

## R1 拒绝原因与修复映射

| R1 阻断 | R2 修复 |
| --- | --- |
| claim 私钥可与授权公钥锚点不一致，writer 可发布自身 verifier 拒绝的目录 | C1C0 plan 嵌入已签名 C1B authorization（含 C1A envelope）；claim 前同时验证 C1B/C1A 公钥链、C1C0 外层签名、授权私钥、claim issuer 与同一 key ID。claim receipt 写入前后验证签名、identity、authorization/plan SHA、nonce 和有效期。 |
| runner 重签 manifest 后可漂移 schema 等语义字段 | 深 verifier 将 observation/manifest schema、adapter/package、runner contract/profile/version/key ID、固定请求、事件序列、结论、所有 SHA、canonical response 与完整 observation 逐项绑定到签名 plan 和固定合同。 |
| 仅复用零散 C1A/C1B helper，形成较弱平行入口 | C1C0 只接受外层 C1C0 canonical authorization；其 plan 完整嵌入并公开验证 C1B authorization、C1A envelope、identity、source/dataset/case、fixed-sample SHA、reference bundle、source contract、promotion profile、frozen versions、时效、nonce、output scope 与 signer/runner identity。C1B 文件本身不能分派 C1C0。 |

## R2 artifact 合同

1. 成功顺序固定为 `PRECHECK → CLAIM → IMPORT → CALL → OUTPUT`；SDK 导入与调用只能在已签名且复验的 claim 之后。
2. 成功 artifact 使用 `.incomplete` staging。writer 在原子发布前以正式深 verifier 的 staging 模式自检，发布后再次以最终目录模式验证；任何失败保留 claim 和 `.incomplete`，不留下伪完整最终目录。
3. read-side verifier 仅使用 artifact 中已签名 C1C0/C1B/C1A 证据与公钥 trust anchor；已发布 artifact 在授权及 runner 私钥移除后仍可验证。
4. `ST`、`*ST`、`PT` 响应仅接受唯一的 `[[start, end], ...]` 区间形态；拒绝未知字段、重复、重叠、倒序、零长度和非法日期。空映射只表示 `INDETERMINATE`。

## 测试证据

- C1C0-R2 定向纯离线测试：`35 passed`。
- 覆盖 Fake SDK 合法顺序与唯一固定调用、C1B-only 文件拒绝、授权私钥/公钥错配、经合法 runner 重签的 manifest/claim/observation 语义漂移、移动 artifact、私钥移除后的独立验证、ID 逃逸、未知字段、模块来源重定向、预载模块、严格区间及 failure staging。
- S1-07 历史回归（C0A/C0B、C1A、C1B、C1C0 及相关持久化）：`173 passed`。
- 完整 `scripts/quality.ps1`：离线 pytest `1357 passed, 2 skipped, 3 deselected`；Ruff、mypy、架构边界、Markdown 链接、前端类型检查、单元测试、生产构建、E2E 与 Git whitespace 检查全部通过。

## 官方文档边界

官方内置 Python 接口说明列出 `ContextInfo.get_his_st_data` 及其 ST 历史语义；Native XtData 页面目前未列出 `xtdata.get_his_st_data`。因此，本任务不声称该方法已在本机 Native XTQuant 证明存在；该事实仍只可由未来单独授权的最小真实探针确认。

- [内置 Python 行情函数说明](https://dict.thinktrader.net/innerApi/data_function.html)
- [Native XtData 文档](https://dict.thinktrader.net/nativeApi/xtdata.html)
