# S1-07C1C0-R3 主验收证据（纯离线 XTData ST observation 运行就绪化）

- 验收结论：**通过**。
- 验收范围：固定单元 `XTQUANT × trading_status × st_warning_603377_sh` 的 MiniQMT/XTData
  真实只读 runner 控制链、provider-observation artifact 与独立 read-side verifier；所有验证只使用
  临时 Fake `xtquant.xtdata`。
- 基提交：`85d5f44a0564a487540ba072f6ba75c4dce8ee34`。
- 主验收性质：**“S1-07C1C0-R3 已完成独立主验收（纯离线 XTData ST observation 运行就绪化）；真实运行数为 0。”**

## 拒绝历史与接受提交

| 版本 | 提交 | 独立验收结论与修复主题 |
|---|---|---|
| 初版 C1C0 | `63c901721923a1b758dcb426f41ec436c593bc0c` | 未通过：claim/output 路径可逃逸、未知 plan 字段未被签名覆盖、`xtquant.__path__` 可把 `xtdata` 重定向至授权根外。 |
| C1C0-R1 | `b0104634af0a55f12fbc03088671a8a9b822300b` | 未通过：claim signer 与 authorization anchor 未在 claim 前自洽、合法 runner 重签的 manifest 语义漂移可被 verifier 接受、控制上下文未完整嵌入 C1A/C1B envelope。 |
| C1C0-R2 | `0dd7c6c08aebac9152d3e06049434b759cbb1614` | 未通过：只复验嵌入 C1B/C1A 的签名和字段，未在执行时复验实际 authority reference bundle、fixed sample 与 metadata database。 |
| C1C0-R3 | `85d5f44a0564a487540ba072f6ba75c4dce8ee34` | 接受：在 `PRECHECK`、`CLAIM`、`IMPORT`、`CALL`、`OUTPUT` 顺序中，claim 前复用 C1B/C1A 无副作用深预检，并对 metadata database SHA 重新绑定。 |

## 独立探针与门禁证据

- 删除已签发授权所引用 authority reference bundle 的 `manifest.json` 后，正式入口在 claim 前拒绝；
  Fake SDK 调用数为 0，未产生新的 claim 或最终 output。
- 替换 metadata database 后，正式入口在 claim 前拒绝；Fake SDK 调用数为 0，未产生 claim 或最终
  output。
- authorization 已消费后，正式入口在新 claim 前拒绝；Fake SDK 调用数为 0，未产生额外 claim 或最终
  output。
- C1C0-R3、C1A/C1B 定向组合测试：`80 passed`。
- 完整 `scripts/quality.ps1`：退出码 `0`；后端
  `1363 passed, 2 skipped, 3 deselected, 4 warnings`；前端 17 项单测、生产构建和 Playwright 1 项
  均通过，并已通过锁文件、Ruff、format、mypy、架构边界、Markdown 链接和 Git whitespace 检查。
- 首次完整质量门仅因沙箱无法读取既有外部 UV cache 而中止；允许只读使用既有工具链缓存后，从头完整运行
  并以退出码 `0` 完成。
- Alembic 唯一 head 为 `20260729_0012`；未新增 migration。验收时工作树 clean，`git diff --check`
  通过。

## 结论边界

固定 runner 仅允许 `xtdata.get_his_st_data("603377.SH")`，并且 observation 始终为
`BLOCKED/CONTRACT_INCOMPLETE`；它不会补写停牌、退市整理期或完整交易状态，更不会产生 `READY`。

Native XtData 官方页面尚未证明 `xtdata.get_his_st_data` 存在。此次验收没有启动 MiniQMT、导入本机
真实 XTQuant、连接 QMT/网络、读取真实行情或正式数据目录，也没有执行 REAL marker；上述真实调用和
marker 的执行数均为 0。因此这份纯离线运行就绪化记录不构成真实 XTQuant 能力、历史 ST 语义或生产
capability 的证明。

C1C1/C2、S1-07D、S1-08 和 Stage 2 均未开始、未获授权。任何真实 invocation 仍须按
source×dataset×case×invocation 取得新的明确授权。
