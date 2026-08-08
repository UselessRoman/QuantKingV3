# S1-07C1C0-R3：恢复 C1B/C1A 执行时深预检

Stage：Stage 1
基提交：`0dd7c6c08aebac9152d3e06049434b759cbb1614`

**S1-07C1C0-R3 纯离线实现完成、等待独立主验收；真实运行数为 0。**

## 范围与未授权边界

- 本任务仅实现离线代码和临时 Fake `xtquant.xtdata` 包测试；未启动 MiniQMT，未导入本机真实 XTQuant，未访问网络或正式数据目录，未执行 REAL marker。
- 唯一业务调用继续固定为 `xtdata.get_his_st_data("603377.SH")`；继续拒绝交易、账户、订单、下载和订阅接口。
- 不新增 migration；不修改 fixed sample v1–v4、reference bundle v3、C1B 历史 schema/authorization/artifact/verifier 或 C1B v1 failure-only 语义。

## R2 拒绝原因与 R3 修复

R2 的 C1C0 只复验嵌入 C1B/C1A context 的签名及字段交叉绑定。若授权签发后实际 authority bundle 或 metadata database 漂移，C1C0 仍可能 claim 并调用 Fake SDK。

R3 在 C1C0 `PRECHECK` 内调用新增的无副作用 C1B extension precheck。该预检复用 C1B 已验收的完整运行前验证，并在 claim 前重新确认：

- C1B outer signature、C1A envelope、C1B/C1A plan SHA 与全部交叉绑定；
- authority `reference_bundle_root` 下 manifest、精确 SHA、文件清单，以及与 fixed sample v4 的绑定；
- metadata database 的存在、常规文件条件和 C1C0 签名计划中绑定的 SHA-256；
- XTQuant package metadata/version、authorization/runner key identity、有效期和未消费 claim；
- 固定 C1C0 outer authorization 与输出/claim 目标仍受控。

该 extension precheck 不会 claim、导入 SDK、调用 runner、写 artifact、写数据库或执行 C1B。若 bundle 或 metadata 失败，C1C0 在 `CLAIM` 前失败，不创建 claim、`.incomplete` 或最终 output。

## Read-side 边界

已发布 provider-observation 的独立 verifier 继续只依赖 artifact 中的签名证据和公钥 trust anchors。它不会重新执行 provider precheck，也不要求 reference bundle、metadata database 或任何私钥在发布后仍存在。

## 测试证据

- C1C0-R3 与 C1A/C1B 定向离线回归：`80 passed`。
- 新增：签发后删除/修改 authority manifest、删除 manifest 声明文件、模拟 fixed-sample loader 漂移、删除/替换 metadata database，均在 claim 与 Fake SDK 调用前拒绝，且无 claim、staging 或最终 output。
- 保留 R2 claim-key、经合法重签的 manifest/claim/observation 漂移、移动 artifact、私钥移除、模块来源、路径边界、严格区间和 failure staging 回归；成功发布后移走 bundle 仍可进行独立公钥验证。
- validation 历史回归：`309 passed`；完整 S1-07 组合回归（含 C0A/C0B、C1A-R3、C1B-R1 与持久化/migration 历史测试）：`179 passed`。
- `scripts/quality.ps1`：`1363 passed, 2 skipped, 3 deselected, 4 warnings`；包含锁文件、Ruff、mypy、架构边界、Markdown 链接、前端类型/单元/构建/E2E 与 Git whitespace 门禁。
