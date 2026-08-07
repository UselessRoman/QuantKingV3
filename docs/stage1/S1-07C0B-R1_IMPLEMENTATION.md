# S1-07C0B-R1 实施记录：授权 canonical、S1-07B 资格与正式读侧修复

状态：**已完成独立主验收（纯离线 Fake provider）**。

## 依据与范围

- 原 C0B 基提交：`7d6bd44eecd3817fcd40b84f37697c166845686a`。
- 被拒绝的初版结果：`5b461fc3be56ba6b864552fc927ff68c71ce9497`。
- R1 只修复独立主验收指出的三项阻断：授权 JSON canonical/type 合同、S1-07B
  authority bundle/逐 case 资格预检、正式 `build_eod_runtime()` 的 v3 sealed-reader。
- 没有启用 C1、S1-07D、S1-08、Stage 2、任何 REAL runner 或供应商 SDK。

## R1 收口

1. `RuntimeAuthorizationEnvelope.from_bytes()` 现在先验证顶层、authorization、identity、
   contract、profile、frozen versions 和 case IDs 的全部 JSON scalar 类型；构造后强制
   `candidate.to_bytes() == payload`。因此把数值换为字符串、把文本换为数字或任何解析后会
   被归一化的 JSON，均在签名/claim 前拒绝。
2. C0B Fake 运行时不再引用 `testing_v3.build_v3_context()` 或其 `bbbb...` placeholder。
   唯一 production context 从受管
   `reference-bundles/st-603377-authority-v3` 经 `AuthorityReferenceBundleVerifier` 构造，
   绑定已验收 SHA `94ac8da21586a13803058b80344ff1b8dd63633c091e90b8cba832b5ef6a0bea`。
   claim 前重新深验 bundle，并仅放行 `st_warning_603377_sh` 的
   `AUTHORITY_APPROVED` 资格；`calendar_sh` 等 `FRAMEWORK_ONLY` case 拒绝且不领取 claim。
   authority verifier 在 C0B preflight/read resolver 中以只读失败模式运行；bundle 缺失或
   损坏不会创建 failure file、输出目录或 claim。
3. C0B 成功 Fake evidence 现在为该已批准 `trading_status` case 的 schema-valid、
   永久 `SYNTHETIC` 离线内容。固定 runner module、descriptor/package version、Fake signer
   key-id 与 `SYNTHETIC` trust policy 都在 claim 前检查；runner module 仍只在 claim 后 import。
4. `SealedAssessmentReadVerifier` 支持 verifier-owned v3 trust resolver。正式
   `build_eod_runtime()` 注入 `ControlledFakeV3TrustResolver(data_root)`；每次 DataCenter/UoW
   读取 C0B v3 row 都重新验证 authority bundle、trust context 和 seal。未配置 REAL trust，
   因而 REAL row、bundle 缺失或篡改、seal 损坏均失败关闭。

## R1 回归证据

- C0B 定向：`22 passed`，包括原签名下 profile integer→string、nested text→integer、
  frozen version/identity 类型篡改，以及 `FRAMEWORK_ONLY calendar_sh` 在 claim/import/output 前
  被拒绝。
- C0A 攻击回归：`21 passed`；S1-07B runtime 回归：`6 passed`。
- 全部 `tests/data/validation`：`229 passed`。
- v3/v2 persistence：`29 passed`；DataCenter/read repository：`31 passed`；migration：`52 passed`。
  Alembic 唯一 head 仍为 `20260729_0012`。没有新增 migration，因为 R1 没有新增机器可查询
  字段或状态；它只让既有 seal column 所指向的 v3 证据在读侧重验。
- 额外正式 composition-root 回归证明：C0B Fake run → v3 seal → SQLite →
  `build_eod_runtime().data_uow_factory` 读取成功；随后篡改 authority bundle 后同一 row 读取
  抛出完整性错误。

## 完整质量门

质量脚本的各原始阶段均已用锁定的本地离线工具链重跑并通过：

- 后端 offline pytest：`1272 passed, 2 skipped, 3 deselected, 4 warnings`；warnings 是既有
  Qlib NumPy timedelta 与 Starlette TestClient deprecation。
- `uv lock --check --offline`、frozen sync、Ruff、format、mypy（133 source files）、架构边界、
  Markdown links 和 `git diff --check` 均通过。
- 前端 typecheck、4 文件/17 项 unit tests、production build 和 1 项 Playwright E2E 均通过。

交互执行器对单个前台命令有 5 分钟限制，前台 `scripts/quality.ps1` 两次均在其完整 pytest
阶段超过该限制后被终止（不是断言失败）。随后以相同离线工具链后台运行原始脚本，输出完整到达
`Git whitespace check`，且没有异常输出；该次完整门禁通过。独立主验收结论与完整证据见
[S1-07C0B-R1_ACCEPTANCE.md](S1-07C0B-R1_ACCEPTANCE.md)。该结论仅覆盖纯离线 Fake provider，
不构成 REAL 能力、真实来源可用性或外部访问授权。

## 外部副作用与未开始项

所有 R1 证据只在 pytest 临时目录和仓库内源文件中读写。REAL marker 数为 0；没有网络、QMT、
XTQuant、BaoStock、AKShare、Tushare、供应商 SDK、`E:\QuantKingV3Data`、`E:\QuantKing` 或
`F:\QuantKingV3Backups` 访问。未开始 C1、S1-07D、S1-08 和 Stage 2。
