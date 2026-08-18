# Stage 1 验收矩阵

**Stage 1 已于 2026-08-18 完成独立终验。** S1-01～S1-06、K1、K2 的历史主验收继续
有效；S1-07K4、S1-08R2 与 S1-08R3 的现行闭环由各自独立验收记录证明。最终结论见
[S1-08 / Stage 1 最终独立验收记录](S1-08_ACCEPTANCE.md)。

## 任务状态

| 任务 | 状态 | 证据 |
|---|---|---|
| S1-01 | 已主验收 | [验收记录](S1-01_ACCEPTANCE.md) |
| S1-02 | 已主验收 | [验收记录](S1-02_ACCEPTANCE.md) |
| S1-03 | 历史离线主验收；目标路线已替换 | [验收记录](S1-03_ACCEPTANCE.md) |
| S1-04 | 已主验收 | [验收记录](S1-04_ACCEPTANCE.md) |
| S1-05 | 已主验收 | [验收记录](S1-05_ACCEPTANCE.md) |
| S1-06 | 已主验收 | [验收记录](S1-06_ACCEPTANCE.md) |
| S1-07K3 | 历史基线完成 | [历史记录](S1-07K3_TASK.md) |
| S1-07K4 | 已主验收（2026-08-17：**S1-07K4 独立验收通过**，形成单一 K4 提交）：K4A/K4B 离线实现完成；K4C-R1（当时独立验收不通过，历史事实）修复、R2 真实实测、R3（ADR-022 + Phase B 9/9）与 R4（ADR-023）依次交付，**K4C-R4 已于 2026-08-16 独立验收通过**，k4 阈值已按 ADR-024 接受重新冻结；**K4D 真实只读 inventory 已于 2026-08-16 获授权执行一次并成功**（5,208 文件、零读取错误、Stage 0 基线差异 0/0）；**K4D 真实 pilot 两次 attempt 均失败即停（2026-08-16/08-17，ADR-026，数据根零写入、授权已消费）**；**旧 XTQuant 迁移已按 ADR-027 拒绝并完成 K4D 决策收口（第 3 次 pilot、全量诊断与 full 均不执行）** | [验收记录](S1-07K4_ACCEPTANCE.md)、[任务卡](S1-07K4_TASK.md)、[实施记录](S1-07K4_IMPLEMENTATION.md) |
| S1-08R2 | 已独立验收（2026-08-17：**S1-08R2 独立只读验收通过**）：SQLite BUSY/LOCKED 有界重试、合法空版本幂等收敛、跨进程单发布者锁、硬中断恢复、manifest 驱动（`s1-08r2-release-manifest.v1` + `RELEASE_SAFETY_CAPS`）的范围/预算/血缘、只读 manifest verifier 与 DataCenter 来源投影/逐证券 coverage 均以合成数据验收；R2 零真实网络、零旧根读取、零正式数据根写入、零 F 盘访问 | [验收记录](S1-08R2_ACCEPTANCE.md)、[任务卡](S1-08R2_TASK.md)、[实施记录](S1-08R2_IMPLEMENTATION.md) |
| S1-08R3 | **已独立终验（2026-08-18，R3D2-R2 PASS）**：C1-R4 发布唯一 PUBLISHED 日历；E1 唯一正式 Job SUCCEEDED 并发布唯一 PUBLISHED 未复权日线版本；R3C 双幂等、E1R/R3V2 只读复验、82 行 A/B 双实际来源逐键证明与 R3F2 两项精确守卫修复均通过；冻结 evidence 未修改，最终离线门禁全绿 | [最终验收记录](S1-08_ACCEPTANCE.md)、[任务卡](S1-08R3_TASK.md)、[实施记录](S1-08R3_IMPLEMENTATION.md) |

## S1-07K1 验收

`akshare==1.18.83` 已精确落锁。R1 腾讯/Sina 四组 complete；D1 两只东财证券各尝试一次、零重试，均为 52 upstream/41 Canonical、`complete=True`，Canonical 日期 `2026-06-11..2026-08-07`，冻结单位策略一致。D1 marker `1 passed, 4 deselected`、failure count=0，代理环境已恢复。综合六组真实 complete 与全套合同，K1 功能验收通过。

已满足：

