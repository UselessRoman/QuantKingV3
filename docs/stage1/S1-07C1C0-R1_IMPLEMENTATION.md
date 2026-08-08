# S1-07C1C0-R1 纯离线 XTData ST observation 控制链修复

状态：**“S1-07C1C0-R1 纯离线实现完成、等待独立主验收；真实运行数为 0。”**

基提交：`63c901721923a1b758dcb426f41ec436c593bc0c`。

## 初版拒绝与修复映射

初版 C1C0 独立主验收未通过：非结构化 `authorization_id`/`run_id` 可逃逸 claim/output 根；authorization 的嵌套 plan 接受未知字段；以及 `xtquant.__path__` 能将 `xtdata` 重定向到授权 SDK 根之外。本 R1 不修改 C1B v1 failure-only 行为、历史 authorization/artifact/verifier 或任何 frozen sample、reference bundle、capability 配置。

- 使用 `ValidationAttemptIdentityV3` 的 C1A 结构化 UUID 身份；输出和 claim 只从 UUID hex 构造，且复用 C1B 的仓库外、reparse-point、`resolve` 和 Windows containment 策略。
- plan、authorization 及嵌套 identity/请求/签发 context 均有精确字段集、类型和 canonical bytes 检查；SHA 与 Ed25519 签名覆盖原始完整 plan，未知或遗漏字段在 claim 前失败。
- 加入 C1A 风格的时效窗口、确切确认口令和 `RealClaimReceipt` 签名。流程固定为 `PRECHECK → CLAIM → IMPORT → CALL → OUTPUT`；claim 后才执行延迟 SDK load。
- C1C0 runner 复用 `ExplicitXtQuantModuleLoader`，检查预载模块、`xtquant` 和 `xtquant.xtdata` 的最终文件均处于授权根，并拒绝任何交易模块加载。唯一业务调用仍为 `xtdata.get_his_st_data("603377.SH")`。
- ST 响应只允许 `{ST|*ST|PT: [[start, end], ...]}`。空映射是 `INDETERMINATE`；重复、重叠、倒序、零长度、非法日期和未知形态均拒绝，且不排序或去重改变供应方语义。
- observation 先写入 `.incomplete` staging；逐文件 SHA 和严格清单自检成功才原子发布。失败保留 signed claim 与 `.incomplete/failure.json`。深验证只从已签名 authorization plan 导出 trust anchors，拒绝额外/缺失文件和 manifest、response、parsed observation 漂移。

## 离线证据与边界

新增 Fake SDK 回归覆盖 ID 逃逸、未知 plan 字段、`__path__` 重定向、外部 `sys.modules`、所有声明的区间缺陷、artifact 漂移、失败 staging 和合法固定调用。没有 migration；Alembic head 保持 `20260729_0012`。本任务不启动 MiniQMT，不导入本机真实 XTQuant，不连接网络/QMT，不读取正式数据目录，也不执行 REAL marker。
