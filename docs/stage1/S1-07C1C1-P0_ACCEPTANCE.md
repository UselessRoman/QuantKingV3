# S1-07C1C1-P0 独立主验收记录

状态：**“S1-07C1C1-P0 已完成独立主验收（纯离线正式 file-only 操作入口与逐次授权交接）；C1C1 真实运行数为 0，仍未授权、未开始。”**

## 验收范围与提交

- Stage：Stage 1 / S1-07C1C1-P0。
- 基提交：`17354fa160a8f97e4721a2c620709ec12b43e401`。
- 被接受结果提交：`5054d2354b77dd6cf36cf3d62b8fbfef0113e83d`。
- 本次独立主验收只整理并复核纯离线证据；验收开始时工作树干净，`git diff --check` 通过。

本结论只证明正式入口的 canonical plan/authorization 文件、file-only CLI、确定性且脱敏的
review，以及受精确 acknowledgement 约束的 execute 入口已经成立。它不证明本机
`xtdata.get_his_st_data` 方法可用，也不证明 MiniQMT 已启动或连接。操作手册中的授权模板只是
人工授权格式，不是用户授权本身。

## 已接受的控制边界

- `prepare` 仅以 C1B 正式 canonical loader 和 live-extension precheck 接受 C1B authorization，
  并产出 canonical C1C0 plan；`issue` 仅从 canonical plan 签发 authorization；`review` 仅从
  已签名 canonical authorization 推导；`execute` 仅接受该 canonical authorization 和精确
  `I_ACKNOWLEDGE_C1C0_R3_AUTHORIZED_XTDATA_ST_OBSERVATION` acknowledgement。
- plan、authorization 与相关输出必须逐字节 canonical、严格字段及类型有效，并且位于 Git
  仓库之外。仓库内、ignored 目录、reparse point 或 symlink 路径会在无副作用的情况下拒绝；
  既有文件不覆盖。C1B 与 C1C0 文件不能交叉分派。
- review 由 authorization 推导并稳定绑定 authorization SHA 和 plan/C1B authorization SHA；
  重复同一 authorization 的 JSON 字节一致。输出不含私钥内容或路径、claim nonce、任何
  authorization/runner signature、SDK response 或其他敏感供应商内容。
- execute 仍受精确 acknowledgement、C1B→plan→C1C0 authorization 签名链、单次 claim 与
  C1C0-R3 深验证共同控制；没有由本验收放宽任何真实调用限制。

## 独立实际子进程探针

探针只使用临时 Fake SDK、密钥、数据库和 reference bundle；不加载本机 XTQuant。通过正式
`scripts/run_s1_07c1c0.py` 子进程实测得到：

- `prepare` 退出码为 0；`issue` 退出码为 0；`review` 退出码为 0。
- 两次 `review` 的 SHA 和输出字节完全一致，且未出现私钥路径、nonce、signature 或 response。
- 错误 acknowledgement 稳定返回 `C1C0_ACKNOWLEDGEMENT_REQUIRED`；缺失必填参数稳定返回
  `C1C0_CLI_ARGUMENT_INVALID`。

该探针没有对成功 execute 作真实或 Fake 的调用；它只验证入口在错误 acknowledgement 处拒绝，
因而不会创建 C1C1 真实 invocation 或 claim。

## 回归与质量证据

- S1-07 历史回归：`321 passed`。
- 被接受实现提交的完整质量门：`1389 passed, 2 skipped, 3 deselected, 4 warnings`。
- 本次状态闭环后的完整质量门：`1390 passed, 2 skipped, 3 deselected, 4 warnings`；新增的通过项
  仅为本验收记录的状态文档断言。
- 前端：17 个测试通过、生产构建通过、Playwright 1 项通过。
- Alembic 唯一 head：`20260729_0012`。
- Markdown 链接检查与 `git diff --check` 均通过。

## 未授权边界与后续

本任务中 REAL marker、真实 invocation、XTQuant/MiniQMT、网络和正式数据根访问均为 0；没有启动
MiniQMT，没有加载或调用真实 XTQuant，也没有访问网络或正式数据根。

后续唯一候选是：用户基于一次具体 review 的完整事实，对该 review 所绑定的单次 C1C1 invocation
另行作出明确授权。授权失败、过期或 claim 后失败都不能复用原 authorization。该验收不授权 C2、
S1-07D、S1-08 或 Stage 2，也不自动开始任何后续步骤。