- 三个适配器只能位于数据适配器层，默认导入不触发网络。
- 三个 endpoint 的原始字段分别映射到同一未复权日线 schema。
- Raw 和 Canonical 候选都能区分 `provider=AKSHARE` 与真实 upstream。
- 日期、价格、成交量和成交额单位经真实小样本确认；未知单位不得猜测。
- 空结果、限流、上游 schema 变化、超时和非交易日有稳定错误分类。
- 真实探针只访问授权的 AKShare 公开上游，不启动 QMT、不使用账号密码、不写正式数据根。
- XTQuant 生产适配、配置、探针和专用测试已删除；通用数据主链回归通过。
- `DataSource` 旧候选集合、active `SourceCandidateAssessment`、24 单元矩阵和 DataCenter readiness 依赖已删除；历史迁移字节保留。
- 生产 EOD 只接受 `daily_bar_unadjusted`，其他历史 schema 不触发任何 upstream。
- R1 前完整质量门和 `git diff --check` 通过，可作为回归基线。
- R2 后完整 `scripts/quality.ps1` 通过：922 passed、2 skipped、4 deselected；Ruff、216 files formatted、mypy 110、架构、Markdown、前端、Playwright 和 git whitespace 全部通过。
- D1 后完整 `scripts/quality.ps1` 通过：935 passed、2 skipped、5 deselected、4 warnings（185.28s）；Ruff、218 files formatted、mypy 110、架构、Markdown、前端 typecheck、17 unit、build、Playwright 1 和 git whitespace 全部通过，总脚本 222.3s。该结果为当前验收主证据；R2 的 922 passed 仅作历史基线。

保留风险与交付状态：

- R1 时东财 `600000.SH`、`000001.SZ` 均为 `AKSHARE_NETWORK`，没有 Raw/Canonical；D1 在保持请求合同不变、仅清除当前代理路径后两组均 complete，实证此前根因在当前代理路径/出口。
- R1 marker 的 4 个阻断已由 D1 两组东财 complete 消解；腾讯/Sina 与东财六组共同构成 K1 真实功能证据。
- R1 后完整 `scripts/quality.ps1` 已通过：918 passed、2 skipped、3 deselected；Ruff、format、mypy、架构、Markdown、前端和 E2E 均通过。
- 原三源、R1、R2 和 D1 真实授权均已消耗，禁止重跑。D1 后完整质量门 935 passed，当前质量证据完整。
- K1 已提交为 `f98f1d9 feat(data): complete S1-07K1 AKShare integration`；该条只记录 K1
  验收时的提交边界，不描述当前工作树。

## S1-07K2 验收

状态：纯离线实现完成并通过独立主验收；已提交为
`0ebf84b feat(data): complete S1-07K2 tri-source resolution`。

- A/B 正常一致时不需要 C。
- A 或 B 失败/缺行/分歧时才调用 C。
- 每个决定至少有两个独立 upstream 在冻结阈值内一致。
- 选源优先级固定为东财、腾讯、新浪，但优先级不能越过一致性门。
- 无两源一致时隔离且不发布；不得静默填补。
- 三源 Raw、DifferenceReport、`ResolutionDecision`、选择来源和决定原因可复查。
- active `SourceOverride`、旧 assessment 和 EOD success/failure evidence 已从运行时、API 和 Web 退役；历史迁移仍可审计。
- 中断 Job 关闭并由新 Job 重试；相同输入指纹只复用 manifest/`COMPLETE` 完整的已发布版本。
- 质量门、幂等、失败恢复、EOD 和 Web 合同无回归。

独立主验收证据：

- 锁文件离线检查解析 222 packages，frozen sync 检查 216 packages。
- 完整离线 pytest：889 passed、2 skipped、5 deselected、4 warnings（131.62s）；另有 K2 组合聚焦 122 passed、EOD 单文件 32 passed。
- Ruff、194 files format check、mypy 111 source files、架构边界、23 个文档状态测试、Markdown 链接和 `git diff --check` 全部通过。
- 前端 typecheck、17 unit、production build 和 Playwright 1 passed（6.4s，Web server 正常关闭）全部通过。
- 固化 `scripts/quality.ps1` 在共享 `E:\Apps` UV cache 的 Windows ACL 处被环境阻断；上述独立验收使用仓库忽略的 `.uv-cache` 逐项执行了同等门禁。该环境限制不掩盖测试失败。
- K2 未运行真实网络、未写正式数据根。Stage 1 仍须完成 S1-08 才能最终验收。

## S1-07K3 验收状态

状态：离线实现完成并已提交（`38103c1`、`31419bc`、`648890e`）；代表性真实证据被东财环境阻断。

- `baostock==0.9.3` 已精确锁定；适配器通过独立子进程硬超时、稳定错误、分页计数、Raw 原字段、
  官方 `preclose` 保留及 Canonical 前序未复权 close 派生合同。
- 已提交基线固定来源为 BaoStock A、东财 B、腾讯 C、新浪 D；正常 A/B、逐级 C/D、无共识隔离和四源
  endpoint/protocol/HTTP 预算均有离线回归，XTQuant 只允许 `DATA_ONLY` 导入。
- 旧日线 inventory/managed import/verifier 已在 `tmp_path` 合成 Parquet 上验证，不读取旧根、
  不创建 Warehouse/PUBLISHED；实际 5,208 候选文件清单已于 2026-08-16 获授权执行成功
  （ADR-025），两次真实 pilot 均失败即停（ADR-026），迁移最终按用户决定拒绝并完成 K4D
  决策收口（ADR-027），full 不再执行。
  K3C CLI `quantking-legacy-import`（inventory/import/dry-run，合成与 REAL marker 双边界）已落地。
- 默认正式 EOD、K3 Resolution artifact、DataCenter 安全 lineage 和 immutable read-only release
  verifier 已通过合成端到端；K2 三源 frozen/artifact 兼容回归保留。
- 差异 manifest k3.v3 显式记录 `unit_policy_versions` 并经 verifier 校验；输入指纹
  `legacy_import_manifest_sha256` 的 None→真实 SHA 替换路径有专门测试。
- 真实执行：2026-08-12 固定 marker 1/8；2026-08-13 V2 代表性 marker（24 次计划）三次均为
  BaoStock/`000001.SZ` 成功、东财断连即停；连通性探针证明腾讯/新浪 complete、仅东财 HTTP
  层不可达。根因=东财 IPv6 CDN 数据断流，已按 ADR-019 把东财适配器钉死 IPv4（写入
  provider_payload `resolution_policy_version`）；随后东财对本出口 IP 限流。证据固化于
  `docs/stage1/evidence/`。
- 以上均不足以形成代表性阈值证据，K3 candidate 继续 inactive，生产仍使用历史零容差并
  fail closed。
- 2026-08-14 FTShare 资格验收失败并由 ADR-020 拒绝；ADR-021 已固定目标路线为 BaoStock A、
  腾讯 B、 新浪 C。东财与 FTShare 均不进入新的正式路线，已提交四角色实现仍待 S1-07K4 重构。

Stage 1 已依次完成 S1-07K4、S1-08R2 与 S1-08R3；历史失败 Job 和替代路线事实继续保留。

## Stage 1 最终完成条件

执行边界见 [S1-08 任务卡](S1-08_TASK.md)。

首次正式 Job `job:4c7c9ecf-ca97-425a-88f1-cd24d7e7c7b5` 因 `RESOLUTION_BLOCKED`
失败且未发布，是保留的历史事实；该次授权已经结束，它未被改写为成功。现行三角色路线随后
完成 K4、R2 与 R3 闭环。以下最终条件均已由 [最终验收记录](S1-08_ACCEPTANCE.md) 和冻结
evidence 满足：

- 产生至少一个明确证券范围、日期范围的已发布未复权日线 DatasetVersion。
- 重跑幂等；失败批次不污染已发布版本。
- 数据满足唯一键、排序、OHLC、非负量额、版本化交易日历、逐证券预期交易日缺口和新鲜度
  检查；合法例外有显式决定。
- 每个发布行绑定两条实际来源行；两个来源共同缺行不构成值共识，比较与仲裁共享同一
  Decimal 阈值版本。
- 所有来源的 Canonical `pre_close` 由前序未复权 close 派生；vendor 参考昨收只保留 Raw。
- BaoStock A、腾讯 B、 新浪 C 的实际覆盖、差异、硬超时、预算/失败情况有报告。
- 旧项目日线候选全部被发现并以可复验的冻结结论处置：本项目为 ADR-027 拒绝迁移
  （inventory 成功、两次 pilot 失败即停、诊断与失败证据冻结、旧根未修改、
  `XTQUANT_LEGACY` 不进入发布链）；成功迁移并复核是未采用的另一分支，不构成 Stage 1
  的额外前置。
- SQLite 锁竞争、合法空版本、正式发布并发/单 owner、legacy 硬中断与增量内部断档的恢复和
  幂等回归通过；失败保留脱敏原因链和有界行级证据。
- R2 证券集合、逐证券 coverage、分块、预算、血缘上限和 verifier 由冻结 manifest 驱动，
  DataCenter 不再因首次两证券常量拒绝合法版本，并展示来源健康与 freshness。
- 已知缺口明确列出，不以合成测试或代码就绪冒充真实覆盖。
- 数据中心可按显式版本查看来源、批次、质量、差异和失败。
- Stage 1 最终验收记录、完整质量门和迁移检查通过。

Stage 1 只证明最终验收记录中明确的两证券、日期范围与未复权日线，不证明全 A 股、复权、
point-in-time 股票池、无幸存者偏差或研究/模型/回测能力。Stage 2 前置已满足，但实施仍未
获得用户单独授权、尚未开始。
